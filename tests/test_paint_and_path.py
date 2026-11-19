"""Paint B (5.14.4) and the Path brush (5.15): one-click paint areas, Painted Only patches and face-painted paths."""
import json
import math
import sys
import traceback
from pathlib import Path

import bpy  # noqa: F401  (before bmesh: bpy as a module provides it)
import bmesh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shared_blend import addon, bpy, leave, image, material, capture, select_faces

N = 4   # ground is N x N quads; face index = row * N + column


def grid(mat, wall=True):
    """A flat N x N ground, every face mapped to atlas tile 0, plus (optionally) one wall quad standing on the far
    edge of the first row, sharing the ground's material."""
    verts = [(x, y, 0.0) for y in range(N + 1) for x in range(N + 1)]
    faces = [(r * (N + 1) + c, r * (N + 1) + c + 1, (r + 1) * (N + 1) + c + 1, (r + 1) * (N + 1) + c)
             for r in range(N) for c in range(N)]
    if wall:
        base = len(verts)
        verts += [(N, 0, 1.0), (N, 1, 1.0)]
        faces.append((N, N + (N + 1), base + 1, base))   # stands on the edge x = N, 0 <= y <= 1
    mesh = bpy.data.meshes.new('Ground')
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new('Ground', mesh)
    bpy.context.collection.objects.link(obj)
    mesh.materials.append(mat)
    uv = mesh.uv_layers.new(name='Original UVs')
    corners = [(0.0, 0.0), (0.25, 0.0), (0.25, 1.0), (0.0, 1.0)]
    for poly in mesh.polygons:
        for k, index in enumerate(poly.loop_indices):
            u, v = corners[k]
            uv.data[index].uv = (.002 + u * .984, .002 + v * .996)
    tint = mesh.color_attributes.new(name='Tint', type='BYTE_COLOR', domain='CORNER')
    tint.data.foreach_set('color', [.5, .5, .5, 1.0] * len(mesh.loops))
    return obj


def setup(wall=True):
    leave()
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    atlas = image()
    obj = grid(material(atlas), wall=wall)
    select_faces(obj, [])
    capture(atlas, 0, 1)
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = 'SHARED'
    scene.sprotile_blend_settings_source = 'NEW'
    scene.sprotile_patch_projection = 'UNFOLD'   # the paint tools must not depend on this
    scene.sprotile_patch_fit = 'RECTANGLE'
    c = scene.sprotile_blend_defaults
    c.direction, c.position, c.width, c.smooth, c.edge_style, c.path = 'LEFT_RIGHT', .5, 1.0, True, 'PLAIN', False
    c.alpha_shaping = False
    return obj, atlas


def render_uvs(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    layer = bm.loops.layers.uv['Original UVs']
    return [[tuple(round(c, 6) for c in loop[layer].uv) for loop in face.loops] for face in bm.faces]


def layers(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    return (bm, bm.faces.layers.int.get(addon.SHARED_PATCH_ID), bm.faces.layers.int.get(addon.SHARED_PATCH_FLAGS),
            bm.loops.layers.uv.get(addon.SHARED_FACTOR_UV), addon.edit_blend_layers(obj.data, bm))


def shared_material(obj):
    return next(slot.material for slot in obj.material_slots if slot.material.get(addon.SHARED_MATERIAL_TAG))


# --- Paint B (5.14.4) ---------------------------------------------------------------------------------------

def test_paint_b_one_click():
    obj, atlas = setup()
    scene = bpy.context.scene
    scene.sprotile_blend_a.image = None   # A is not needed: the faces keep their own tile
    select_faces(obj, [5, 6, 9, 10])
    before = render_uvs(obj)
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'FINISHED'}
    assert obj.mode == 'VERTEX_PAINT'
    assert obj.data.color_attributes.active_color.name == 'Tint'
    bpy.ops.object.mode_set(mode='EDIT')
    assert render_uvs(obj) == before, 'Paint B keeps every face\'s tile as A'
    bm, patch, flags, factor, stored = layers(obj)
    painted = [f.index for f in bm.faces if f[patch]]
    assert painted == [5, 6, 9, 10], painted
    assert [f.index for f in bm.faces if f.select] == [5, 6, 9, 10], 'the selection is put back'
    assert len({bm.faces[i][patch] for i in painted}) == 1, 'one paint area'
    for i in painted:
        face = bm.faces[i]
        assert face[flags] & addon.PAINTED_FLAG and face[flags] & addon.TILE_FACE_FLAG
        assert all(abs(loop[factor].uv.x) < 1e-6 for loop in face.loops), 'Position 0: all A until painted'
        assert all(.25 <= loop[stored.b].uv.x <= .5 for loop in face.loops), 'B is tile 1 on every face'
    record = next(r for r in obj.data.sprotile_blend_patches if r.patch_id == bm.faces[5][patch])
    assert record.direction == 'PAINTED' and record.name.startswith('Paint area')
    leave()
    material = shared_material(obj)
    assert 'alpha_result' in addon.shared_nodes(material)
    addon.validate_shared_alpha(material)


def test_paint_b_leaves_existing_blends_and_adds_stage():
    obj, atlas = setup()
    select_faces(obj, [0, 1])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}   # an ordinary Fade, no painted alpha
    bm, patch, flags, factor, _stored = layers(obj)
    fade = bm.faces[0][patch]
    fade_factors = [tuple(loop[factor].uv) for loop in bm.faces[0].loops]
    assert 'alpha_result' not in addon.shared_nodes(shared_material(obj))
    bpy.context.scene.sprotile_blend_settings_source = 'NEW'
    select_faces(obj, [0, 1, 2])
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'FINISHED'}
    bpy.ops.object.mode_set(mode='EDIT')
    bm, patch, flags, factor, _stored = layers(obj)
    assert bm.faces[0][patch] == fade and bm.faces[1][patch] == fade
    assert [tuple(loop[factor].uv) for loop in bm.faces[0].loops] == fade_factors
    assert bm.faces[2][patch] not in (0, fade) and bm.faces[2][flags] & addon.PAINTED_FLAG
    assert 'alpha_result' in addon.shared_nodes(shared_material(obj)), 'the Fade now bends where painted'
    leave()


def test_paint_b_needs_faces_and_b():
    obj, atlas = setup()
    select_faces(obj, [])
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'CANCELLED'}
    bpy.context.scene.sprotile_blend_b.image = None
    select_faces(obj, [3])
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'CANCELLED'}
    assert obj.mode == 'EDIT'
    leave()
    # Object Mode on a mesh without blends: nothing to paint on, say so.
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'CANCELLED'}


def test_painted_only_patch_controls():
    obj, atlas = setup()
    select_faces(obj, [5, 6])
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'FINISHED'}
    bpy.ops.object.mode_set(mode='EDIT')
    bm, patch, flags, factor, _stored = layers(obj)
    record = next(r for r in obj.data.sprotile_blend_patches if r.patch_id == bm.faces[5][patch])
    record.position = .4   # one amount of B everywhere
    bm, patch, flags, factor, _stored = layers(obj)
    assert all(abs(loop[factor].uv.x - .4) < 1e-6 for loop in bm.faces[5].loops)
    record.direction = 'LEFT_RIGHT'
    bm, patch, flags, factor, _stored = layers(obj)
    assert not bm.faces[5][flags] & addon.PAINTED_FLAG
    assert len({round(loop[factor].uv.x, 4) for loop in bm.faces[5].loops}) > 1, 'a gradient again'
    record.direction = 'PAINTED'
    bm, patch, flags, factor, _stored = layers(obj)
    assert bm.faces[5][flags] & addon.PAINTED_FLAG and record.direction == 'PAINTED'
    record.edge_style = 'GRASS'
    bm, patch, flags, factor, _stored = layers(obj)
    assert bm.faces[5][flags] & addon.PAINTED_FLAG, 'changing Style keeps Painted Only'
    # Copy Blend reads it back.
    select_faces(obj, [5])
    recipe = addon.read_blend_recipe(bpy.context)
    assert recipe['settings']['direction'] == 'PAINTED'
    leave()


def test_separate_material_refuses_painted_only():
    obj, atlas = setup()
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = 'SEPARATE'
    scene.sprotile_blend_defaults.direction = 'PAINTED'
    select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'CANCELLED'}
    scene.sprotile_blend_defaults.direction = 'LEFT_RIGHT'
    scene.sprotile_blend_material_mode = 'SHARED'
    leave()


def test_painted_only_has_no_tufts():
    c = addon.BlendSettings(direction='PAINTED', position=1.0, width=.35, smooth=True, angle=0.0, edge_style='SNOW')
    assert addon.shared_edge_mapper(c)((.3, .7))[1] == 0.0
    assert addon.shared_factor_mapper(c)((.3, .7)) == (1.0, 2.0)
    assert not addon.validate_blend_settings(dict(c._asdict(), path=True))['path']


def test_clear_painting():
    obj, atlas = setup()
    leave()
    tint = obj.data.color_attributes['Tint']
    tint.data.foreach_set('color', [.5, .5, .5, .2] * len(obj.data.loops))
    select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_clear_alpha() == {'FINISHED'}
    leave()
    values = [0.0] * (4 * len(obj.data.loops))
    obj.data.color_attributes['Tint'].data.foreach_get('color', values)
    face = obj.data.polygons[0]
    for index in range(len(obj.data.loops)):
        alpha = values[4 * index + 3]
        expected = 1.0 if index in face.loop_indices else .2
        assert abs(alpha - expected) < .01, (index, alpha)
        assert abs(values[4 * index] - .5) < .01, 'the Tint colour stays'


# --- Path brush (5.15) -----------------------------------------------------------------------------------------

def test_border_settings():
    square = [(0, 0), (1, 0), (1, 1), (0, 1)]
    one_edge = addon.border_settings(square, [1, 1, 0, 0], 1.0)   # B along v = 0
    assert one_edge['direction'] == 'ANGLE' and abs(one_edge['angle'] - 1.5 * math.pi) < 1e-6
    assert abs(one_edge['width'] - 1.0) < 1e-6 and abs(one_edge['position'] - .5) < 1e-6
    for targets in ([1, 0, 0, 0], [1, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]):
        settings = addon.border_settings(square, targets, 1.0)
        base = dict(direction='LEFT_RIGHT', position=.5, width=.35, smooth=True)
        mapper = addon.shared_factor_mapper(addon.BlendSettings(**dict(base, **settings)))
        values = [addon.shader_factor(*mapper(p)) for p in square]
        b_corners = [v for v, t in zip(values, targets) if t]
        a_corners = [v for v, t in zip(values, targets) if not t]
        assert min(b_corners) > max(a_corners), (targets, values)
    assert addon.border_settings(square, [1, 1, 1, 1], 1.0) == dict(direction='PAINTED', position=1.0)
    assert addon.border_settings(square, [1, 0, 1, 0], 1.0)['direction'] == 'PAINTED'   # opposite corners
    narrow = addon.border_settings(square, [1, 1, 0, 0], .2)
    assert abs(narrow['width'] - .2) < 1e-6


def path_state(obj):
    bm, patch, flags, factor, stored = layers(obj)
    paint = bm.faces.layers.int.get(addon.PATH_PAINT_ATTRIBUTE)
    return bm, patch, flags, factor, stored, paint


def test_path_from_selection():
    obj, atlas = setup()
    before = render_uvs(obj)
    select_faces(obj, [1, 5, 9, 13])   # a column through the ground
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    assert render_uvs(obj) == before, 'every face keeps its tile as A'
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert [f.index for f in bm.faces if f.select] == [1, 5, 9, 13]
    painted = {f.index for f in bm.faces if f[paint] == addon.PAINTED}
    border = {f.index for f in bm.faces if f[paint] == addon.BORDER}
    assert painted == {1, 5, 9, 13}
    assert border == {0, 4, 8, 12, 2, 6, 10, 14}, border
    wall = bm.faces[N * N]
    assert not wall[patch] and not wall[paint], 'a wall beside the path is left alone'
    for i in painted:
        assert all(abs(loop[factor].uv.x - 1.0) < 1e-6 for loop in bm.faces[i].loops)
        assert bm.faces[i][flags] & addon.PAINTED_FLAG
    painted_vertices = {v.index for i in painted for v in bm.faces[i].verts}
    for i in border:
        face = bm.faces[i]
        assert face[patch]
        for loop in face.loops:
            value = addon.shader_factor(*tuple(loop[factor].uv))
            want = 1.0 if loop.vert.index in painted_vertices else 0.0
            assert abs(value - want) < 1e-3, (i, value, want)
        assert all(.25 <= loop[stored.b].uv.x <= .5 for loop in face.loops)
    assert len({bm.faces[i][patch] for i in border}) == len(border), 'one patch per border face'
    leave()


def test_path_grows_erases_and_updates():
    obj, atlas = setup(wall=False)
    select_faces(obj, [5])
    original = render_uvs(obj)
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    corner = bm.faces[0]   # touches face 5 at one vertex only
    assert corner[paint] == addon.BORDER
    select_faces(obj, [6])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert bm.faces[6][paint] == addon.PAINTED and bm.faces[3][paint] == addon.BORDER
    # Erase both: the ground goes back to what it was, with no blends left.
    select_faces(obj, [5, 6])
    assert bpy.ops.mesh.sprotile_path_selection(action='REMOVE') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert not any(f[paint] for f in bm.faces) and not any(f[patch] for f in bm.faces)
    assert render_uvs(obj) == original
    # Update redoes the edges with the current Feather.
    select_faces(obj, [5])
    bpy.ops.mesh.sprotile_path_selection(action='ADD')
    bpy.context.scene.sprotile_blend_defaults.width = .3
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    widths = bm.faces.layers.float.get(addon.SHARED_PATCH_FLOATS['width'])
    assert abs(bm.faces[4][widths] - .3) < 1e-5, bm.faces[4][widths]
    leave()


def test_path_grass_edges_and_refusals():
    obj, atlas = setup(wall=False)
    scene = bpy.context.scene
    scene.sprotile_blend_defaults.edge_style = 'GRASS'
    select_faces(obj, [5, 6])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert bm.loops.layers.uv.get(addon.SHARED_EDGE_UV) is not None
    assert bm.faces[4][flags] & 16, 'border faces get grass tufts'
    leave()
    addon.validate_shared_grass(shared_material(obj))
    scene.sprotile_blend_defaults.edge_style = 'OVERLAY'
    select_faces(obj, [10])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'CANCELLED'}
    scene.sprotile_blend_defaults.edge_style = 'PLAIN'
    scene.sprotile_blend_b.image = None
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'CANCELLED'}
    leave()


def test_path_survives_object_mode_and_remove_blend():
    obj, atlas = setup(wall=False)
    select_faces(obj, [5, 6, 9, 10])
    bpy.ops.mesh.sprotile_path_selection(action='ADD')
    leave()
    assert obj.data.attributes.get(addon.PATH_PAINT_ATTRIBUTE) is not None
    select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}   # an ordinary Remove Blend on a border face
    select_faces(obj, [9])
    assert bpy.ops.mesh.sprotile_path_selection(action='REMOVE') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert bm.faces[9][paint] == addon.BORDER and bm.faces[9][patch]
    # Remove Blend on a painted face wins: the next update treats it as ground and re-blends around it.
    select_faces(obj, [10])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert bm.faces[10][paint] == addon.BORDER and bm.faces[10][patch]
    assert {f.index for f in bm.faces if f[paint] == addon.PAINTED} == {5, 6}
    leave()


def test_path_is_one_row_in_the_list():
    obj, atlas = setup(wall=False)
    select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}   # an ordinary blend keeps its own row
    bpy.context.scene.sprotile_blend_settings_source = 'NEW'
    select_faces(obj, [6, 10])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    mesh = obj.data
    shown = [r for r, visible in zip(mesh.sprotile_blend_patches, addon.patch_list_visibility(mesh)) if visible]
    paths = addon.path_groups(mesh)
    leads = set(paths.values())
    assert len(leads) == 1, paths
    assert sum(r.patch_id in leads for r in shown) == 1 and len(shown) == 2, [r.name for r in shown]
    count, _hidden, style = addon._patch_overview_cache[mesh.as_pointer()][next(iter(leads))]
    bm, patch, flags, factor, stored, paint = path_state(obj)
    region = {f.index for f in bm.faces if f[paint]}
    assert style == 'PATH_BRUSH' and count == len(region), (count, len(region))
    # Select Faces on the path's row selects the whole path.
    index = next(i for i, r in enumerate(mesh.sprotile_blend_patches) if r.patch_id in leads)
    bpy.context.scene.sprotile_patch_list_index = index
    assert bpy.ops.mesh.sprotile_select_blend_patch(frame=False) == {'FINISHED'}
    bm = bmesh.from_edit_mesh(mesh)
    assert {f.index for f in bm.faces if f.select} == region
    # Object Mode reads the same paths.
    edit_groups = addon.path_groups(mesh)
    leave()
    assert addon.path_groups(mesh) == edit_groups


# --- Undo and hidden faces (5.16.1) ---------------------------------------------------------------------------

UNDO = ('EXEC_DEFAULT', True)   # as a click does: the operator leaves an Undo step


def test_undo_of_a_later_blend_keeps_the_path():
    """Undoing an operator used after the Path tools took the path's records and its grass shader with it."""
    obj, atlas = setup(wall=False)
    leave()
    bpy.ops.ed.undo_push(message='start')
    bpy.ops.object.mode_set(*UNDO, mode='EDIT')
    scene = bpy.context.scene
    scene.sprotile_blend_defaults.edge_style = 'GRASS'
    select_faces(obj, [5, 6])
    bpy.ops.ed.undo_push(message='select')
    assert bpy.ops.mesh.sprotile_path_selection(*UNDO, action='ADD') == {'FINISHED'}

    def state():
        mesh = bpy.context.active_object.data
        bm = bmesh.from_edit_mesh(mesh)
        patch = bm.faces.layers.int.get(addon.SHARED_PATCH_ID)
        ids = {face[patch] for face in bm.faces if face[patch]}
        mat = mesh.materials[0]
        return (ids, {record.patch_id for record in mesh.sprotile_blend_patches},
                bool(mat.get(addon.SHARED_MATERIAL_TAG)), 'edge_result' in addon.shared_nodes(mat))
    path = state()
    assert path[0] and path[0] == path[1] and path[2] and path[3], path
    scene.sprotile_blend_task = 'FADE'
    select_faces(bpy.context.active_object, [14, 15])   # away from the path
    bpy.ops.ed.undo_push(message='select')
    assert bpy.ops.mesh.sprotile_apply_blend(*UNDO) == {'FINISHED'}
    assert len(state()[0]) == len(path[0]) + 1
    bpy.ops.ed.undo()
    assert state() == path, (state(), path)
    bpy.ops.ed.redo()
    after = state()
    assert len(after[0]) == len(path[0]) + 1 and after[0] <= after[1] and after[2] and after[3], after
    leave()


def test_refresh_path_leaves_hidden_faces_alone():
    obj, atlas = setup()
    select_faces(obj, [1, 5, 9, 13])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    before = {i: (bm.faces[i][patch], bm.faces[i][paint]) for i in (5, 4)}
    assert all(before[i][0] and before[i][1] for i in before)
    for i in (5, 4):   # one painted face and one that blends into the path
        bm.faces[i].hide_set(True)
    bmesh.update_edit_mesh(obj.data)
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert {i: (bm.faces[i][patch], bm.faces[i][paint]) for i in (5, 4)} == before
    assert all(bm.faces[i][patch] and bm.faces[i][paint] == addon.PAINTED for i in (1, 9, 13))
    bpy.ops.mesh.reveal()
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    bm, patch, flags, factor, stored, paint = path_state(obj)
    assert {f.index for f in bm.faces if f[paint] == addon.PAINTED} == {1, 5, 9, 13}
    assert {f.index for f in bm.faces if f[paint] == addon.BORDER} == {0, 4, 8, 12, 2, 6, 10, 14}
    leave()


def test_remove_blend_after_a_material_swap():
    """A blend face given another material by hand could not be removed: its row stayed in the list for good."""
    obj, atlas = setup(wall=False)
    bpy.context.scene.sprotile_blend_task = 'FADE'
    select_faces(obj, [0, 1])
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    other = material(atlas)
    obj.data.materials.append(other)
    bm, patch, flags, factor, stored = layers(obj)
    bm.faces[0].material_index = len(obj.data.materials) - 1
    bmesh.update_edit_mesh(obj.data)
    uvs = render_uvs(obj)[0]
    select_faces(obj, [0])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    bm, patch, flags, factor, stored = layers(obj)
    assert not bm.faces[0][patch] and bm.faces[1][patch]
    assert bm.faces[0].material_index == len(obj.data.materials) - 1 and render_uvs(obj)[0] == uvs, 'left as mapped'
    select_faces(obj, [1])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    assert not any(addon.patch_list_visibility(obj.data))
    leave()



def path_face_state(obj, index):
    bm, patch, _flags, factor, stored, paint = path_state(obj)
    face = bm.faces[index]
    return (face[patch], face[paint], tuple(tuple(loop[factor].uv) for loop in face.loops),
            tuple(tuple(loop[stored.b].uv) for loop in face.loops))


def test_path_refresh_preserves_replacement_blend():
    for remove_first in (False, True):
        obj, atlas = setup(wall=False)
        scene = bpy.context.scene
        select_faces(obj, [5, 6])
        assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
        select_faces(obj, [5])
        if remove_first:
            assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
            assert path_face_state(obj, 5)[:2] == (0, 0)
        capture(atlas, 0, 2)
        scene.sprotile_patch_fit = 'FILL'
        assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
        replacement = path_face_state(obj, 5)
        assert replacement[0] > 0 and replacement[1] == 0
        assert replacement[0] not in addon.path_groups(obj.data)
        name = obj.name
        if remove_first:
            leave()
            atlas.pack()
            saved = Path(__file__).with_name('path_replacement_saved_state.blend')
            try:
                bpy.ops.wm.save_as_mainfile(filepath=str(saved))
                bpy.ops.wm.open_mainfile(filepath=str(saved))
            finally:
                saved.unlink(missing_ok=True)
            obj = bpy.data.objects[name]
            atlas = bpy.context.scene.sprotile_blend_b.image
            select_faces(obj, [5])
        capture(atlas, 0, 1)
        assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
        assert path_face_state(obj, 5) == replacement
        assert path_face_state(obj, 6)[1] == addon.PAINTED


def test_plain_mapping_releases_path_ownership():
    obj, atlas = setup(wall=False)
    select_faces(obj, [5])
    assert bpy.ops.mesh.sprotile_path_selection(action='ADD') == {'FINISHED'}
    bpy.context.scene.sprotile_active_col = 2
    assert bpy.ops.mesh.sprotile_map_selected(mapping_mode='PER_FACE') == {'FINISHED'}
    assert path_face_state(obj, 5)[:2] == (0, 0)
    assert bpy.ops.mesh.sprotile_path_selection(action='REFRESH') == {'FINISHED'}
    assert path_face_state(obj, 5)[:2] == (0, 0)


results = []
for test in (test_paint_b_one_click, test_paint_b_leaves_existing_blends_and_adds_stage, test_paint_b_needs_faces_and_b,
             test_painted_only_patch_controls, test_separate_material_refuses_painted_only, test_painted_only_has_no_tufts,
             test_clear_painting, test_border_settings, test_path_from_selection, test_path_grows_erases_and_updates,
             test_path_grass_edges_and_refusals, test_path_survives_object_mode_and_remove_blend,
             test_path_is_one_row_in_the_list, test_refresh_path_leaves_hidden_faces_alone,
             test_remove_blend_after_a_material_swap,
             test_undo_of_a_later_blend_keeps_the_path,
             test_path_refresh_preserves_replacement_blend, test_plain_mapping_releases_path_ownership):
    try:
        test()
    except Exception:
        traceback.print_exc()
        results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
    else:
        results.append(dict(name=test.__name__, passed=True))
    try:
        leave()
    except Exception:
        pass
report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
Path(__file__).with_name('paint_and_path_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
addon.unregister()
if report['passed'] != report['total']:
    raise SystemExit(1)
