"""Blend This Seam: fade two tiles into each other across the edges where they meet.

Lay the tiles down first (grass here, dirt there), select the edges between them and click once:

- every face along the seam keeps its own tile as A and gets the tile from across the seam as B;
- the fade runs from nothing at the face's far side to half and half at the seam, so the two sides meet in the
  same mix. Feather narrows it towards the seam;
- each face is its own small patch (as the Path brush's edge faces are), because a seam may bend. The faces are
  marked SEAM in PATH_PAINT_ATTRIBUTE, so Blends on This Mesh shows one row per seam.
- with faces selected, the seam is the edges BETWEEN the selected faces; with only edges selected, those edges.

Nothing is read from New Blend's A and B, and they are left as they were.
"""

from collections import Counter

import bmesh
import bpy

SEAM = 3   # PATH_PAINT_ATTRIBUTE: 1 painted and 2 border belong to the Path brush
SEAM_OVERRIDES = dict(tiles='FACE', path=False, alpha_shaping=False, direction='ANGLE', edge_style='PLAIN')


def tile_reader(obj, bm, scene):
    """face -> (image, mode, column, row) of the tile it shows, or None when it does not show one whole or half
    tile of the grid (the reason is kept in reader.problems)."""
    mesh, slots, size = obj.data, obj.material_slots, scene.sprotile_tile_size
    sources, problems = {}, Counter()

    def source(index):
        if index not in sources:
            found = None
            material = slots[index].material if index < len(slots) else None
            texture = atlas_node_from_material(material) if material is not None else None
            if texture is not None and texture.image is not None and min(image_size(texture.image)) > 0:
                try:
                    found = (texture.image, pipette_uv_layer(mesh, bm, texture), image_size(texture.image))
                except ValueError:
                    found = None
            sources[index] = found
        return sources[index]

    def read(face):
        found = source(face.material_index)
        if found is None:
            problems['no atlas'] += 1
            return None
        image, layer, atlas = found
        try:
            mode, column, row, _placement = pipette_tile_grid(face, layer, obj, size, atlas)
        except ValueError:
            problems['not one tile'] += 1
            return None
        return image, mode, column, row
    read.problems = problems
    return read


def seam_plan(obj, bm, scene, edges=None, sides='BOTH'):
    """What Blend This Seam would do: ({face index: tile from across the seam}, {face index: corner targets},
    number of seam edges, reader). edges defaults to the selected ones."""
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    read = tile_reader(obj, bm, scene)
    tiles = {}

    def tile(face):
        if face.index not in tiles:
            tiles[face.index] = None if face.hide else read(face)
        return tiles[face.index]
    if edges is None:
        edges = [edge for edge in bm.edges if edge.select and not edge.hide]
        # Selected FACES mean "the seam between these": only the edges they share, not every edge around them
        # (two faces picked across a seam blend across that one edge). Edges picked on their own are used as they are.
        # Only in Face Select: in Edge Select every selected edge was picked on purpose, even where a face happens
        # to have all its edges selected.
        if bm.select_mode == {'FACE'}:
            between = [edge for edge in edges
                       if len(edge.link_faces) > 1 and all(face.select for face in edge.link_faces if not face.hide)]
            if between:
                edges = between
    seam_edges = []
    for edge in edges:
        faces = [face for face in edge.link_faces if not face.hide]
        if len(faces) != 2:
            continue
        first, second = tile(faces[0]), tile(faces[1])
        if first is None or second is None or first == second:
            continue
        seam_edges.append((edge, faces))
    seam_vertices = {vert for edge, _faces in seam_edges for vert in edge.verts}
    votes = {}
    for _edge, (first, second) in seam_edges:
        # The face across a seam edge decides; faces that only touch the seam at a corner follow their neighbours.
        votes.setdefault(first.index, Counter())[tile(second)] += 100
        votes.setdefault(second.index, Counter())[tile(first)] += 100
    for vert in seam_vertices:
        around = [face for face in vert.link_faces if not face.hide and tile(face) is not None]
        for face in around:
            for other in around:
                if tile(other) != tile(face):
                    votes.setdefault(face.index, Counter())[tile(other)] += 1
    partner = {}
    for index, counter in votes.items():
        # The same image only: Shared Material blends two tiles of one atlas.
        choices = [(count, repr(key[1:]), key) for key, count in counter.items() if key[0] == tiles[index][0]]
        if choices:
            partner[index] = max(choices)[2]
    level = 0.5
    if sides != 'BOTH' and partner:
        # One side only: those faces fade all the way into the other tile at the seam, and the faces across it
        # are left as they are. "First" is the tile that sorts first, so the choice is the same every time.
        first = min((tiles[index] for index in partner), key=lambda tile: (tile[0].name,) + tile[1:])
        partner = {index: tile for index, tile in partner.items() if (tiles[index] == first) == (sides == 'FIRST')}
        level = 1.0
    targets = {index: [level if loop.vert in seam_vertices else 0.0 for loop in bm.faces[index].loops]
               for index in partner}
    return partner, targets, len(seam_edges), read


def seam_settings(mask, targets, feather):
    """One face's fade: A at its far side, half and half at the seam."""
    if max(targets) - min(targets) < 1e-6:
        return dict(direction='PAINTED', position=max(targets))   # every corner is on a seam: an even mix
    found = border_settings(mask, targets, feather)
    if found['direction'] == 'PAINTED':
        found['position'] = min(1.0, sum(targets) / len(targets))
    return found


def set_tile_record(record, scene, tile):
    image, mode, column, row = tile
    width, height = image_size(image)
    record.image, record.col, record.row = image, column, row
    record.width_px, record.height_px = tile_dimensions(scene.sprotile_tile_size, mode)
    record.atlas_width, record.atlas_height = width, height
    record.rotation, record.flip_u, record.flip_v = 0, False, False
    record.inset_px = get_uv_inset(scene)
    record.is_sampled, record.sample_uv = False, (1, 0, 0, 0, 1, 0)


def blend_seam(context, obj, edges=None):
    """Blend the selected seam edges of obj (in Edit Mode). Returns (faces blended, seam edges, faces left out)."""
    scene, mesh = context.scene, obj.data
    if scene.sprotile_blend_material_mode != 'SHARED':
        raise ValueError('Blend This Seam uses the Shared Material; switch Material under Blend Options')
    upgrade_file_blends()
    bm = bmesh.from_edit_mesh(mesh)
    partner, targets, edge_count, read = seam_plan(obj, bm, scene, edges, scene.sprotile_seam_sides)
    if not edge_count:
        if read.problems:
            raise ValueError('The faces beside the selected edges do not each show one tile of the atlas grid; '
                             'check Tile Size, or map them first')
        raise ValueError('Select the edges where two different tiles meet')
    path_layer(mesh, bm, create=False)   # refuses a reserved name before anything changes, and creates nothing yet
    record = scene.sprotile_blend_b
    kept = {name: (tuple(getattr(record, name)) if name == 'sample_uv' else getattr(record, name))
            for name in BLEND_TILE_FIELDS}
    feather = scene.sprotile_blend_defaults.width
    groups = {}
    for index, tile in partner.items():
        groups.setdefault(tile, []).append(index)
    key, done, skipped, journal = mesh.as_pointer(), [], 0, []
    try:
        # Sorted, so the same seam always gets its patches in the same order.
        for tile in sorted(groups, key=lambda item: (item[0].name,) + item[1:]):
            usable = usable_face_test(obj, tile[0])
            bm = bmesh.from_edit_mesh(mesh)
            bm.faces.ensure_lookup_table()
            faces = sorted(index for index in groups[tile] if usable(bm.faces[index]))
            skipped += len(groups[tile]) - len(faces)
            if not faces:
                continue
            set_tile_record(record, scene, tile)

            def settings(indices, masks):
                return seam_settings(masks[indices[0]], targets[indices[0]], feather)

            def apply(method):
                return apply_shared_texture_blend(
                    context, overrides=SEAM_OVERRIDES, keep_a=True, patch_groups={key: [[index] for index in faces]},
                    patch_settings=settings, method=method, fit='FILL', patch_name='Seam',
                    controls=scene.sprotile_blend_defaults, faces={key: faces}, undo_log=journal,
                    preserve_path_marks=True)
            try:
                apply('EXISTING')
            except ValueError as error:
                if 'Existing UVs' not in str(error):
                    raise
                apply('UNFOLD')
            done.extend(faces)
        if done:
            bm = bmesh.from_edit_mesh(mesh)
            bm.faces.ensure_lookup_table()
            layer = path_layer(mesh, bm)
            for index in done:
                bm.faces[index][layer] = SEAM
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    except Exception:
        # All or nothing: a seam with several tiles along it is several Apply calls, and a refusal in a later
        # one must not leave the earlier ones on the mesh.
        rollback_shared_blends(journal)
        raise
    finally:
        for name, value in kept.items():
            setattr(record, name, value)
    tag_redraw_view3d()
    return len(done), edge_count, skipped


def seam_blocker(context):
    """Why Blend This Seam cannot run right now, in a few words, or None."""
    obj = context.active_object
    if not is_editable_mesh(obj):
        return 'Enter Edit Mode on a mesh'
    if not obj.data.total_edge_sel:
        return 'Select the edges where two tiles meet'
    return None


class MESH_OT_sprotile_blend_seam(bpy.types.Operator):
    """Fade the tiles on both sides of the selected edges into each other. Each face keeps its own tile and \
blends towards the tile across the seam; Feather sets how far the fade reaches"""
    bl_idname = 'mesh.sprotile_blend_seam'
    bl_label = 'Blend This Seam'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        reason = seam_blocker(context)
        if reason:
            cls.poll_message_set(reason)
        return reason is None

    def execute(self, context):
        try:
            faces, edges, skipped = blend_seam(context, context.active_object)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        if not faces:
            self.report({'WARNING'}, 'No face beside the seam could be blended (their material must show the atlas '
                                     'of the tile across the seam)')
            return {'CANCELLED'}
        text = (f'Blended {faces} face{"s" if faces != 1 else ""} along {edges} seam edge{"s" if edges != 1 else ""}')
        if skipped:
            text += f'; {skipped} left out (another atlas, or no area)'
        self.report({'INFO'}, text)
        return {'FINISHED'}


from .draw import tag_redraw_view3d
from .tiles import atlas_node_from_material, get_uv_inset, image_size, is_editable_mesh, tile_dimensions
from .picker import pipette_tile_grid, pipette_uv_layer
from .blend_settings import BLEND_TILE_FIELDS
from .blend_shared import apply_shared_texture_blend, rollback_shared_blends
from .blend_storage import upgrade_file_blends
from .path_brush import border_settings, path_layer, usable_face_test
