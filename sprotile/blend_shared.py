"""Shared-material patches: patch records, live edits in Edit Active, Stretch / Per Face switching and
Apply.
"""

import bpy
import bmesh
import math
import struct
from collections import Counter, defaultdict
from bpy.props import IntProperty, FloatProperty, EnumProperty, BoolProperty
# Needed while this module loads (class bodies).
from .blend_settings import BLEND_DIRECTIONS, EDGE_STYLES, TILE_MODES
from . import state


def shared_patch_face(mesh, patch_id, faces, membership):
    key = (mesh.as_pointer(), getattr(mesh, 'session_uid', 0), patch_id, mesh.is_editmode)
    hint = _shared_patch_hints.get(key, -1)
    if mesh.is_editmode:
        faces.ensure_lookup_table()
        value = lambda face: face[membership]
    else:
        value = lambda face: membership.data[face.index].value
    if 0 <= hint < len(faces) and value(faces[hint]) == patch_id:
        return faces[hint]
    for index, face in enumerate(faces):
        if value(face) == patch_id:
            if len(_shared_patch_hints) >= SHARED_HINT_LIMIT:
                _shared_patch_hints.clear()
            _shared_patch_hints[key] = index
            return face
    _shared_patch_hints.pop(key, None)
    return None


def update_shared_patch(controls, _context=None, *, change=None):
    """Only this patch's factor UVs change; Tint and authored/sample UVs stay intact."""
    mesh = controls.id_data
    if not isinstance(mesh, bpy.types.Mesh) or controls.patch_id <= 0:
        return
    values = {name: getattr(controls, name) for name in BLEND_CONTROL_FIELDS}
    if change is not None:
        field, value = change
        values[field] = (BLEND_DIRECTIONS[value][0] if field == 'direction' else EDGE_STYLES[value][0] if field == 'edge_style'
                         else TILE_MODES[value][0] if field == 'tiles' else value)
    settings = BlendSettings(**values)
    changed = False

    def metadata(previous):
        if change[0] == 'direction':
            return (int(previous) & ~DIRECTION_BITS) | blend_direction_flag(int(change[1]))
        if change[0] == 'smooth':
            return (int(previous) & ~4) | (4 if change[1] else 0)
        if change[0] == 'edge_style':
            return (int(previous) & (15 | PATH_FLAG | TILE_FACE_FLAG | PAINTED_FLAG)) | (16 if change[1]==1 else 32 if change[1]==2 else 0)
        if change[0] == 'path':
            return (int(previous) & ~PATH_FLAG) | (PATH_FLAG if change[1] else 0)
        if change[0] == 'tiles':
            return (int(previous) & ~TILE_FACE_FLAG) | (TILE_FACE_FLAG if change[1] == 1 else 0)
        return change[1]

    coordinate = shared_factor_mapper(settings)
    edge_coordinate=shared_edge_mapper(settings)
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
        stored = edit_blend_layers(mesh, bm)
        mask, factor, edge = stored.mask, stored.factor, stored.edge
        if patch is None or mask is None or factor is None:
            return
        data = None
        if change is not None and change[0] != 'opacity':
            field = change[0]
            data = (bm.faces.layers.float if field in SHARED_PATCH_FLOATS else bm.faces.layers.int).get(
                SHARED_PATCH_FLOATS.get(field, SHARED_PATCH_FLAGS))
            if data is None and field in LATE_PATCH_FLOATS:
                data = new_late_float_layer(bm, field)
            if data is None:
                return
        for face in bm.faces:
            if face[patch] == controls.patch_id:
                changed = True
                if data is not None:
                    face[data] = metadata(face[data])
                for loop in face.loops:
                    if settings.edge_style == 'OVERLAY':
                        loop[factor].uv.x = settings.opacity / 100
                    else:
                        loop[factor].uv = coordinate(loop[mask].xy)
                    if edge is not None:loop[edge].uv=edge_coordinate(loop[mask].xy)
        if changed:
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        patch = mesh.attributes.get(SHARED_PATCH_ID)
        stored = object_blend_layers(mesh)
        mask, factor, edge = stored.mask, stored.factor, stored.edge
        if patch is None or mask is None or factor is None:
            return
        data = mesh.attributes.get(SHARED_PATCH_FLOATS.get(change[0], SHARED_PATCH_FLAGS)) if change and change[0] != 'opacity' else None
        if change and change[0] in LATE_PATCH_FLOATS and data is None:
            name = SHARED_PATCH_FLOATS[change[0]]
            new_late_float_attribute(mesh, change[0])
            # Blender 4.x reallocates attribute storage when one is added, which
            # leaves earlier attribute and UV references empty. Look them up again.
            data = mesh.attributes.get(name)
            patch = mesh.attributes.get(SHARED_PATCH_ID)
            stored = object_blend_layers(mesh)
            mask, factor, edge = stored.mask, stored.factor, stored.edge
            if patch is None or mask is None or factor is None:
                return
        if change is not None and change[0] != 'opacity' and data is None:
            return
        for face in mesh.polygons:
            if patch.data[face.index].value == controls.patch_id:
                changed = True
                if data is not None:
                    entry = data.data[face.index]
                    entry.value = metadata(entry.value)
                for index in face.loop_indices:
                    if settings.edge_style == 'OVERLAY':
                        factor.data[index].uv.x = settings.opacity / 100
                    else:
                        factor.data[index].uv = coordinate(mask.data[index].vector.xy)
                    if edge is not None:edge.data[index].uv=edge_coordinate(mask.data[index].vector.xy)
        if changed:
            mesh.update()
    if changed:
        tag_redraw_view3d()


def patch_alpha_strength(controls):
    """The alpha stage strength of the material under a shared patch's first face, or 0."""
    mesh = controls.id_data
    if not isinstance(mesh, bpy.types.Mesh) or controls.patch_id <= 0:
        return 0.0
    try:
        if mesh.is_editmode:
            bm = bmesh.from_edit_mesh(mesh)
            patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
            face = shared_patch_face(mesh, controls.patch_id, bm.faces, patch) if patch is not None else None
            index = face.material_index if face is not None else None
        else:
            patch = mesh.attributes.get(SHARED_PATCH_ID)
            index = next((f.material_index for f, p in zip(mesh.polygons, patch.data) if p.value == controls.patch_id), None) if patch else None
        if index is None or index >= len(mesh.materials):
            return 0.0
        return shared_alpha_strength(mesh.materials[index])
    except (ReferenceError, AttributeError, ValueError):
        return 0.0


def new_late_float_layer(bm, field):
    """Create a patch float layer that older files lack, filled with the field's default on EVERY face, so patches
    that never stored it keep reading the value they always had (a new layer is zero otherwise)."""
    layer = bm.faces.layers.float.new(SHARED_PATCH_FLOATS[field])
    default = LATE_PATCH_FLOATS.get(field, 0.0)
    if default:
        for face in bm.faces:
            face[layer] = default
    return layer


def new_late_float_attribute(mesh, field):
    """Object Mode twin of new_late_float_layer. Returns nothing: callers must look every attribute up again."""
    name = SHARED_PATCH_FLOATS[field]
    mesh.attributes.new(name, 'FLOAT', 'FACE')
    default = LATE_PATCH_FLOATS.get(field, 0.0)
    if default:
        mesh.attributes[name].data.foreach_set('value', [default] * len(mesh.polygons))


def shared_patch_property(controls, field, default):
    """Face data participates in Blender's mesh Undo; collection ID props alone do not."""
    mesh = controls.id_data
    if controls.patch_id > 0:
        name = SHARED_PATCH_FLOATS.get(field, SHARED_PATCH_FLAGS)
        if mesh.is_editmode:
            bm = bmesh.from_edit_mesh(mesh)
            patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
            data = (bm.faces.layers.float if field in SHARED_PATCH_FLOATS else bm.faces.layers.int).get(name)
            face = shared_patch_face(mesh, controls.patch_id, bm.faces, patch) if patch is not None else None
            value = face[data] if face is not None and data is not None else None
        else:
            patch, data = mesh.attributes.get(SHARED_PATCH_ID), mesh.attributes.get(name)
            face = shared_patch_face(mesh, controls.patch_id, mesh.polygons, patch) if patch is not None else None
            value = data.data[face.index].value if face is not None and data is not None else None
        if value is not None:
            if field=='edge_style':return 3 if int(value)&OVERLAY_FLAG else 2 if int(value)&32 else 1 if int(value)&16 else 0
            if field == 'path':return bool(int(value) & PATH_FLAG) and not int(value) & OVERLAY_FLAG
            if field == 'tiles':return 1 if int(value) & TILE_FACE_FLAG else 0
            if field == 'opacity':
                if not int(value) & OVERLAY_FLAG:
                    return 100.0
                if mesh.is_editmode:
                    factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
                    return face.loops[0][factor].uv.x * 100 if factor else default
                factor = mesh.uv_layers.get(SHARED_FACTOR_UV)
                return factor.data[face.loop_start].uv.x * 100 if factor else default
            if field=='edge_frequency' and value<1:return 8.
            return decode_direction_flag(value) if field == 'direction' else (bool(int(value) & 4) if field == 'smooth' else value)
        if field in LATE_PATCH_FLOATS and face is not None:
            return LATE_PATCH_FLOATS[field]  # Old files and Undo before the layer was created.
    return controls.get(field, default)


def patch_materials(controls):
    """The shared materials a patch's faces use."""
    mesh = controls.id_data
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh); membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
        slots = {f.material_index for f in bm.faces if membership is not None and f[membership] == controls.patch_id}
    else:
        membership = mesh.attributes.get(SHARED_PATCH_ID)
        slots = {f.material_index for f in mesh.polygons
                 if membership is not None and membership.data[f.index].value == controls.patch_id}
    materials = []
    for slot in slots:
        if slot >= len(mesh.materials) or mesh.materials[slot] is None:
            raise ValueError('The patch material is missing; reapply before changing this setting')
        materials.append(mesh.materials[slot])
    return materials


def shared_overlay_image(material, flags):
    """Read the image from the placed overlay's own bank slot."""
    code = FIRST_OVERLAY_CODE + ((flags >> 7) & (OVERLAY_INDEX_LIMIT - 1))
    sampler = shared_nodes(material).get(f'overlay_{code}_alpha')
    return getattr(sampler, 'image', None)


def shared_overlay_cell_size(mesh, faces, bm=None):
    """Read the stored B grid only when all corners have proven, matching metadata."""
    required = OVERLAY_FLAG | OVERLAY_CELL_SIZE_FLAG
    if bm is not None:
        flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
        stored = edit_blend_layers(mesh, bm)
        if flags is None or any(face[flags] & required != required for face in faces):
            return 0
        read_z = lambda corner, layer: corner[layer].z
    else:
        flags = mesh.attributes.get(SHARED_PATCH_FLAGS)
        stored = object_blend_layers(mesh)
        if (flags is None or (flags.domain, flags.data_type) != ('FACE', 'INT')
                or any(flags.data[face.index].value & required != required for face in faces)):
            return 0
        read_z = lambda corner, layer: corner[layer].vector.z
    if stored.base is None or stored.mask is None:
        return 0
    halves = {(read_z(loop, stored.base), read_z(loop, stored.mask))
              for face in faces for loop in face.loops}
    if len(halves) != 1:
        return 0
    low, high = halves.pop()
    if 0 <= low <= 0xffff and 0 <= high <= 0x7fff and low.is_integer() and high.is_integer():
        return int(low) | (int(high) << 16)
    return 0


def fill_tile_cell(coefficients, image, scene, *, tile_size=None):
    """Grow a tile region that Keep Proportions cropped back to its whole tile, keeping turns and inset.

    Per Face fills every face's tile, as Apply does. Returns None when the region already fills its cell or
    is not inside one cell of the current tile grid (an overlay PNG, or another tile size).
    """
    width, height = image_size(image) if image is not None else (0, 0)
    if min(width, height) <= 0:
        return None
    u0, v0, du, dv = sample_uv_bounds(coefficients)
    tile_w, tile_h = tile_size or get_tile_dims(scene)
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
    margin = max(0.0, min(x0 - cx0, cx1 - x1, y0 - cy0, cy1 - y1))   # the inset it already keeps
    room_u, room_v = (cx0 + margin) / width, (cy0 + margin) / height
    room_du, room_dv = (cx1 - cx0 - 2 * margin) / width, (cy1 - cy0 - 2 * margin) / height
    if min(room_du, room_dv) <= 0.0 or (abs(room_du - du) <= 1e-7 and abs(room_dv - dv) <= 1e-7):
        return None
    sx, sy = room_du / du, room_dv / dv
    a, b, c, d, e, f = coefficients
    result = (a * sx, b * sx, room_u + (c - u0) * sx, d * sy, e * sy, room_v + (f - v0) * sy)
    sample_uv_bounds(result)
    return result


def retile_loops(groups, mask, layers, per_face, images=None, scene=None, overlay_cell_size=0):
    """Switch placed patches between Stretch and Per Face without changing their tiles or turns.

    Each group is (faces, currently per face). Every group is read and checked before the first write, so a
    hand-edited patch cancels the switch with nothing changed. With images (one per layer, None where
    unknown) and a scene, a tile that Keep Proportions cropped grows back to its whole tile for Per Face.
    """
    plans = []
    for faces, current in groups:
        loops = [loop for face in faces for loop in face.loops]
        transforms = sampled_uv_transforms(loops, mask, layers, domain=tile_domain(faces, mask, current))
        if per_face and not current and images is not None and scene is not None:
            a, b = transforms
            image_a, image_b = images
            tile_size = (overlay_cell_size, overlay_cell_size) if overlay_cell_size else None
            transforms = [fill_tile_cell(a, image_a, scene) or a,
                          fill_tile_cell(b, image_b, scene, tile_size=tile_size) or b]
        plans.append((loops, transforms, tile_domain(faces, mask, per_face)))
    for loops, transforms, domain in plans:
        for loop in loops:
            for layer, coefficients in zip(layers, transforms):
                loop[layer].uv = affine_uv(coefficients, domain[loop])


class _MeshCorner:
    """A face corner of an Object Mode mesh that reads like a BMLoop: corner[uv_layer].uv."""
    __slots__ = ('index',)

    def __init__(self, index):
        self.index = index

    def __getitem__(self, layer):
        return layer.data[self.index]


class _MeshFace:
    __slots__ = ('loops', 'index')

    def __init__(self, loops, index):
        self.loops = loops
        self.index = index


def retile_shared_patch(controls, per_face):
    """Rewrite one shared patch's A and B UVs for Per Face or Stretch, in place (no mesh reallocation)."""
    mesh = controls.id_data
    bm = None
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
        flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
        stored = edit_blend_layers(mesh, bm)
        layers = [stored.a, stored.b, stored.mask]
        faces = [face for face in bm.faces if membership is not None and face[membership] == controls.patch_id]
        first = (faces[0].material_index, faces[0][flags] if flags is not None else 0) if faces else None
    else:
        membership = mesh.attributes.get(SHARED_PATCH_ID)
        flags = mesh.attributes.get(SHARED_PATCH_FLAGS)
        stored = object_blend_layers(mesh)
        layers = [stored.a, stored.b, stored.mask]
        faces = [_MeshFace([_MeshCorner(index) for index in polygon.loop_indices], polygon.index) for polygon in mesh.polygons
                 if membership is not None and membership.data[polygon.index].value == controls.patch_id]
        first = (mesh.polygons[faces[0].index].material_index,
                 flags.data[faces[0].index].value if flags is not None else 0) if faces else None
    if membership is None or any(layer is None for layer in layers):
        raise ValueError('Blend UV maps are missing; reapply the blend before changing its tiles')
    if not faces:
        return
    # The tile images let a Keep Proportions crop grow back to the whole tile (fill_tile_cell).
    material_index, bits = first
    overlay = bool(bits & OVERLAY_FLAG)
    cell_size = shared_overlay_cell_size(mesh, faces, bm) if overlay else 0
    material = mesh.materials[material_index] if material_index < len(mesh.materials) else None
    images = None
    if material is not None:
        nodes = shared_nodes(material)
        image_b = shared_overlay_image(material, bits) if cell_size else None
        if not overlay:
            image_b = getattr(nodes.get('texture_b'), 'image', None)
        images = (getattr(nodes.get('texture_a'), 'image', None), image_b)
    retile_loops([(faces, not per_face)], layers[2], layers[:2], per_face, images, bpy.context.scene,
                 overlay_cell_size=cell_size)
    if mesh.is_editmode:
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        mesh.update()


def set_shared_patch_property(controls, field, value):
    """Live Edit Blend controls write straight into the mesh. A refusal is remembered for the panel (state.last_control_refusal)."""
    try:
        _set_shared_patch_property(controls, field, value)
    except ValueError as error:
        state.last_control_refusal = str(error)
        raise
    else:
        state.last_control_refusal = None
        keep_undo_in_step()   # a control can change the material (Path, an edge style), not only the mesh


def _set_shared_patch_property(controls, field, value):
    if field in SHARED_PATCH_FLOATS:
        value = struct.unpack('f', struct.pack('f', value))[0]
    elif field == 'opacity':
        # Match the float32 UV representation when checking for a no-op.
        value = struct.unpack('f', struct.pack('f', value / 100))[0] * 100
    if controls.patch_id <= 0:
        controls[field] = value
        return
    if field == 'edge_style' and value == 3:
        if shared_patch_property(controls, field, None) == value:
            return
        raise ValueError('Choose New Blend, select Overlay and Apply to set its image first')
    if field == 'edge_style' and value != 3 and shared_patch_property(controls, field, None) == 3:
        raise ValueError('Changing Overlay to another style needs a new blend; Remove Blend and Apply again')
    if field == 'path' and value:
        if shared_patch_property(controls, 'edge_style', 0) == 3:
            raise ValueError('Overlay uses the alpha of B instead of a path transition')
        for material in patch_materials(controls):
            validate_shared_path(material)
    if field == 'tiles':
        if shared_patch_property(controls, 'tiles', 0) == value:
            return
        retile_shared_patch(controls, value == 1)
    if field=='edge_style' and value in (1,2):
        mesh=controls.id_data
        exists=(bmesh.from_edit_mesh(mesh).loops.layers.uv.get(SHARED_EDGE_UV) is not None if mesh.is_editmode else mesh.uv_layers.get(SHARED_EDGE_UV) is not None)
        if not exists:raise ValueError('Choose New Blend, select an edge style and Apply before editing this patch')
        if mesh.is_editmode:
            bm=bmesh.from_edit_mesh(mesh);membership=bm.faces.layers.int.get(SHARED_PATCH_ID)
            slots={f.material_index for f in bm.faces if membership is not None and f[membership]==controls.patch_id}
        else:
            membership=mesh.attributes.get(SHARED_PATCH_ID)
            slots={f.material_index for f in mesh.polygons if membership is not None and membership.data[f.index].value==controls.patch_id}
        for slot in slots:
            if slot>=len(mesh.materials) or mesh.materials[slot] is None:raise ValueError('The patch material is missing; reapply before changing edge style')
            validate_shared_grass(mesh.materials[slot],require_snow=value==2)
    if shared_patch_property(controls, field, None) == value:
        return  # No UV writes, dependency-graph update or redraw for a no-op.
    controls[field] = value
    update_shared_patch(controls, change=(field, value))


class SprotileBlendPatch(bpy.types.PropertyGroup):
    opacity: FloatProperty(name='Opacity', subtype='PERCENTAGE', min=0, max=100,
        get=lambda self:shared_patch_property(self,'opacity',100.0),
        set=lambda self,value:set_shared_patch_property(self,'opacity',value))
    feather_percent: FloatProperty(name='Feather',description='Total fade width as a percentage of the patch; 2% fades about 1% on each side of the edge',
        subtype='PERCENTAGE',min=0,max=200,get=lambda self:self.width*100,set=lambda self,value:setattr(self,'width',value/100))
    patch_id: IntProperty(default=0)
    # How many separate parts Apply gave this patch: 1 usually, more for Existing UVs over separate faces.
    # 0 in files from before 5.13.2. Separating copies never splits a patch made of several parts.
    parts: IntProperty(default=0)
    direction: EnumProperty(name="Direction", items=BLEND_DIRECTIONS,
        get=lambda self: shared_patch_property(self, 'direction', 3),
        set=lambda self, value: set_shared_patch_property(self, 'direction', value))
    position: FloatProperty(name="Position", min=0.0, max=1.0,
        get=lambda self: shared_patch_property(self, 'position', 0.5),
        set=lambda self, value: set_shared_patch_property(self, 'position', value))
    width: FloatProperty(name="Blend Width", min=0.0, max=2.0,
        get=lambda self: shared_patch_property(self, 'width', 0.35),
        set=lambda self, value: set_shared_patch_property(self, 'width', value))
    smooth: BoolProperty(name="Smooth Feather",
        get=lambda self: shared_patch_property(self, 'smooth', True),
        set=lambda self, value: set_shared_patch_property(self, 'smooth', value))
    angle: FloatProperty(name="Angle", description="Blend direction in patch UVs: 0 degrees right, 90 degrees up",
        subtype='ANGLE', min=0.0, max=math.tau,
        get=lambda self: shared_patch_property(self, 'angle', 0.0),
        set=lambda self, value: set_shared_patch_property(self, 'angle', value))
    edge_style: EnumProperty(name='Style',items=EDGE_STYLES,
        get=lambda self:shared_patch_property(self,'edge_style',0),set=lambda self,value:set_shared_patch_property(self,'edge_style',value))
    edge_height: FloatProperty(name='Tuft Height',min=0,max=.5,
        get=lambda self:shared_patch_property(self,'edge_height',.12),set=lambda self,value:set_shared_patch_property(self,'edge_height',value))
    edge_frequency: FloatProperty(name='Count',min=1,max=64,
        get=lambda self:shared_patch_property(self,'edge_frequency',8.),set=lambda self,value:set_shared_patch_property(self,'edge_frequency',value))
    path: BoolProperty(name='Path',
        get=lambda self: shared_patch_property(self, 'path', False),
        set=lambda self, value: set_shared_patch_property(self, 'path', value))
    path_width: FloatProperty(name='Path Width', min=0., max=1.,
        get=lambda self: shared_patch_property(self, 'path_width', .3),
        set=lambda self, value: set_shared_patch_property(self, 'path_width', value))
    path_width_percent: FloatProperty(name='Path Width', description='Width of the B band as a percentage of the patch',
        subtype='PERCENTAGE', min=0, max=100, get=lambda self: self.path_width*100,
        set=lambda self, value: setattr(self, 'path_width', value/100))
    tiles: EnumProperty(name='Tiles', items=TILE_MODES,
        get=lambda self: shared_patch_property(self, 'tiles', 0),
        set=lambda self, value: set_shared_patch_property(self, 'tiles', value))
    # Painted alpha lives on the material (one stage for every patch), so these only read it.
    alpha_shaping: BoolProperty(name='Painted Alpha', get=lambda self: patch_alpha_strength(self) > 0, set=lambda self, value: None)
    alpha_strength: FloatProperty(name='Strength', min=0., max=8.,
        get=lambda self: patch_alpha_strength(self) or 2.0, set=lambda self, value: None)


def _face_components(faces):
    """Edge-connected groups of `faces`, each starting with its lowest face index."""
    pool, components = set(faces), []
    for seed in sorted(faces, key=lambda face: face.index):
        if seed not in pool:
            continue
        pool.discard(seed)
        component, stack = [seed], [seed]
        while stack:
            for edge in stack.pop().edges:
                for other in edge.link_faces:
                    if other in pool:
                        pool.discard(other)
                        component.append(other)
                        stack.append(other)
        components.append(component)
    return components


def _inside_polygon(x, y, polygon):
    inside = False
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


class _LayoutShapes:
    """Each face's outline in the patch layout (the Mask), with a coarse grid to find neighbours quickly."""
    CELL = 1 / 32

    def __init__(self, faces, mask):
        self.shapes = {}
        for face in faces:
            points = [tuple(corner_uv(loop, mask)) for loop in face.loops]
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            self.shapes[face] = (points, (min(xs), min(ys), max(xs), max(ys)), (sum(xs) / len(xs), sum(ys) / len(ys)))

    def _grid(self, faces):
        grid = defaultdict(list)
        for face in faces:
            x0, y0, x1, y1 = self.shapes[face][1]
            for i in range(math.floor(x0 / self.CELL), math.floor(x1 / self.CELL) + 1):
                for j in range(math.floor(y0 / self.CELL), math.floor(y1 / self.CELL) + 1):
                    grid[i, j].append(face)
        return grid

    def overlap(self, first, second):
        """Whether two face groups cover the same part of the layout: a face centre of one inside a face of the other."""
        for centres, outlines in ((first, second), (second, first)):
            grid = self._grid(outlines)
            for face in centres:
                cx, cy = self.shapes[face][2]
                for other in grid.get((math.floor(cx / self.CELL), math.floor(cy / self.CELL)), ()):
                    points, (x0, y0, x1, y1), _ = self.shapes[other]
                    if x0 <= cx <= x1 and y0 <= cy <= y1 and _inside_polygon(cx, cy, points):
                        return True
        return False


def patch_record(mesh, patch_id):
    return next((item for item in mesh.sprotile_blend_patches if item.patch_id == patch_id), None)


def patch_instances(faces, mask, keep=()):
    """One patch id's faces, split into the copies that share it.

    Duplicating blended faces (Shift+D) or joining two blended objects copies the patch id, so one id can cover
    two copies of a blend, or two different blends. One Apply never puts two separate parts of a patch on the same
    place in the patch layout, so a disconnected group whose layout overlaps the rest is a copy. Groups that don't
    overlap stay together (a patch with faces deleted from its middle). The first list holds `keep`, or else the
    lowest face index.
    """
    components = _face_components(faces)
    if len(components) < 2:
        return [list(faces)]
    shapes = _LayoutShapes(faces, mask)
    keep = set(keep)
    first = next((component for component in components if keep.intersection(component)), components[0])
    instances = []
    for component in [first] + [c for c in components if c is not first]:
        for instance in instances:
            if not shapes.overlap(instance, component):
                instance.extend(component)
                break
        else:
            instances.append(list(component))
    return instances


def blend_copies(mesh, bm, patch_id, keep=(), faces=None, record=False):
    """The copies of one shared patch in Edit Mode, the one holding `keep` first; one list when it has none.

    Existing UVs can place one patch in several separate parts (the record's `parts`); those are never split.
    A caller that looks at many patches passes each one's faces and record, found in one pass over the mesh.
    """
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    if membership is None or patch_id <= 0:
        return []
    if faces is None:
        bm.faces.index_update()   # "the lowest face index keeps the id" must use live indices, not stale ones
        faces = [face for face in bm.faces if face[membership] == patch_id]
    mask = edit_blend_layers(mesh, bm).mask
    if record is False:
        record = patch_record(mesh, patch_id)
    if not faces or mask is None or (record is not None and record.parts > 1):
        return [faces] if faces else []
    return patch_instances(faces, mask, keep)


def separate_blend_copies(mesh, bm, patch_ids=None, keep=()):
    """Give every copy of a shared patch its own id and its own row in Blends on This Mesh.

    The copy holding `keep` (or the lowest face index) keeps the id and name; the others become "<name> copy".
    Their settings live on their faces, so nothing else changes. Returns how many copies were separated.
    """
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    if membership is None:
        return 0
    # One pass over the mesh and one over the records, however many patches are asked about (a selected path is
    # hundreds of them): each patch's faces in index order, and the first record with each id.
    bm.faces.index_update()   # "the lowest face index keeps the id" must use live indices, not stale ones
    by_id, largest_face_id = defaultdict(list), 0
    for face in bm.faces:
        identifier = face[membership]
        if identifier > 0:
            by_id[identifier].append(face)
            if identifier > largest_face_id:
                largest_face_id = identifier
    ids = set(patch_ids) if patch_ids is not None else set(by_id)
    ids.discard(0)
    records = {}
    for item in mesh.sprotile_blend_patches:
        records.setdefault(item.patch_id, item)
    largest = max(max(records, default=0), largest_face_id)
    created = []
    # Ctrl+J keeps only the active object's records, so a joined blend arrives with faces but no record; give it one
    # (Edit Blend, the list and Copy Blend all need it). Its original name lived on the other object.
    queued_names = {}
    for patch_id in sorted(ids):
        if patch_id not in records:
            created.append((patch_id, 'Joined blend ' + str(patch_id), 0))
            queued_names[patch_id] = created[-1][1]
    for patch_id in sorted(ids):
        record = records.get(patch_id)
        instances = blend_copies(mesh, bm, patch_id, keep, faces=by_id.get(patch_id, []), record=record)
        if len(instances) < 2:
            continue
        name = record.name if record is not None and record.name else queued_names.get(patch_id, 'Patch ' + str(patch_id))
        parts = record.parts if record is not None else 0
        for instance in instances[1:]:
            largest += 1
            for face in instance:
                face[membership] = largest
            created.append((largest, name + ' copy', parts))
    # Growing the collection can invalidate earlier RNA items, so records are added last.
    for patch_id, name, parts in created:
        item = mesh.sprotile_blend_patches.add()
        item.patch_id, item.name, item.parts = patch_id, name, parts
    if created:
        _shared_patch_hints.clear()
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        tag_redraw_view3d()
    return len(created)


def separate_selected_blend_copies(mesh, bm):
    """Separate the copies of every shared blend with a selected face. The first copy (lowest face index, usually
    the original) keeps the id and name; the others become "<name> copy"."""
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    if membership is None:
        return 0
    selected = {face[membership] for face in bm.faces if face.select and not face.hide and face[membership] > 0}
    return separate_blend_copies(mesh, bm, selected)


def active_shared_patch(context):
    obj = context.active_object
    if not is_editable_mesh(obj):
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    face, patch = bm.faces.active, bm.faces.layers.int.get(SHARED_PATCH_ID)
    if patch is None:
        return None
    if face is None or not face.select or face.hide:
        # Vertex/edge selection often has no active BMFace. A single selected
        # patch is unambiguous and must still expose its live controls.
        face = None
        remaining = getattr(obj.data, 'total_face_sel', -1)   # lets the search stop at the last selected face
        if remaining == 0:
            return None   # nothing selected (while painting, say): no need to look at any face
        for item in bm.faces:
            if item.select and not item.hide:
                if item[patch] == 0 or (face is not None and item[patch] != face[patch]):
                    return None
                if face is None:
                    face = item
                remaining -= 1
                if remaining == 0:
                    break
        if face is None:
            return None
    if face.material_index >= len(obj.material_slots):
        return None
    material = obj.material_slots[face.material_index].material
    if material is None or not material.get(SHARED_MATERIAL_TAG):
        return None
    return next((item for item in obj.data.sprotile_blend_patches if item.patch_id == face[patch]), None)


TOOL_PATCH_NAMES = ('Path', 'Seam', 'Paint area')   # the names the paint tools give their patches


def apply_shared_texture_blend(context, overrides=None, keep_a=False, split_faces=False, patch_settings=None,
                               method=None, fit=None, patch_name='Patch', controls=None, patch_groups=None,
                               faces=None, undo_log=None, preserve_path_marks=False):
    """Apply the New Blend to the selected faces with the shared atlas material.

    controls (default: what Apply uses) supplies the settings and overrides replace some of them; keep_a leaves
    every face's current tile as A (only B, Mask and Factor are written); faces ({mesh pointer: [face index, ...]})
    names the faces to blend instead of the selection, which is then left exactly as it is; split_faces makes each
    face its own patch, patch_groups names the patches exactly; patch_settings(face_indices, masks) may return more
    overrides for one patch, where masks maps each face index to its corners' Mask UVs. The paint tools use these.
    undo_log (a list) collects what a successful call changed, so a caller that makes several calls can take them
    all back with rollback_shared_blends(undo_log) when a later one fails. Path and Seam manage their own face
    markers and set preserve_path_marks; other Apply calls replace the previous tool ownership."""
    explicit = faces
    settings = controls if controls is not None else active_blend_settings(context)
    settings = {name: getattr(settings, name) for name in BLEND_CONTROL_FIELDS}
    settings.update(overrides or {})
    settings = validate_blend_settings(settings)
    tile_b = validated_blend_tile(context.scene.sprotile_blend_b)
    # Keep A never writes A, so a missing or stale A capture does not matter; B stands in for the checks.
    tile_a = tile_b if keep_a else validated_blend_tile(context.scene.sprotile_blend_a)
    overlay = settings['edge_style'] == 'OVERLAY'
    cell_size = overlay_capture_cell_size(tile_b) if overlay else 0
    if not overlay and tile_a['image'] != tile_b['image']:
        raise ValueError("Shared Material blends two tiles from the same atlas; choose Separate Material for different images")
    # Check every selected instance before mesh deduplication, including objects
    # whose linked mesh is visited through a different Data-linked instance.
    edited = edit_mesh_objects(context)
    edited_meshes = {obj.data.as_pointer() for obj in edited}
    instances = list(edited)
    for obj in getattr(context, 'selected_objects', ()):
        if obj.type == 'MESH' and obj.data.as_pointer() in edited_meshes and obj not in instances:
            instances.append(obj)
    for obj in instances:
        bm = bmesh.from_edit_mesh(obj.data)
        if explicit is not None:
            bm.faces.ensure_lookup_table()
            total = len(bm.faces)
            chosen = [bm.faces[index] for index in explicit.get(obj.data.as_pointer(), ()) if 0 <= index < total]
        else:
            chosen = [face for face in bm.faces if face.select]
        slots = obj.material_slots
        for index in {face.material_index for face in chosen if not face.hide}:
            if index < len(slots) and slots[index].link != 'DATA':
                raise ValueError("Shared blending needs Data-linked material slots; switch the slot link or use Separate Material")
    plans = prepare_blend_plans(context, tile_a, tile_b, per_face=settings['tiles'] == 'FACE', split_faces=split_faces,
                                method=method, fit=fit, patch_groups=patch_groups, faces=explicit)
    if not plans:
        return False
    upgrades = {}
    for plan in plans:
        obj, bm = plan['obj'], plan['bm']
        if bm.loops.layers.uv.active is None:
            raise ValueError("Shared Material needs an original UV map; use Separate Material for an untextured mesh")
        if plan['blend_version'] < 2 and bm.loops.layers.uv.get(SHARED_FACTOR_UV):
            raise ValueError("Reserved SprotileBlend_Factor UV map already exists; rename that user map first")
        required_uvs = blend_uv_maps_needed(True, settings['edge_style'] in SHAPED_EDGES)
        missing_uvs = sum(bm.loops.layers.uv.get(name) is None for name in required_uvs)
        if len(bm.loops.layers.uv) + missing_uvs > 8:
            raise ValueError("Shared Material needs "+('three' if settings['edge_style'] in SHAPED_EDGES else 'two')+" blend UV maps; Blender's eight-map limit would be exceeded")
        attribute = obj.data.attributes.get(SHARED_PATCH_ID)
        patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
        if attribute is not None and (patch is None or plan['blend_version'] < 2):
            raise ValueError("Reserved patch-id attribute already exists or has an incompatible type")
        for name in tuple(SHARED_PATCH_FLOATS.values()) + (SHARED_PATCH_FLAGS,):
            collection = bm.faces.layers.int if name == SHARED_PATCH_FLAGS else bm.faces.layers.float
            if obj.data.attributes.get(name) is not None and (collection.get(name) is None or plan['blend_version'] < 2):
                raise ValueError("Reserved blend-settings attribute already exists or has an incompatible type")
        history = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
        plan['slots'] = {}
        for index, _ in plan['faces']:
            face = bm.faces[index]
            slot = face.material_index
            material = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
            if material and material.get('sprotile_blend_material'):
                # Joined meshes can't trust a bare slot number; a material key (5.15.1) survives the join.
                if plan['recovered'] and (history is None or face[history] <= HISTORY_SLOT_MASK):
                    raise ValueError("The original material binding was lost after a mesh edit; assign the source atlas material or use Separate Material")
                if history is None or face[history] <= 0:
                    raise ValueError("This separate blend has no original atlas binding; restore an atlas material first")
                slot = history_slot(obj, face[history])
                if slot < 0:
                    raise ValueError(history_conflict(obj, face[history]) or
                                     "This separate blend's original material is no longer on the object; assign it to a slot first")
                material = obj.material_slots[slot].material
            if slot < len(obj.material_slots) and obj.material_slots[slot].link != 'DATA':
                raise ValueError("Shared blending needs Data-linked material slots; switch the slot link or use Separate Material")
            key = material.as_pointer() if material is not None else None
            if key not in upgrades:
                original = shared_atlas_node(material, tile_a['image'])
                check_atlas_reads_render_map(obj.data, original)
                upgrades[key] = (material, original)
            plan['slots'][index] = slot
    snapshots = [_blend_snapshot(plan) for plan in plans]
    journals, touched, edge_journals = [], [], []
    overlay_codes = {}
    try:
        for material, original in upgrades.values():
            journal = upgrade_shared_material(material, original)
            if journal is not None:
                journals.append(journal)
            if settings['edge_style'] in SHAPED_EDGES:
                edge_journal=ensure_shared_edge_support(material,snow=settings['edge_style']=='SNOW')
                if edge_journal is not None:edge_journals.append(edge_journal)
            if settings['path']:
                path_journal = ensure_shared_path_support(material)
                if path_journal is not None:edge_journals.append(path_journal)
            if settings.get('alpha_shaping'):
                alpha_journal = ensure_shared_alpha_support(material, settings['alpha_strength'])
                if alpha_journal is not None:edge_journals.append(alpha_journal)
            if overlay:
                code, overlay_journal = ensure_shared_overlay(material, tile_b['image'])
                overlay_codes[material.as_pointer()] = code
                if overlay_journal is not None:
                    edge_journals.append(overlay_journal)
        for plan, snapshot in zip(plans, snapshots):
            touched.append((plan, snapshot))
            mesh, bm = plan['obj'].data, plan['bm']
            plan['created'] = ensure_edit_blend_layers(mesh, bm, shared=True, edge=settings['edge_style'] in SHAPED_EDGES)
            if bm.faces.layers.int.get(SHARED_PATCH_ID) is None:
                bm.faces.layers.int.new(SHARED_PATCH_ID)
            for field, name in SHARED_PATCH_FLOATS.items():
                if bm.faces.layers.float.get(name) is None:
                    new_late_float_layer(bm, field)
            if bm.faces.layers.int.get(SHARED_PATCH_FLAGS) is None:
                bm.faces.layers.int.new(SHARED_PATCH_FLAGS)
            stored = edit_blend_layers(mesh, bm)
            layers = [stored.a, stored.b, stored.mask]
            factor, edge = stored.factor, stored.edge
            patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
            path_mark = None if preserve_path_marks else bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE)
            previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
            metadata = {key: bm.faces.layers.float.get(name) for key, name in SHARED_PATCH_FLOATS.items()}
            flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
            bm.faces.ensure_lookup_table()
            # The first record with each id, by position: a collection that grows can invalidate RNA items.
            record_index = {}
            for position, item in enumerate(mesh.sprotile_blend_patches):
                if item.patch_id > 0:
                    record_index.setdefault(item.patch_id, position)
            record_ids = set(record_index)
            counts = Counter([face[patch] for face in bm.faces])
            largest = max(max(record_ids, default=0), max(counts, default=0))
            face_patch_ids, face_values = {}, {}
            masks = {index: coordinates[2] for index, coordinates in plan['faces']}
            for indices in plan['patches']:
                values = settings
                if patch_settings is not None:
                    extra = patch_settings(indices, masks) or {}
                    if {'edge_style', 'path', 'alpha_shaping'} & set(extra):
                        raise ValueError('Internal: per-patch settings cannot change the shader stages')
                    values = validate_blend_settings(dict(settings, **extra))
                face_values.update(dict.fromkeys(indices, values))
                candidate = bm.faces[indices[0]][patch]
                whole = (candidate in record_ids and counts[candidate] == len(indices)
                         and all(bm.faces[index][patch] == candidate for index in indices))
                if not whole and patch_groups is not None:
                    # The paint tools grow a patch a few faces at a time (a Path brush stroke runs over the faces
                    # that blended into it). One of the tool's own patches that lies wholly inside the new one
                    # lends it its id and record, instead of leaving a record without faces behind for every face
                    # painted over. A blend the user applied and named is never taken over this way.
                    inside = Counter(bm.faces[index][patch] for index in indices)
                    swallowed = [identifier for identifier, count in inside.items()
                                 if identifier > 0 and identifier in record_ids and counts[identifier] == count
                                 and mesh.sprotile_blend_patches[record_index[identifier]].name.startswith(patch_name + ' ')]
                    if swallowed:
                        candidate, whole = min(swallowed), True
                patch_id = candidate
                if not whole:
                    controls = mesh.sprotile_blend_patches.add()
                    for name, value in values.items():
                        setattr(controls, name, value)
                    largest += 1
                    patch_id = largest
                    controls.patch_id = patch_id
                    controls.name = patch_name + ' ' + str(patch_id)
                    controls.parts = len(_face_components([bm.faces[index] for index in indices]))
                else:
                    # A complete existing patch reuses its record; its shape may have changed, so refresh `parts`.
                    record = mesh.sprotile_blend_patches[record_index[candidate]]
                    parts = len(_face_components([bm.faces[index] for index in indices]))
                    name = patch_name + ' ' + str(candidate)
                    rename = record.name != name and record.name in {
                        tool + ' ' + str(candidate) for tool in TOOL_PATCH_NAMES}
                    if record.parts != parts or rename:
                        snapshot.setdefault('updated_records', {}).setdefault(
                            record_index[candidate], (record.parts, record.name))
                        record.parts = parts
                        if rename:
                            record.name = name
                    counts[candidate] = len(indices)
                # Keeping old records for partial splits preserves Edit Mode Undo history.
                # Growing a Blender collection can invalidate previous RNA items.
                # Only carry numeric IDs from this loop into the mesh writes.
                face_patch_ids.update(dict.fromkeys(indices, patch_id))
            mappers = {}
            for index, coordinates in plan['faces']:
                face = bm.faces[index]
                values = face_values[index]
                key = id(values)
                if key not in mappers:
                    patch_values = BlendSettings(*(values[name] for name in BLEND_CONTROL_FIELDS))
                    mappers[key] = (patch_values, shared_factor_mapper(patch_values),
                                    shared_edge_mapper(patch_values) if edge is not None else None)
                patch_values, factor_coordinate, edge_coordinate = mappers[key]
                code = overlay_codes[mesh.materials[plan['slots'][index]].as_pointer()] if overlay else None
                if not face_is_blended(face, patch, previous):
                    remember_base_tile(face, stored)
                for number, (layer, uvs) in enumerate(zip(layers, coordinates)):
                    if number == 0 and keep_a:
                        continue   # A stays the tile the face shows now (it is in the render map)
                    for loop, uv in zip(face.loops, uvs):
                        set_corner_uv(loop, layer, uv)
                for loop in face.loops:
                    loop[factor].uv = (patch_values.opacity / 100, code) if overlay else factor_coordinate(loop[stored.mask].xy)
                    if edge is not None:loop[edge].uv=edge_coordinate(loop[stored.mask].xy)
                for key, layer in metadata.items():
                    face[layer] = getattr(patch_values, key)
                face[flags] = blend_direction_flag([item[0] for item in BLEND_DIRECTIONS].index(patch_values.direction)) | (4 if patch_values.smooth else 0) | (32 if patch_values.edge_style=='SNOW' else 16 if patch_values.edge_style=='GRASS' else 0)
                if overlay:
                    face[flags] |= OVERLAY_FLAG | ((code - FIRST_OVERLAY_CODE) << 7)
                elif patch_values.path:
                    face[flags] |= PATH_FLAG
                if patch_values.tiles == 'FACE':
                    face[flags] |= TILE_FACE_FLAG
                face[patch] = face_patch_ids[index]
                if cell_size:
                    for loop in face.loops:
                        loop[stored.base].z = cell_size & 0xffff
                        loop[stored.mask].z = cell_size >> 16
                    face[flags] |= OVERLAY_CELL_SIZE_FLAG
                if path_mark is not None:
                    face[path_mark] = 0
                face.material_index = plan['slots'][index]
            mesh[BLEND_MESH_TAG] = 3 if edge is not None else 2
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
            _blend_restore_uv_choice(mesh, snapshot)
    except Exception:
        _rollback_shared_blend(touched, edge_journals, journals)
        raise
    if undo_log is not None:
        undo_log.append((touched, edge_journals, journals))
    tag_redraw_view3d()
    return True


def _rollback_shared_blend(touched, edge_journals, journals):
    # Every restore runs even if one of them fails, so a problem in one mesh or material never leaves the
    # others half-changed; the first error is the one reported.
    for step, item in [(_blend_restore_plan, pair) for pair in reversed(touched)] + \
                      [(restore_edge_support, j) for j in reversed(edge_journals)] + \
                      [(restore_shared_material, j) for j in reversed(journals)]:
        try:
            step(*item) if isinstance(item, tuple) else step(item)
        except Exception as error:
            print('Sprotile: rollback step failed:', error)


def rollback_shared_blends(journal):
    """Take back every successful apply_shared_texture_blend call that recorded itself in journal, newest first."""
    while journal:
        _rollback_shared_blend(*journal.pop())
    tag_redraw_view3d()


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import keep_undo_in_step
from .draw import tag_redraw_view3d
from .tiles import edit_mesh_objects, get_tile_dims, image_size, is_editable_mesh, tile_uv_bounds
from .blend_settings import (
    BLEND_CONTROL_FIELDS, BLEND_MESH_TAG, BLEND_ORIGINAL_MATERIAL, BlendSettings,
    FIRST_OVERLAY_CODE, LATE_PATCH_FLOATS, OVERLAY_CELL_SIZE_FLAG, OVERLAY_FLAG, OVERLAY_INDEX_LIMIT,
    PATH_FLAG, SHAPED_EDGES, SHARED_EDGE_UV,
    SHARED_FACTOR_UV, SHARED_HINT_LIMIT, SHARED_MATERIAL_TAG, SHARED_PATCH_FLAGS, SHARED_PATCH_FLOATS,
    PATH_PAINT_ATTRIBUTE, SHARED_PATCH_ID, TILE_FACE_FLAG, _shared_patch_hints, blend_direction_flag, validated_blend_settings,
    validate_blend_settings, DIRECTION_BITS, PAINTED_FLAG, decode_direction_flag,
)
from .blend_plan import (
    _blend_restore_plan, _blend_restore_uv_choice, _blend_snapshot, overlay_capture_cell_size, prepare_blend_plans, tile_domain,
    validated_blend_tile,
)
from .blend_shader import (
    ensure_shared_edge_support, ensure_shared_overlay, ensure_shared_path_support, restore_edge_support,
    restore_shared_material, shared_atlas_node, shared_edge_mapper, shared_factor_mapper, shared_nodes,
    upgrade_shared_material, validate_shared_grass, validate_shared_path,
)
from .blend_recipes import active_blend_settings, affine_uv, sample_uv_bounds, sampled_uv_transforms
from .blend_storage import (
    blend_uv_maps_needed, check_atlas_reads_render_map, corner_uv, edit_blend_layers, ensure_edit_blend_layers,
    face_is_blended, object_blend_layers, remember_base_tile, set_corner_uv, HISTORY_SLOT_MASK, history_conflict,
    history_slot,
)
from .blend_alpha import ensure_shared_alpha_support, shared_alpha_strength
