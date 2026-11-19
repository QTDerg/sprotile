"""Turn Textures: turning and flipping A and B on placed blends and placed tiles.
"""

import bpy
import bmesh
import math
from collections import defaultdict
from bpy.props import IntProperty, EnumProperty


# ---------------------------------------------------------------------------
# Turning texture A or B on its own
#
# Every blend corner keeps its patch coordinate in the mask map: 0-1 across the
# patch, u to the right and v up on an upright wall. Textures A and B are each
# an affine image of that coordinate. Turning one texture composes its mapping
# with a quarter turn or mirror of the patch coordinate, inside the same atlas
# region, so the transition, the other texture and Tint lighting stay put.
# ---------------------------------------------------------------------------

TEXTURE_TURNS = (
    ('CCW', "Turn Left", "turn {} 90 degrees counter-clockwise"),
    ('CW', "Turn Right", "turn {} 90 degrees clockwise"),
    ('FLIP_U', "Flip U", "mirror {} left to right"),
    ('FLIP_V', "Flip V", "mirror {} top to bottom"),
)
def sentence(text):
    """Uppercase the first letter only; str.capitalize() would lowercase 'A'."""
    return text[:1].upper() + text[1:]


TEXTURE_TURN_ITEMS = [(key, name, sentence(description.format('the texture')))
                      for key, name, description in TEXTURE_TURNS]


def patch_turn(action):
    """Patch-space map for one turn. Clockwise is as seen on the surface:
    sampling the old layout at the 90 degree turned coordinate turns the
    visible texture clockwise, the same way R turns a painted tile."""
    if action == 'CW':
        return lambda u, v: apply_tile_transform(u, v, 90, False, False)
    if action == 'CCW':
        return lambda u, v: apply_tile_transform(u, v, 270, False, False)
    if action == 'FLIP_U':
        return lambda u, v: (1.0 - u, v)
    if action == 'FLIP_V':
        return lambda u, v: (u, 1.0 - v)
    raise ValueError('Unknown texture turn')


def turned_sample_uv(coefficients, action):
    """Affine sample after a turn; it covers exactly the same atlas region."""
    turn = patch_turn(action)
    points = [affine_uv(coefficients, turn(*uv)) for uv in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))]
    result = tuple(value for axis in (0, 1) for value in
                   (points[1][axis] - points[0][axis], points[2][axis] - points[0][axis], points[0][axis]))
    sample_uv_bounds(result)
    return result


def turn_tile_record(record, action):
    """Turn a captured A/B choice so its thumbnail and next Apply turn together.

    Captured tiles keep rotation and flips as fields, so Keep Proportions can
    still refit a turned tile. A mirror of the surface reverses the rotation:
    painting rotates and then flips, and R(t) followed by a mirror equals the
    mirror followed by R(-t).
    """
    if record.is_sampled:
        record.sample_uv = turned_sample_uv(tuple(record.sample_uv), action)
        return
    rotation = int(record.rotation) % 360
    if action == 'CW':
        record.rotation = (rotation + 90) % 360
    elif action == 'CCW':
        record.rotation = (rotation + 270) % 360
    else:
        record.rotation = (-rotation) % 360
        if action == 'FLIP_U':
            record.flip_u = not record.flip_u
        else:
            record.flip_v = not record.flip_v


def selected_blend_patches(obj, bm):
    """Every placed blend patch with a selected visible face, as face lists.

    A shared patch is all faces with its patch ID, hidden ones included, so it
    stays one consistent layout. A separate-material patch is an edge-connected
    group of faces using that blend material, as Copy This Blend reads it.
    """
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    shared_ids, separate = set(), defaultdict(set)
    for face in bm.faces:
        if not face.select or face.hide or face.material_index >= len(obj.material_slots):
            continue
        material = obj.material_slots[face.material_index].material
        if material is None:
            continue
        if material.get('sprotile_blend_material'):
            separate[face.material_index].add(face)
        elif material.get(SHARED_MATERIAL_TAG) and membership is not None and face[membership] > 0:
            shared_ids.add(face[membership])
    groups = []
    if shared_ids:
        by_id = defaultdict(list)
        for face in bm.faces:
            if face[membership] in shared_ids:
                by_id[face[membership]].append(face)
        groups.extend(by_id.values())
    for slot, chosen in separate.items():
        eligible = [face for face in bm.faces if face.material_index == slot]
        groups.extend(patch for patch in connected_face_patches(eligible)
                      if any(face in chosen for face in patch))
    return groups


def blend_patch_image(obj, bm, face, slot):
    """The image texture A or B of a placed patch samples, or None if unknown."""
    if face.material_index >= len(obj.material_slots):
        return None
    material = obj.material_slots[face.material_index].material
    if material is None:
        return None
    role = 'texture_a' if slot == 'A' else 'texture_b'
    if material.get('sprotile_blend_material'):
        return getattr(blend_nodes(material).get(role), 'image', None)
    flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
    if slot == 'B' and flags is not None and face[flags] & OVERLAY_FLAG:
        return None  # A whole overlay PNG keeps its own region.
    return getattr(shared_nodes(material).get(role), 'image', None)


def surface_scale(faces, mask, matrix_world):
    """World length of one patch unit along mask u and v, area weighted."""
    linear = matrix_world.to_3x3()
    total = along_u = along_v = 0.0
    for face in faces:
        loops = list(face.loops)
        p0, m0 = linear @ loops[0].vert.co, loops[0][mask].xy
        for first, second in zip(loops[1:-1], loops[2:]):
            e1u, e1v = first[mask].x - m0.x, first[mask].y - m0.y
            e2u, e2v = second[mask].x - m0.x, second[mask].y - m0.y
            det = e1u * e2v - e2u * e1v
            if abs(det) < 1e-12:
                continue
            d1, d2 = linear @ first.vert.co - p0, linear @ second.vert.co - p0
            # Columns of the surface Jacobian d(position)/d(patch u, v).
            along_u += ((d1 * e2v - d2 * e1v) / det).length * abs(det)
            along_v += ((d2 * e1u - d1 * e2u) / det).length * abs(det)
            total += abs(det)
    if total <= 0.0 or along_u <= 0.0 or along_v <= 0.0:
        return None
    return along_u / total, along_v / total


def proportional_quarter_turn(coefficients, action, faces, mask, matrix_world, image, scene):
    """A quarter turn that keeps texels square, or None for a plain turn.

    Turning inside the old atlas rectangle is exact when that rectangle is
    square in pixels. A Keep Proportions texture on a wide wall uses a wide
    rectangle, and turning inside it would squash the texels, so refit the
    turned texture into its tile as painting a turned tile does. Stretched
    Fill textures, and regions not inside one tile of the current grid, keep
    their rectangle.
    """
    width, height = image_size(image) if image is not None else (0, 0)
    if min(width, height) <= 0:
        return None
    u0, v0, du, dv = sample_uv_bounds(coefficients)
    w_px, h_px = du * width, dv * height
    if abs(w_px - h_px) <= 1e-4 * max(w_px, h_px):
        return None
    scale = surface_scale(faces, mask, matrix_world)
    if scale is None:
        return None
    a, b, _c, d, e, _f = coefficients
    density_u = math.hypot(a * width, d * height) / scale[0]
    density_v = math.hypot(b * width, e * height) / scale[1]
    if abs(density_u - density_v) > 0.02 * max(density_u, density_v):
        return None
    tile_w, tile_h = get_tile_dims(scene)
    if tile_w > width or tile_h > height:
        return None
    x0, y0, x1, y1 = u0 * width, v0 * height, (u0 + du) * width, (v0 + dv) * height
    col = int(((x0 + x1) / 2) // tile_w)
    row = int((height - (y0 + y1) / 2) // tile_h)
    if not (0 <= col < width // tile_w and 0 <= row < height // tile_h):
        return None
    cell_u, cell_v, cell_du, cell_dv = tile_uv_bounds(col, row, tile_w, tile_h, width, height)
    cx0, cy0 = cell_u * width, cell_v * height
    cx1, cy1 = cx0 + cell_du * width, cy0 + cell_dv * height
    if x0 < cx0 - 1e-3 or y0 < cy0 - 1e-3 or x1 > cx1 + 1e-3 or y1 > cy1 + 1e-3:
        return None
    # The rectangle touches its inset on at least one side; keep that inset.
    margin = max(0.0, min(x0 - cx0, cx1 - x1, y0 - cy0, cy1 - y1))
    room_w, room_h = (cx1 - cx0) - 2 * margin, (cy1 - cy0) - 2 * margin
    if min(room_w, room_h) <= 0.0:
        return None
    fit = min(room_w / h_px, room_h / w_px)
    sx, sy = h_px * fit / w_px, w_px * fit / h_px
    centre_u, centre_v = u0 + du / 2, v0 + dv / 2
    ta, tb, tc, td, te, tf = turned_sample_uv(coefficients, action)
    result = (ta * sx, tb * sx, centre_u + (tc - centre_u) * sx,
              td * sy, te * sy, centre_v + (tf - centre_v) * sy)
    sample_uv_bounds(result)
    return result


def turn_blend_patches(context, slot, action):
    """Turn texture A or B on every selected placed blend; returns the patch count.

    Every patch is read and validated before the first UV changes, so one
    hand-edited patch cancels the whole operation without partial edits.
    """
    plans, meshes, visited = [], {}, set()
    upgrade_file_blends()
    for obj in edit_mesh_objects(context):
        mesh = obj.data
        if mesh.as_pointer() in visited:
            continue
        visited.add(mesh.as_pointer())
        bm = bmesh.from_edit_mesh(mesh)
        # Every check that can refuse comes BEFORE the separation below, which writes patch ids and records that a
        # CANCELLED operator would otherwise leave behind without an Undo step.
        if not selected_blend_patches(obj, bm):
            continue
        if mesh.library is not None:
            raise ValueError("Make linked library mesh data editable before turning its blend")
        stored = edit_blend_layers(mesh, bm)
        layer, mask = (stored.a if slot == 'A' else stored.b), stored.mask
        if layer is None or mask is None:
            raise ValueError('Blend UV maps are missing; reapply the blend before turning a texture')
        # A duplicated or joined copy shares its patch id: turn only the copy that's selected. Separating copies is a
        # valid state on its own, so if the turn below still refuses, the copies simply keep their own ids.
        separate_selected_blend_copies(mesh, bm)
        groups = selected_blend_patches(obj, bm)
        stored = edit_blend_layers(mesh, bm)
        layer, mask = (stored.a if slot == 'A' else stored.b), stored.mask
        # Hold every BMesh wrapper until the writes: freeing one invalidates
        # the loop wrappers taken from it, even though the mesh still exists.
        meshes[mesh.as_pointer()] = (mesh, bm)
        for faces in groups:
            loops = [loop for face in faces for loop in face.loops]
            per_face = patch_tiles_per_face(obj, bm, faces)
            domain = tile_domain(faces, mask, per_face)
            coefficients = sampled_uv_transforms(loops, mask, [layer], domain=domain)[0]
            turned = None
            if action in {'CW', 'CCW'} and not per_face:   # Per Face fills each face's tile, as painting does
                turned = proportional_quarter_turn(coefficients, action, faces, mask, obj.matrix_world,
                                                   blend_patch_image(obj, bm, faces[0], slot), context.scene)
            plans.append((loops, layer, domain, turned or turned_sample_uv(coefficients, action)))
    for loops, layer, domain, coefficients in plans:
        for loop in loops:
            loop[layer].uv = affine_uv(coefficients, domain[loop])
    for mesh, _bm in meshes.values():
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    if plans:
        tag_redraw_view3d()
    return len(plans)


def texture_turn_text(slot, action):
    description = next(item[2] for item in TEXTURE_TURNS if item[0] == action)
    return description.format('texture ' + slot)


def plan_placed_tile_turn(context, steps):
    """Preflight the whole selection before changing any loop on any mesh."""
    tile_size = get_tile_dims(context.scene)
    plans = {}
    has_blends = False
    seen = set()
    for obj in mapping_edit_mesh_objects(context):
        mesh = obj.data
        if mesh in seen:
            continue
        seen.add(mesh)
        bm = bmesh.from_edit_mesh(mesh)
        layer = bm.loops.layers.uv.active
        membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
        factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
        sizes = {}
        writes = []
        for face in bm.faces:
            if face.hide or not face.select:
                continue
            if mesh.library:
                raise ValueError('Make linked library mesh data editable before turning tiles.')
            slot = face.material_index
            mat = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
            if mat and (mat.get('sprotile_blend_material') or
                        (mat.get(SHARED_MATERIAL_TAG) and membership and face[membership] > 0)):
                has_blends = True
                continue
            if mat and mat.get(SHARED_MATERIAL_TAG) and factor and any(loop[factor].uv.y for loop in face.loops):
                raise ValueError('A blend is missing its patch record; reapply its recipe before turning it.')
            if not layer or layer.name in INTERNAL_BLEND_UVS:
                raise ValueError('Activate the original tile UV map before turning placed tiles.')
            if slot not in sizes:
                sizes[slot] = image_size(atlas_for_material_index(obj, slot))
                if min(sizes[slot]) <= 0:
                    raise ValueError('Each selected face needs a material with a loaded atlas.')
                validate_plain_material_uv(obj, bm, mat)
            uvs = [loop[layer].uv.copy() for loop in face.loops]
            cell = tile_cell_from_uvs(uvs, *tile_size, *sizes[slot])
            x, y, w, h = tile_uv_bounds(*cell, *tile_size, *sizes[slot])
            local = [(min(1., max(0., (uv.x - x) / w)), min(1., max(0., (uv.y - y) / h))) for uv in uvs]
            # Mirrored UVs reverse handedness. Keep the visible turn consistent.
            area2 = sum(a*d - b*c for (a, b), (c, d) in zip(local, local[1:] + local[:1]))
            if area2 == 0:
                raise ValueError('A selected face has collapsed UVs; map that face before turning it.')
            angle = 90 * (steps if area2 > 0 else -steps)
            for loop, uv in zip(face.loops, local):
                u, v = apply_tile_transform(*uv, angle, False, False)
                writes.append((loop, (x+u*w, y+v*h)))
        if writes:
            plans[mesh] = (bm, layer, writes)
    if plans and has_blends:
        raise ValueError('Select plain tiles or blends separately to turn them.')
    return plans, has_blends


class MESH_OT_sprotile_turn_placed_tile(bpy.types.Operator):
    """Turn the tiles on selected faces in place; choose A or B for a blend"""
    bl_idname = 'mesh.sprotile_turn_placed_tile'
    bl_label = 'Turn Selected Tiles'
    bl_options = {'UNDO'}
    steps: IntProperty(name='Quarter Turn', default=1, min=-1, max=1, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and is_editable_mesh(context.active_object)

    def execute(self, context):
        steps = -1 if self.steps < 0 else 1
        try:
            plans, has_blends = plan_placed_tile_turn(context, steps)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        if has_blends:
            action = 'CW' if steps > 0 else 'CCW'
            def draw(menu, _context):
                for slot, label in (('A', 'A   First texture'), ('B', 'B   Second texture / overlay')):
                    op = menu.layout.operator('mesh.sprotile_turn_blend_texture', text=label)
                    op.slot, op.action = slot, action
            title = 'Turn blend layer ' + ('right' if steps > 0 else 'left')
            context.window_manager.popup_menu(draw, title=title)
            # The chosen layer's operator owns the edit and its undo step.
            return {'CANCELLED'}
        if not plans:
            self.report({'WARNING'}, 'Select the tiled faces to turn.')
            return {'CANCELLED'}
        for mesh, (_bm, layer, writes) in plans.items():
            for loop, uv in writes:
                loop[layer].uv = uv
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_turn_blend_tile(bpy.types.Operator):
    """Turn or mirror captured texture A or B before Apply"""
    bl_idname = 'mesh.sprotile_turn_blend_tile'
    bl_label = 'Turn Captured Texture'
    bl_options = {'UNDO'}
    slot: EnumProperty(items=[('A', 'A', 'Texture A'), ('B', 'B', 'Texture B')], default='A',
                       options={'SKIP_SAVE'})
    action: EnumProperty(items=TEXTURE_TURN_ITEMS, default='CW', options={'SKIP_SAVE'})

    @classmethod
    def description(cls, _context, properties):
        return (sentence(texture_turn_text(properties.slot, properties.action))
                + ' for the next Apply; placed blends are unchanged')

    def execute(self, context):
        scene = context.scene
        record = getattr(scene, 'sprotile_blend_' + self.slot.lower())
        try:
            validated_blend_tile(record)
            turn_tile_record(record, self.action)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        invalidate_clipboard_slot(scene, self.slot.lower())
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_turn_blend_texture(bpy.types.Operator):
    """Turn or mirror texture A or B on the selected placed blends; the transition stays in place"""
    bl_idname = 'mesh.sprotile_turn_blend_texture'
    bl_label = 'Turn Blend Texture'
    # REGISTER keeps F9 from redoing an older step over this edit. Hidden
    # properties mean no redo panel opens over the atlas picker.
    bl_options = {'REGISTER', 'UNDO'}
    slot: EnumProperty(items=[('A', 'A', 'Texture A'), ('B', 'B', 'Texture B')], default='A',
                       options={'HIDDEN', 'SKIP_SAVE'})
    action: EnumProperty(items=TEXTURE_TURN_ITEMS, default='CW', options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    @classmethod
    def description(cls, _context, properties):
        return (sentence(texture_turn_text(properties.slot, properties.action))
                + ' on every selected blend; the other texture and the transition stay')

    def execute(self, context):
        try:
            count = turn_blend_patches(context, self.slot, self.action)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        if not count:
            self.report({'WARNING'}, 'Select faces of a placed blend first')
            return {'CANCELLED'}
        self.report({'INFO'}, sentence(texture_turn_text(self.slot, self.action))
                    + f' on {count} blend' + ('s' if count != 1 else ''))
        return {'FINISHED'}


def draw_turn_buttons(layout, operator, slot, stacked=False):
    """Turn buttons, then the two mirrors on the same row or the next one."""
    row = layout.row(align=True)
    for action, icon in (('CCW', 'LOOP_BACK'), ('CW', 'LOOP_FORWARDS')):
        op = row.operator(operator, text='', icon=icon)
        op.slot, op.action = slot, action
    if stacked:
        row = layout.row(align=True)
    for action, text in (('FLIP_U', 'Flip U'), ('FLIP_V', 'Flip V')):
        op = row.operator(operator, text=text)
        op.slot, op.action = slot, action


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .draw import tag_redraw_view3d
from .tiles import (
    apply_tile_transform, atlas_for_material_index, edit_mesh_objects, get_tile_dims, image_size,
    is_editable_mesh, mapping_edit_mesh_objects, tile_uv_bounds,
)
from .mapping import connected_face_patches, validate_plain_material_uv
from .picker import tile_cell_from_uvs
from .blend_settings import (
    INTERNAL_BLEND_UVS, OVERLAY_FLAG, SHARED_EDGE_UV, SHARED_FACTOR_UV, SHARED_MATERIAL_TAG, SHARED_PATCH_FLAGS,
    SHARED_PATCH_ID, blend_nodes,
)
from .blend_plan import patch_tiles_per_face, tile_domain, validated_blend_tile
from .blend_shader import shared_nodes
from .blend_recipes import affine_uv, sample_uv_bounds, sampled_uv_transforms
from .blend_ops import invalidate_clipboard_slot
from .blend_storage import edit_blend_layers, upgrade_file_blends
from .blend_shared import separate_selected_blend_copies
