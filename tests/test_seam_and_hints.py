"""Sprotile 5.17.0: Blend This Seam, the Path Brush hover preview, the status line and the lines that say why a
button would refuse."""
import json
import sys
import traceback
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shared_blend as t
addon, bpy, bmesh = t.addon, t.bpy, t.bmesh

# Grid helpers from the Paint B / Path brush suite, without running that suite.
pp = type(sys)('paint_and_path_helpers')
pp.__dict__['__file__'] = str(Path(__file__).with_name('test_paint_and_path.py'))
_source = Path(pp.__file__).read_text(encoding='utf-8')
exec(compile(_source[:_source.index('results = []')], 'test_paint_and_path', 'exec'), pp.__dict__)
N = 4
SEAM = 3


def ground(tiles):
    """A 4 x 4 ground; tiles maps a face index to the atlas column it shows (default 0). B is captured at 1."""
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    scene = bpy.context.scene
    for column in sorted(set(tiles.values())):
        pp.select_faces(obj, [index for index, value in tiles.items() if value == column])
        scene.sprotile_active_col = column
        assert bpy.ops.mesh.sprotile_map_selected(mapping_mode='PER_FACE') == {'FINISHED'}
    scene.sprotile_active_col = 0
    scene.sprotile_blend_task = 'SEAM'
    scene.sprotile_blend_defaults.width = 1.0
    return obj, atlas


def select_edges_between(obj, pairs):
    """Select exactly the edges shared by the given pairs of faces."""
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    wanted = {frozenset(pair) for pair in pairs}
    for edge in bm.edges:
        if frozenset(face.index for face in edge.link_faces) in wanted:
            edge.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)


def seam_state(obj):
    bm, patch, flags, factor, stored, paint = pp.path_state(obj)
    rows = {}
    for face in bm.faces:
        rows[face.index] = dict(
            patch=face[patch] if patch is not None else 0, mark=face[paint] if paint is not None else 0,
            factor=[round(addon.shader_factor(*tuple(loop[factor].uv)), 3) for loop in face.loops] if factor else [],
            x=[round(loop.vert.co.x, 3) for loop in face.loops], y=[round(loop.vert.co.y, 3) for loop in face.loops],
            b=[round(loop[stored.b].uv.x, 3) for loop in face.loops] if stored.b is not None else [])
    return rows


RIGHT_HALF = {row * N + column: 2 for row in range(N) for column in (2, 3)}
STRAIGHT = [(row * N + 1, row * N + 2) for row in range(N)]


def test_straight_seam_fades_both_sides_into_each_other():
    obj, atlas = ground(RIGHT_HALF)
    scene = bpy.context.scene
    before = pp.render_uvs(obj)
    captured = (scene.sprotile_blend_b.col, scene.sprotile_blend_b.row, scene.sprotile_blend_a.image)
    select_edges_between(obj, STRAIGHT)
    bm = bmesh.from_edit_mesh(obj.data)
    selection = ([e.select for e in bm.edges], [f.select for f in bm.faces])
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    assert pp.render_uvs(obj) == before, 'every face keeps its own tile as A'
    assert (scene.sprotile_blend_b.col, scene.sprotile_blend_b.row, scene.sprotile_blend_a.image) == captured, \
        'New Blend A and B are left as they were'
    bm = bmesh.from_edit_mesh(obj.data)
    assert ([e.select for e in bm.edges], [f.select for f in bm.faces]) == selection
    state = seam_state(obj)
    for index, row in state.items():
        column = index % N
        if column in (1, 2):
            assert row['patch'] and row['mark'] == SEAM, (index, row)
            # Half and half on the seam (x = 2), the face's own tile at its far side.
            assert row['factor'] == [0.5 if x == 2.0 else 0.0 for x in row['x']], (index, row)
            # B is the tile from across the seam: column 2 of the atlas (u .5 to .75) or column 0 (0 to .25).
            low, high = (.5, .75) if column == 1 else (0.0, .25)
            assert all(low - 1e-3 <= u <= high + 1e-3 for u in row['b']), (index, row)
        else:
            assert not row['patch'] and not row['mark'], (index, row)
    assert len({state[i]['patch'] for i in state if state[i]['patch']}) == 8, 'each face is its own patch'
    # One row in Blends on This Mesh, and Select Faces on it would take the whole seam.
    visible = [record.name for record, shown in zip(obj.data.sprotile_blend_patches,
                                                    addon.patch_list_visibility(obj.data)) if shown]
    assert len(visible) == 1 and visible[0].startswith('Seam '), visible
    # Again: the same result, and no new records.
    records = len(obj.data.sprotile_blend_patches)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    assert len(obj.data.sprotile_blend_patches) == records and seam_state(obj) == state
    pp.leave()


def test_feather_narrows_the_fade_towards_the_seam():
    obj, atlas = ground(RIGHT_HALF)
    bpy.context.scene.sprotile_blend_defaults.width = 0.5
    select_edges_between(obj, STRAIGHT)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = pp.path_state(obj)
    face = bm.faces[1]
    assert sorted(round(addon.shader_factor(*tuple(loop[factor].uv)), 3) for loop in face.loops) == [0, 0, .5, .5]
    # Halfway across the face nothing of B shows yet: the band is half as wide.
    record = next(r for r in obj.data.sprotile_blend_patches if r.patch_id == face[patch])
    assert abs(record.width - 1.0) < 1e-4 and abs(record.position - 1.0) < 1e-4, (record.width, record.position)
    pp.leave()


def test_seam_with_a_corner_and_an_island():
    # Tile 2 on the top-right 2 x 2 block: an L-shaped seam; and one lone face of tile 3 inside tile 0.
    tiles = {index: 2 for index in (10, 11, 14, 15)}
    tiles[4] = 3
    obj, atlas = ground(tiles)
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='SELECT')   # every edge: only those between different tiles count
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    state = seam_state(obj)
    # The face diagonal to the block's corner (5) touches the seam at one corner only and still blends.
    assert state[5]['patch'] and state[5]['mark'] == SEAM
    corner = [f for f, x, y in zip(state[5]['factor'], state[5]['x'], state[5]['y']) if (x, y) == (2.0, 2.0)]
    assert corner and 0.2 < corner[0] <= 0.5, state[5]
    # The island has the seam on every side: an even mix, not solid B.
    assert state[4]['patch'] and state[4]['factor'] == [0.5] * 4, state[4]
    # Faces of the block next to the seam blend towards tile 0.
    assert all(u <= .25 + 1e-3 for u in state[10]['b']), state[10]
    # Far from any seam nothing changes.
    assert not state[3]['patch'] and not state[12]['patch']
    pp.leave()


def test_selected_faces_mean_the_edges_between_them():
    """5.17.1: two faces picked across a seam blend across their shared edge, not around the whole of each face."""
    tiles = {5: 2}   # one lone face of another tile, as a single transition tile laid into grass
    obj, atlas = ground(tiles)
    bpy.ops.mesh.select_mode(type='FACE')
    pp.select_faces(obj, [4, 5])   # the tile and the face on its left
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    state = seam_state(obj)
    # The tile fades from the shared edge (x = 1) across to nothing at its far side: one side to the other.
    assert state[5]['factor'] == [0.5 if x == 1.0 else 0.0 for x in state[5]['x']], state[5]
    assert state[4]['factor'] == [0.5 if x == 1.0 else 0.0 for x in state[4]['x']], state[4]
    # Its other three sides are not seams: the face on its right is untouched.
    assert not state[6]['patch'] and not state[6]['mark'], state[6]
    # A closed loop of edges around the one face (which selects the face) still means all four edges.
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    select_edges_between(obj, [(5, 4), (5, 6), (5, 1), (5, 9)])
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    state = seam_state(obj)
    assert state[5]['factor'] == [0.5] * 4 and state[6]['patch'], (state[5], state[6])
    pp.leave()


def test_one_side_only():
    """5.17.1: Sides. One tile's faces fade fully into the other at the seam; the faces across it are untouched."""
    scene = bpy.context.scene
    seen = []
    for side in ('FIRST', 'SECOND'):
        obj, atlas = ground(RIGHT_HALF)
        scene.sprotile_seam_sides = side
        select_edges_between(obj, STRAIGHT)
        assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
        state = seam_state(obj)
        blended = {index % N for index, row in state.items() if row['patch']}
        assert blended in ({1}, {2}), (side, blended)
        seen.append(blended)
        for index, row in state.items():
            if row['patch']:
                assert row['factor'] == [1.0 if x == 2.0 else 0.0 for x in row['x']], (index, row)
        pp.leave()
    scene.sprotile_seam_sides = 'BOTH'
    assert seen[0] != seen[1], 'the two choices are the two sides'


def test_update_path_and_remove_blend_with_a_seam():
    obj, atlas = ground(RIGHT_HALF)
    select_edges_between(obj, STRAIGHT)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    state = seam_state(obj)
    # A path elsewhere, then Update Path Edges: the seam is not the path's to clear.
    bpy.ops.mesh.select_mode(type='FACE')
    pp.select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    after = seam_state(obj)
    for index in (2, 6, 10, 14, 9, 13):   # seam faces the path does not reach
        assert after[index]['patch'] == state[index]['patch'] and after[index]['mark'] == SEAM, (index, after[index])
    # The path and the seam are two rows.
    visible = [record.name for record, shown in zip(obj.data.sprotile_blend_patches,
                                                    addon.patch_list_visibility(obj.data)) if shown]
    assert sorted(name.split()[0] for name in visible) == ['Path', 'Seam'], visible
    # Remove Blend takes a seam face back to its tile.
    pp.select_faces(obj, [14])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    assert not seam_state(obj)[14]['patch']
    pp.leave()


def test_refresh_keeps_a_seam_that_replaced_path_borders():
    obj, _atlas = ground(RIGHT_HALF)
    bpy.ops.mesh.select_mode(type='FACE')
    pp.select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    assert seam_state(obj)[1]['mark'] == addon.BORDER
    select_edges_between(obj, STRAIGHT)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    before = seam_state(obj)
    assert before[1]['mark'] == SEAM
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    assert seam_state(obj)[1] == before[1]
    assert seam_state(obj)[5] == before[5]


def test_seam_refusals_change_nothing():
    obj, atlas = ground(RIGHT_HALF)
    blend_seam = addon.MESH_OT_sprotile_blend_seam
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')
    assert not blend_seam.poll(bpy.context) and addon.seam_blocker(bpy.context) == 'Select the edges where two tiles meet'
    # Edges inside one tile are not a seam.
    select_edges_between(obj, [(0, 1), (4, 5)])
    before = seam_state(obj)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'CANCELLED'}
    assert seam_state(obj) == before and len(obj.data.sprotile_blend_patches) == 0
    # Separate Material is refused with a reason.
    select_edges_between(obj, STRAIGHT)
    bpy.context.scene.sprotile_blend_material_mode = 'SEPARATE'
    assert bpy.ops.mesh.sprotile_blend_seam() == {'CANCELLED'}
    assert seam_state(obj) == before
    bpy.context.scene.sprotile_blend_material_mode = 'SHARED'
    pp.leave()
    assert addon.seam_blocker(bpy.context) == 'Enter Edit Mode on a mesh'


# --- 5.17.2: findings of the outside review of 5.17.1 -------------------------------------------------------

def whole_state(obj):
    """Everything a cancelled Blend This Seam must leave alone, including whether layers exist at all."""
    mesh, scene = obj.data, bpy.context.scene
    bm = bmesh.from_edit_mesh(mesh)
    material = mesh.materials[0]
    record = scene.sprotile_blend_b
    return dict(faces=seam_state(obj), uvs=pp.render_uvs(obj),
                face_layers=sorted(bm.faces.layers.int.keys()) + sorted(bm.faces.layers.float.keys()),
                loop_layers=sorted(bm.loops.layers.uv.keys()) + sorted(bm.loops.layers.float_vector.keys()),
                records=[(r.patch_id, r.name) for r in mesh.sprotile_blend_patches],
                shared=bool(material.get(addon.SHARED_MATERIAL_TAG)), nodes=len(material.node_tree.nodes),
                slots=[face.material_index for face in bm.faces], materials=[m.name for m in mesh.materials],
                b=(record.image, record.col, record.row, record.is_sampled),
                selection=([e.select for e in bm.edges], [f.select for f in bm.faces]))


def test_a_refusal_in_a_later_tile_group_takes_everything_back():
    obj, atlas = ground(RIGHT_HALF)
    select_edges_between(obj, STRAIGHT)   # two groups: faces fading into tile 2, faces fading into tile 0
    before = whole_state(obj)
    assert 'sprotile_path_paint' not in before['face_layers']
    real, calls = addon.apply_shared_texture_blend, []

    def second_call_refuses(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('refused for the test')
        return real(*args, **kwargs)
    with mock.patch.object(addon, 'apply_shared_texture_blend', side_effect=second_call_refuses):
        assert bpy.ops.mesh.sprotile_blend_seam() == {'CANCELLED'}
    assert len(calls) == 2, 'the first group had been applied'
    after = whole_state(obj)
    assert after == before, {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    # And it still works afterwards.
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    pp.leave()


def test_a_seam_nothing_can_blend_creates_nothing():
    obj, atlas = ground(RIGHT_HALF)
    # The right half on a second material with its own image: Shared Material cannot blend across atlases.
    other = pp.material(atlas.copy())
    obj.data.materials.append(other)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for index in RIGHT_HALF:
        bm.faces[index].material_index = 1
    bmesh.update_edit_mesh(obj.data)
    select_edges_between(obj, STRAIGHT)
    before = whole_state(obj)
    assert bpy.ops.mesh.sprotile_blend_seam() == {'CANCELLED'}
    after = whole_state(obj)
    assert after == before, {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    pp.leave()


def test_edge_select_keeps_every_selected_edge():
    """In Edge Select, two faces with all their edges selected count as selected faces; a seam edge picked
    elsewhere was then dropped."""
    obj, atlas = ground(RIGHT_HALF)
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for edge in list(bm.faces[1].edges) + list(bm.faces[2].edges):
        edge.select_set(True)
    for edge in bm.edges:
        if {face.index for face in edge.link_faces} == {13, 14}:
            edge.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    assert bm.faces[1].select and bm.faces[2].select and not bm.faces[13].select
    assert bpy.ops.mesh.sprotile_blend_seam() == {'FINISHED'}
    state = seam_state(obj)
    assert all(state[index]['patch'] for index in (1, 2, 13, 14)), {i: state[i]['patch'] for i in (1, 2, 13, 14)}
    pp.leave()


def test_paint_b_strength_zero_is_zero():
    for strength in (0.0, 1.0, 2.0):
        pp.N = N
        obj, atlas = pp.setup(wall=False)
        scene = bpy.context.scene
        scene.sprotile_blend_task = 'PAINTED'
        scene.sprotile_blend_defaults.alpha_strength = strength
        pp.select_faces(obj, [5, 6])
        assert bpy.ops.mesh.sprotile_paint_alpha() == {'FINISHED'}
        found = addon.shared_alpha_strength(obj.data.materials[0])
        assert abs(found - strength) < 1e-6, (strength, found)
        pp.leave()
    bpy.context.scene.sprotile_blend_defaults.alpha_strength = 2.0


def test_copied_history_key_is_not_guessed():
    """5.17.3: two materials with one history key and no owner record (a 5.15.1 file after a material copy). The
    original is not guessed from the names; the user's click in the material list decides, once."""
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    scene = bpy.context.scene
    scene.sprotile_blend_task = 'FADE'
    scene.sprotile_blend_material_mode = 'SEPARATE'
    addon.capture_blend_tile(scene, 'A', atlas)
    pp.select_faces(obj, [0, 1])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    scene.sprotile_blend_material_mode = 'SHARED'
    mesh = obj.data
    original = mesh.materials[0]
    copy = original.copy()                       # carries the same key
    copy.name = 'A name that proves nothing'
    key = original[addon.HISTORY_KEY_PROPERTY]
    assert copy[addon.HISTORY_KEY_PROPERTY] == key
    del mesh[addon.HISTORY_OWNERS]                # as 5.15.1 saved it: no owner record
    mesh.materials.append(copy)
    blend_slot = next(i for i, m in enumerate(mesh.materials) if m.get('sprotile_blend_material'))
    assert addon.history_owner(key, mesh) is None, 'two candidates and no record: nothing is guessed'
    # Remove Blend with the blend material's slot active: refused with the reason, faces untouched.
    obj.active_material_index = blend_slot
    pp.select_faces(obj, [0, 1])
    bm = bmesh.from_edit_mesh(mesh); bm.faces.ensure_lookup_table()
    assert bpy.ops.mesh.sprotile_remove_blend() == {'CANCELLED'}
    bm = bmesh.from_edit_mesh(mesh); bm.faces.ensure_lookup_table()
    assert bm.faces[0].material_index == blend_slot
    message = addon.history_conflict(obj, bm.faces[0][bm.faces.layers.int.get(addon.BLEND_ORIGINAL_MATERIAL)])
    assert message and original.name in message and copy.name in message, message
    # The user clicks the right material in the list: that settles it, and it stays settled.
    obj.active_material_index = 0
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    bm = bmesh.from_edit_mesh(mesh); bm.faces.ensure_lookup_table()
    assert mesh.materials[bm.faces[0].material_index] == original
    obj.active_material_index = blend_slot
    assert addon.history_owner(key, mesh) == original, 'recorded on the mesh'
    pp.leave()


def test_paint_b_says_when_faces_keep_their_blend():
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    scene = bpy.context.scene
    scene.sprotile_blend_task = 'FADE'
    addon.capture_blend_tile(scene, 'A', atlas)
    pp.select_faces(obj, [0, 1])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    pp.select_faces(obj, [0, 1, 2])
    heard = []
    stub = SimpleNamespace(report=lambda kind, text: heard.append(text))
    assert addon.MESH_OT_sprotile_paint_alpha.execute(stub, bpy.context) == {'FINISHED'}
    assert any("2 faces already in a blend keep that blend's B" in text and '1 face ready' in text for text in heard), heard
    pp.leave()


# --- why a button would refuse ------------------------------------------------------------------------------

class Layout:
    def __init__(self):
        self.labels, self.ops = [], []
    def panel(self, idname, **kw):
        return self, None
    def label(self, text='', **kw):
        self.labels.append(text)
    def operator(self, name, **kw):
        self.ops.append(name)
        return SimpleNamespace()
    def __getattr__(self, name):
        return lambda *a, **k: self


def panel_labels():
    layout = Layout()
    with mock.patch.object(addon, 'prefs', return_value=None):
        addon.VIEW3D_PT_sprotile_blend.draw(SimpleNamespace(layout=layout), bpy.context)
    return layout


def test_reasons_under_the_buttons():
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    scene = bpy.context.scene
    scene.sprotile_blend_task = 'FADE'
    blocker = addon.action_blocker
    # Edit Mode, B captured by setup, A not, nothing selected.
    scene.sprotile_blend_a.image = None
    assert 'Capture A' in blocker(bpy.context, 'apply')
    assert 'Capture A first (pick a tile, then Capture A)' in panel_labels().labels
    addon.capture_blend_tile(scene, 'A', atlas)
    assert blocker(bpy.context, 'apply') == 'Select the faces to blend'
    pp.select_faces(obj, [0, 1])
    assert blocker(bpy.context, 'apply') is None
    assert not [text for text in panel_labels().labels if 'Capture' in text and 'first' in text]
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}, 'no reason shown, and it does work'
    pp.select_faces(obj, [])   # back to New Blend: with a blend selected the panel shows Edit Blend
    # The Path Brush wants B; Paint B wants faces.
    scene.sprotile_blend_task = 'PATH_BRUSH'
    assert blocker(bpy.context, 'path_brush') is None
    image, scene.sprotile_blend_b.image = scene.sprotile_blend_b.image, None
    assert 'Capture B' in blocker(bpy.context, 'path_brush')
    assert any('Capture B' in text for text in panel_labels().labels)
    scene.sprotile_blend_b.image = image
    assert blocker(bpy.context, 'paint_b') == 'Select the faces to paint on'
    assert blocker(bpy.context, 'remove') == 'Select the blended faces first'
    # The seam task shows its own button and reason, and no A / B capture row.
    scene.sprotile_blend_task = 'SEAM'
    layout = panel_labels()
    assert 'mesh.sprotile_blend_seam' in layout.ops and 'mesh.sprotile_apply_blend' not in layout.ops
    assert 'mesh.sprotile_capture_blend_tile' not in layout.ops
    assert 'Select the edges where two tiles meet' in layout.labels
    # Outside Edit Mode every action names the mode.
    pp.leave()
    for action in ('apply', 'path_brush', 'remove'):
        assert blocker(bpy.context, action) == 'Press Tab for Edit Mode', action
    assert not addon.MESH_OT_sprotile_apply_blend.poll(bpy.context)
    scene.sprotile_blend_task = 'FADE'
    assert 'Press Tab for Edit Mode' in panel_labels().labels


# --- status line and hover preview --------------------------------------------------------------------------

def test_status_lines_name_the_tile_and_the_brush():
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    scene = bpy.context.scene
    scene.sprotile_active_col, scene.sprotile_active_row, scene.sprotile_brush_radius = 2, 0, 40
    scene.sprotile_rotation = '90'
    lines = addon.paint_status(scene)
    assert 'Tile (2, 0)' in lines[0] and '90deg' in lines[0] and 'Size 40 px' in lines[0] and '[ ]' in lines[1], lines
    scene.sprotile_rotation = '0'
    scene.sprotile_blend_defaults.edge_style, scene.sprotile_blend_defaults.width = 'GRASS', 0.5
    line = addon.path_status(scene)[0]
    assert 'B (1, 0)' in line and 'grass edge' in line and 'Feather 50%' in line, line
    assert '(erasing)' in addon.path_status(scene, True)[0]
    scene.sprotile_blend_b.image = None
    assert 'B not captured' in addon.path_status(scene)[0]
    scene.sprotile_blend_defaults.edge_style = 'PLAIN'
    # Off in the preferences: nothing is drawn.
    with mock.patch.object(addon, 'prefs', return_value=SimpleNamespace(show_status_line=False)):
        assert not addon.status_enabled(bpy.context)
    with mock.patch.object(addon, 'prefs', return_value=None):
        assert addon.status_enabled(bpy.context)
    pp.leave()


def test_path_brush_hover_preview():
    pp.N = N
    obj, atlas = pp.setup(wall=False)
    cls = addon.MESH_OT_sprotile_path_brush
    stub = SimpleNamespace(obj=obj, painting=False, over_ui=False, hover=[], hover_erase=False,
                           bm=bmesh.from_edit_mesh(obj.data))
    stub.bm.faces.ensure_lookup_table()
    stub.update_hover = MethodType(cls.update_hover, stub)
    stub.faces_under_brush = lambda context: {5, 6}
    stub.update_hover(bpy.context)
    assert len(stub.hover) == 2 and all(len(corners) == 4 for corners in stub.hover) and not stub.hover_erase
    world = obj.matrix_world
    assert [tuple(c) for c in stub.hover[0]] == [tuple(world @ v.co) for v in stub.bm.faces[5].verts]
    stub.update_hover(bpy.context, erase=True)
    assert stub.hover and stub.hover_erase
    # Nothing while a stroke runs, over the sidebar, or when the look-up fails.
    stub.painting = True
    stub.update_hover(bpy.context)
    assert stub.hover == []
    stub.painting, stub.over_ui = False, True
    stub.update_hover(bpy.context)
    assert stub.hover == []
    stub.over_ui = False
    stub.faces_under_brush = lambda context: 1 / 0
    stub.update_hover(bpy.context)
    assert stub.hover == []
    pp.leave()


results = []
for test in (test_straight_seam_fades_both_sides_into_each_other, test_feather_narrows_the_fade_towards_the_seam,
             test_seam_with_a_corner_and_an_island, test_selected_faces_mean_the_edges_between_them,
             test_one_side_only,
             test_update_path_and_remove_blend_with_a_seam, test_refresh_keeps_a_seam_that_replaced_path_borders,
             test_seam_refusals_change_nothing, test_a_refusal_in_a_later_tile_group_takes_everything_back,
             test_a_seam_nothing_can_blend_creates_nothing, test_edge_select_keeps_every_selected_edge,
             test_paint_b_strength_zero_is_zero, test_copied_history_key_is_not_guessed,
             test_paint_b_says_when_faces_keep_their_blend, test_reasons_under_the_buttons,
             test_status_lines_name_the_tile_and_the_brush, test_path_brush_hover_preview):
    try:
        test()
    except Exception:
        traceback.print_exc()
        results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
    else:
        results.append(dict(name=test.__name__, passed=True))
    try:
        pp.leave()
    except Exception:
        pass
report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
Path(__file__).with_name('seam_and_hints_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
addon.unregister()
if report['passed'] != report['total']:
    raise SystemExit(1)
