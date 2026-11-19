"""Reading a placed blend back: its UV transforms, recipes, Copy Blend and turning a copy.
"""

import bpy
import bmesh
import math
import struct
from bpy.props import IntProperty


def active_blend_settings(context):
    """Preserve the active blend's appearance when changing material modes."""
    if context.scene.sprotile_blend_settings_source == 'NEW':
        return context.scene.sprotile_blend_defaults
    patch = active_shared_patch(context)
    if patch is not None:
        return patch
    material = active_blend_material(context)
    return material.sprotile_blend_controls if material else context.scene.sprotile_blend_defaults


def blend_uv_usage(mesh, shared, grass=False):
    """Read live Edit Mode layers without scanning faces or creating UV data."""
    layers = bmesh.from_edit_mesh(mesh).loops.layers.uv if mesh.is_editmode else mesh.uv_layers
    required = blend_uv_maps_needed(shared, grass)
    return len(layers), sum(layers.get(name) is None for name in required) + (not len(layers))


def apply_texture_blend(context):
    if context.scene.sprotile_blend_material_mode == 'SEPARATE':
        applied = apply_separate_texture_blend(context)
    else:
        applied = apply_shared_texture_blend(context)
    if applied:
        context.scene.sprotile_blend_settings_source = 'ACTIVE'
    return applied


def affine_uv(coefficients, uv):
    a,b,c,d,e,f = coefficients
    return a*uv[0]+b*uv[1]+c, d*uv[0]+e*uv[1]+f


def sample_uv_bounds(coefficients):
    if not all(math.isfinite(x) for x in coefficients):
        raise ValueError('Copied blend has invalid UV coordinates; pipette it again')
    a,b,c,d,e,f = coefficients
    if abs(a*e-b*d) <= 1e-16:
        raise ValueError('Copied blend has collapsed UVs; pipette a valid patch')
    corners = [affine_uv(coefficients,uv) for uv in ((0,0),(1,0),(1,1),(0,1))]
    low = tuple(min(uv[i] for uv in corners) for i in (0,1))
    high = tuple(max(uv[i] for uv in corners) for i in (0,1))
    if min(low) < -2e-6 or max(high) > 1+2e-6:
        raise ValueError('Copied blend samples outside the atlas; repair its UVs first')
    return low[0],low[1],high[0]-low[0],high[1]-low[1]


def sampled_uv_transforms(loops, mask, layers, domain=None):
    """Sample A/B together without duplicate per-corner coordinate arrays.

    A and B are affine in the domain: the Mask, or with Tiles: Per Face each face's share of it (tile_domain).
    """
    if not loops:
        raise ValueError('There are no blend corners to copy')
    point = (lambda loop: domain[loop]) if domain is not None else (lambda loop: corner_uv(loop, mask))
    p = tuple(point(loops[0]))
    j,furthest = 0,-1.0
    for i,loop in enumerate(loops):
        uv = point(loop)
        if not all(math.isfinite(value) for value in uv):
            raise ValueError('Blend contains invalid UVs')
        distance = (uv[0]-p[0])**2+(uv[1]-p[1])**2
        if distance > furthest:j,furthest = i,distance
    q = tuple(point(loops[j]))
    def determinant(r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    k,largest = 0,-1.0
    for i,loop in enumerate(loops):
        area = abs(determinant(point(loop)))
        if area > largest:k,largest = i,area
    r = tuple(point(loops[k]));det = determinant(r)
    if abs(det) <= 1e-12:
        raise ValueError('Blend mask is collapsed; pick a complete valid patch')
    transforms = []
    for layer in layers:
        target = [tuple(loops[index][layer].uv) for index in (0,j,k)]
        if not all(math.isfinite(value) for uv in target for value in uv):
            raise ValueError('Blend contains invalid UVs')
        coefficients = []
        for axis in (0,1):
            v0,v1,v2 = (uv[axis] for uv in target)
            a = ((v1-v0)*(r[1]-p[1])-(v2-v0)*(q[1]-p[1]))/det
            b = ((q[0]-p[0])*(v2-v0)-(r[0]-p[0])*(v1-v0))/det
            coefficients.extend((a,b,v0-a*p[0]-b*p[1]))
        sample_uv_bounds(coefficients)
        transforms.append(tuple(coefficients))
    for loop in loops:
        uv = point(loop)
        for layer,coefficients in zip(layers,transforms):
            actual = loop[layer].uv
            if not all(math.isfinite(value) for value in actual):
                raise ValueError('Blend contains invalid UVs')
            expected = affine_uv(coefficients,uv)
            if max(abs(a-b) for a,b in zip(actual,expected)) > 2e-6:
                raise ValueError("This blend's faces no longer match its layout (UVs edited by hand, or faces cut after a "
                                 "Per Face blend); reapply it before copying")
    return transforms


def sampled_uv_transform(loops, mask, layer):
    return sampled_uv_transforms(loops,mask,[layer])[0]


def validate_copy_gradient(material, settings):
    """Reject a custom separate-material gradient that no longer matches its controls."""
    if settings['edge_style'] == 'OVERLAY':
        check_separate_overlay(material, settings['opacity'])
        return
    nodes = blend_nodes(material)
    gradient, axis, mask = (nodes.get(name) for name in ('gradient','mask_axis','mask_uv'))
    if (gradient is None or gradient.bl_idname!='ShaderNodeMapRange'
            or axis is None or axis.bl_idname!='ShaderNodeSeparateXYZ'
            or not is_mask_node(mask)
            or any(node.mute for node in (gradient,axis,mask))):
        raise ValueError('The blend gradient nodes were edited; restore the generated gradient before copying')
    controls = BlendSettings(**settings)
    x,y,offset,reverse = blend_axis(controls)
    if is_path(controls):
        reverse = False
    width = max(controls.width,1e-6)
    expected = (controls.position-width*.5,controls.position+width*.5,1.0 if reverse else 0.0,0.0 if reverse else 1.0)
    expected = tuple(struct.unpack('f',struct.pack('f',value))[0] for value in expected)
    if (gradient.data_type!='FLOAT' or not gradient.clamp
            or gradient.interpolation_type!=('SMOOTHSTEP' if controls.smooth else 'LINEAR')
            or any(gradient.inputs[i].is_linked for i in range(1,5))
            or tuple(gradient.inputs[i].default_value for i in range(1,5))!=expected):
        raise ValueError('The shader gradient differs from its controls; reapply this blend before copying')
    def connected(destination, source):
        return len(destination.links)==1 and destination.links[0].from_socket==source
    if not connected(axis.inputs[0],mask_socket(mask)):
        raise ValueError('The blend mask connection was edited; restore it before copying')
    source = axis.outputs[0 if x else 1]
    if controls.direction=='ANGLE':
        dot,shift = nodes.get('angle_dot'),nodes.get('angle_offset')
        if (dot is None or dot.bl_idname!='ShaderNodeVectorMath' or dot.operation!='DOT_PRODUCT'
                or shift is None or shift.bl_idname!='ShaderNodeMath' or shift.operation!='ADD'
                or dot.mute or shift.mute or shift.use_clamp
                or dot.inputs[1].is_linked or shift.inputs[1].is_linked
                or not connected(dot.inputs[0],mask_socket(mask))
                or not connected(shift.inputs[0],dot.outputs['Value'])
                or tuple(dot.inputs[1].default_value)!=tuple(struct.unpack('f',struct.pack('f',v))[0] for v in (x,y,0))
                or shift.inputs[1].default_value!=struct.unpack('f',struct.pack('f',offset))[0]):
            raise ValueError('The blend angle nodes were edited; restore them before copying')
        source = shift.outputs[0]
    if is_path(controls):
        source = check_separate_path(material, source, controls)
    if controls.edge_style in SHAPED_EDGES:
        dot,center,strength=(nodes.get(name) for name in ('edge_tangent_dot','edge_tangent_center','edge_strength'))
        tx,ty=(-y,x) if not reverse else (y,-x);tx*=controls.edge_frequency;ty*=controls.edge_frequency
        f32=lambda v:struct.unpack('f',struct.pack('f',v))[0]
        if (dot is None or dot.bl_idname!='ShaderNodeVectorMath' or dot.operation!='DOT_PRODUCT'
                or center is None or center.bl_idname!='ShaderNodeMath' or center.operation!='ADD'
                or strength is None or strength.bl_idname!='ShaderNodeValue'
                or tuple(dot.inputs[1].default_value)!=tuple(f32(v) for v in (tx,ty,0))
                or center.inputs[1].default_value!=f32(-.5*(tx+ty))
                or strength.outputs[0].default_value!=f32(controls.edge_height*(-1 if reverse else 1))
                or not connected(dot.inputs[0],mask_socket(mask)) or not connected(center.inputs[0],dot.outputs['Value'])):
            raise ValueError('Grass-edge controls differ from their shader; reapply before copying')
        source=validate_grass_profile(material,'sprotile_blend_role',center.outputs[0],strength.outputs[0],source,snow=controls.edge_style=='SNOW')
    if not connected(gradient.inputs[0],source):
        raise ValueError('The blend gradient connection was edited; restore it before copying')


def read_blend_recipe(context):
    obj = context.active_object
    if not is_editable_mesh(obj):
        raise ValueError('Select a blended face in Edit Mode')
    bm = bmesh.from_edit_mesh(obj.data)
    face = bm.faces.active
    if face is None or not face.select or face.hide:
        selected = (f for f in bm.faces if f.select and not f.hide)
        face = next(selected, None)
        if face is None or next(selected, None) is not None:
            raise ValueError('Make a face in the blend active, or select just one face to pipette')
    return blend_recipe_from_face(obj,bm,face)


class RecipeSources:
    """Material connections are constant during one synchronous copy or search."""
    def __init__(self):
        self.images = {}
        self.overlay_banks = {}

    def image_pair(self, material, separate, settings, code):
        if material is None:
            raise ValueError('The patch material is missing')
        style = settings['edge_style']
        key = (material, separate, style, code, bool(settings.get('path')))
        if key not in self.images:
            try:
                self.images[key] = self._read_pair(material, separate, settings, code)
            except (ValueError, RuntimeError) as error:
                # A damaged shader can be shared by hundreds of patches too.
                self.images[key] = str(error)
        result = self.images[key]
        if isinstance(result, str):
            raise ValueError(result)
        return result

    def _read_pair(self, material, separate, settings, code):
        roles = blend_nodes(material) if separate else shared_nodes(material)
        if separate:
            validate_copy_gradient(material, settings)
        elif code is not None:
            if material not in self.overlay_banks:
                self.overlay_banks[material] = shared_overlay_bank(material)
            roles['texture_b'] = self.overlay_banks[material].get(code)
            if roles['texture_b'] is None:
                raise ValueError('The selected overlay source is missing. Reapply before copying.')
        elif settings['edge_style'] in SHAPED_EDGES:
            validate_shared_grass(material, require_snow=settings['edge_style']=='SNOW')
        if not separate and settings.get('path'):
            validate_shared_path(material)
        images = []
        for i, role in enumerate(('texture_a', 'texture_b')):
            texture = roles.get(role)
            image = getattr(texture, 'image', None)
            if image is None or min(image_size(image)) <= 0:
                raise ValueError('The blend source images are missing; repair the material first')
            uv = roles.get(role+'_uv' if separate else 'uv_'+str(i))
            if (uv is None or uv.type != 'UVMAP' or uv.uv_map != ('', BLEND_B_UV)[i]
                    or texture.type != 'TEX_IMAGE' or texture.projection != 'FLAT'
                    or not any(link.from_node == uv for link in texture.inputs['Vector'].links)):
                raise ValueError('The blend sampling nodes were edited; restore the generated UV connections before copying')
            images.append(image)
        return tuple(images)


def blend_recipe_from_face(obj,bm,face,source_faces=None,reader=None):
    material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
    if material is None:
        raise ValueError('The active face has no blend material')
    separate = bool(material.get('sprotile_blend_material'))
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    if separate:
        if source_faces is None:
            eligible = [f for f in bm.faces if f.material_index == face.material_index]
            faces = next(patch for patch in connected_face_patches(eligible) if face in patch)
        else:faces = source_faces
        settings = {key:getattr(material.sprotile_blend_controls,key) for key in BLEND_CONTROL_FIELDS}
    elif material.get(SHARED_MATERIAL_TAG) and membership is not None and face[membership] > 0:
        if source_faces is not None:
            faces = source_faces
        else:
            # Only this face's copy: duplicating or joining blended faces can leave two copies on one patch id.
            faces = blend_copies(obj.data, bm, face[membership], keep=(face,))[0]
        flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
        fields = {key:bm.faces.layers.float.get(name) for key,name in SHARED_PATCH_FLOATS.items()}
        if flags is None or any(fields[key] is None for key in ('position','width')):
            raise ValueError('Blend settings are incomplete; reapply the patch before copying it')
        factor_layer = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
        styles = {0:'PLAIN', 16:'GRASS', 32:'SNOW', OVERLAY_FLAG:'OVERLAY'}
        def values(f):
            bits = f[flags]
            style = styles.get(bits & (48 | OVERLAY_FLAG))
            if style is None:raise ValueError('Patch edge flags are inconsistent; reapply before copying')
            if style == 'OVERLAY' and factor_layer is None:
                raise ValueError('Overlay factor map is missing; reapply before copying')
            opacity = f.loops[0][factor_layer].uv.x * 100 if style == 'OVERLAY' else 100.0
            path = bool(bits & PATH_FLAG) and style != 'OVERLAY'
            return dict(direction=BLEND_DIRECTIONS[decode_direction_flag(bits)][0],smooth=bool(bits & 4),edge_style=style,opacity=opacity,path=path,
                        tiles='FACE' if bits & TILE_FACE_FLAG else 'PATCH',
                        **{key:(LATE_PATCH_FLOATS[key] if (key in {'edge_height','edge_frequency'} and not bits&48) or (key == 'path_width' and not path) else
                                (f[layer] if layer is not None else LATE_PATCH_FLOATS[key])) for key,layer in fields.items()})
        settings = values(face)
        if any(values(f) != settings for f in faces):
            raise ValueError('Patch IDs contain mixed settings after mesh edits; reapply this patch before copying it')
        strength = shared_alpha_strength(material)
        settings['alpha_shaping'] = strength > 0
        settings['alpha_strength'] = strength if strength > 0 else 2.0
    else:
        raise ValueError('The active face is not a Sprotile blend; use the ordinary tile pipette')
    validate_blend_settings(settings)
    if reader is None:
        reader = RecipeSources()
    codes = {}
    shared_overlay = not separate and settings['edge_style'] == 'OVERLAY'
    if shared_overlay:
        for f in faces:
            code = FIRST_OVERLAY_CODE + ((f[flags] >> 7) & (OVERLAY_INDEX_LIMIT - 1))
            if codes.setdefault(f.material_index, code) != code:
                raise ValueError('Mixed overlay sources on one patch. Reapply before copying.')
    images = None
    for slot in {f.material_index for f in faces}:
        other = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
        current_images = reader.image_pair(other, separate, settings, codes.get(slot))
        if images is None:
            images = current_images
        elif current_images != images:
            raise ValueError('This patch contains different source images; reapply it before copying')
    stored = edit_blend_layers(obj.data, bm)
    layers = [stored.a, stored.b, stored.mask]
    if any(layer is None for layer in layers):
        raise ValueError('Blend UV maps are missing; reapply the blend before copying it')
    loops = [loop for f in faces for loop in f.loops]
    if not separate:
        factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
        if factor is None:
            raise ValueError('Blend factor map is missing; reapply before copying')
        expected_factor = shared_factor_mapper(BlendSettings(**settings))
        if not all(math.isfinite(value) for loop in loops for value in loop[factor].uv):
            raise ValueError('Blend factor map contains invalid values; reapply this patch before copying')
        def expected_at(loop):
            if shared_overlay:
                return settings['opacity'] / 100, codes[loop.face.material_index]
            return expected_factor(loop[layers[2]].xy)
        if any(abs(a-b) > 2e-6*max(1.0,abs(b)) for loop in loops
               for a,b in zip(loop[factor].uv,expected_at(loop))):
            raise ValueError('Blend factors differ from their saved settings; reapply this patch before copying')
        if settings['edge_style'] in SHAPED_EDGES:
            edge=bm.loops.layers.uv.get(SHARED_EDGE_UV);expected_edge=shared_edge_mapper(BlendSettings(**settings))
            if edge is None or any(not math.isfinite(a) or abs(a-b)>2e-6*max(1.,abs(b)) for loop in loops for a,b in zip(loop[edge].uv,expected_edge(loop[layers[2]].xy))):
                raise ValueError('Grass-edge coordinates differ from their settings; reapply before copying')
    transforms = sampled_uv_transforms(loops,layers[2],layers[:2],domain=tile_domain(faces,layers[2],settings['tiles']=='FACE'))
    recipe = dict(settings=settings,mode='SEPARATE' if separate else 'SHARED')
    overlay_cell_size = 0
    if settings['edge_style'] == 'OVERLAY':
        overlay_cell_size = (material.get(OVERLAY_CELL_SIZE_ATTRIBUTE, 0) if separate
                             else shared_overlay_cell_size(obj.data, faces, bm))
    for slot,image,transform in zip(('a','b'),images,transforms):
        width,height = image_size(image)
        size = overlay_cell_size if slot == 'b' else 0
        recipe[slot] = dict(image=image,col=0,row=0,atlas_width=width,atlas_height=height,width_px=size,height_px=size,
                            rotation=0,flip_u=False,flip_v=False,inset_px=0.,sample_uv=transform,is_sampled=True)
    return recipe


def write_blend_recipe(scene,recipe):
    for slot in ('a','b'):
        record = getattr(scene,'sprotile_blend_'+slot)
        for key,value in recipe[slot].items():setattr(record,key,value)
        invalidate_clipboard_slot(scene, slot)
    for key,value in recipe['settings'].items():setattr(scene.sprotile_blend_defaults,key,value)
    scene.sprotile_blend_material_mode = recipe['mode']
    scene.sprotile_blend_settings_source = 'NEW'


def copied_tile_in_grid(scene, record):
    """The picker tile a copied layer shows: (mode, col, row, rotation, flip_u, flip_v, inset px), or None.

    None when the layer isn't one whole tile of the current grid: a Keep Proportions crop, an overlay PNG, or an
    atlas cut at another Tile Size.
    """
    image = record.image
    if image is None or not record.is_sampled:
        return None
    width, height = image_size(image)
    if min(width, height) <= 0:
        return None
    coefficients = tuple(record.sample_uv)
    u0, v0, du, dv = sample_uv_bounds(coefficients)
    unit = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))
    for mode in ('FULL', 'HALF_WIDE', 'HALF_TALL'):
        tile_w, tile_h = tile_dimensions(scene.sprotile_tile_size, mode)
        if tile_w > width or tile_h > height:
            continue
        col = int((u0 + du / 2) * width // tile_w)
        row = int((1 - (v0 + dv / 2)) * height // tile_h)
        if not (0 <= col < width // tile_w and 0 <= row < height // tile_h):
            continue
        x, y, w, h = tile_uv_bounds(col, row, tile_w, tile_h, width, height)
        margins = ((u0 - x) * width, (x + w - u0 - du) * width, (v0 - y) * height, (y + h - v0 - dv) * height)
        if min(margins) < -.01 or max(margins) - min(margins) > .02 or max(margins) > min(tile_w, tile_h) * .25:
            continue
        local = [((point[0] - x) / w, (point[1] - y) / h) for point in (affine_uv(coefficients, c) for c in unit)]
        rotation, flip_u, flip_v = detect_tile_transform(local, list(unit))
        return mode, col, row, rotation, flip_u, flip_v, sum(margins) / 4
    return None


def copy_blend_to_scene(context):
    """Copy the active face's blend into New Blend. Returns whether the picker now shows its tile A.

    The picker follows tile A, so the readout, Find and the 4 key don't stay on an unrelated earlier tile.
    """
    scene = context.scene
    write_blend_recipe(scene,read_blend_recipe(context))
    reset_preview_interaction()
    found = copied_tile_in_grid(scene, scene.sprotile_blend_a)
    if found is None:
        return False
    mode, col, row, rotation, flip_u, flip_v, inset = found
    scene.sprotile_tile_mode = mode
    scene.sprotile_active_col, scene.sprotile_active_row = col, row
    scene.sprotile_rotation = str(rotation)
    scene.sprotile_flip_u, scene.sprotile_flip_v = flip_u, flip_v
    scene.sprotile_use_uv_inset = inset > .01
    if inset > .01:
        scene.sprotile_uv_inset_px = inset
    _recent_tiles.remember(scene, scene.sprotile_blend_a.image, col, row)
    return True


class MESH_OT_sprotile_pick_blend(bpy.types.Operator):
    """Copy the active face's blend, both tiles and every setting, into New Blend; Apply Blend places the copy"""
    bl_idname = 'mesh.sprotile_pick_blend'
    bl_label = 'Copy Blend'
    bl_options = {'UNDO'}
    @classmethod
    def poll(cls,context):return is_editable_mesh(context.active_object)
    def execute(self,context):
        try:follows = copy_blend_to_scene(context)
        except ValueError as error:
            self.report({'WARNING'},str(error));return {'CANCELLED'}
        self.report({'INFO'},'Blend copied' + ('; the picker shows its tile A' if follows else '')
                    + '. Select the target faces, turn the copy if needed, then Apply Blend')
        return {'FINISHED'}


class MESH_OT_sprotile_rotate_blend_copy(bpy.types.Operator):
    """Turn both copied tiles and their transition 90 degrees; Apply Blend places the copy"""
    bl_idname = 'mesh.sprotile_rotate_blend_copy'
    bl_label = 'Turn Blend Copy'
    bl_options = {'UNDO'}
    quarter_turns: IntProperty(default=1,min=-1,max=1)
    def execute(self,context):
        scene = context.scene
        if not (scene.sprotile_blend_a.is_sampled and scene.sprotile_blend_b.is_sampled):
            self.report({'WARNING'},'Use Copy Blend on a placed blend first');return {'CANCELLED'}
        try:tiles = [validated_blend_tile(record) for record in (scene.sprotile_blend_a,scene.sprotile_blend_b)]
        except ValueError as error:
            self.report({'WARNING'},str(error));return {'CANCELLED'}
        rotation = self.quarter_turns*90
        transformed = []
        for tile in tiles:
            points = [affine_uv(tile['sample_uv'],apply_tile_transform(*uv,rotation,False,False))
                      for uv in ((0,0),(1,0),(0,1))]
            coefficients = tuple(value for axis in (0,1) for value in
                                 (points[1][axis]-points[0][axis],points[2][axis]-points[0][axis],points[0][axis]))
            sample_uv_bounds(coefficients);transformed.append(coefficients)
        controls = scene.sprotile_blend_defaults
        try:settings = validated_blend_settings(controls)
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        if settings['edge_style'] != 'OVERLAY':
            x,y,offset,reverse = blend_axis(controls)
            if reverse:x,y = -x,-y
            settings['direction'] = 'ANGLE'
            settings['angle'] = (math.atan2(y,x)-math.radians(rotation)) % math.tau
            if reverse:settings['position'] = 1-settings['position']
        for record,coefficients in zip((scene.sprotile_blend_a,scene.sprotile_blend_b),transformed):record.sample_uv = coefficients
        for key,value in settings.items():setattr(scene.sprotile_blend_defaults,key,value)
        scene.sprotile_blend_settings_source = 'NEW'
        reset_preview_interaction()
        self.report({'INFO'},'Copy turned; Apply Blend to place it')
        return {'FINISHED'}


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .tiles import (
    _recent_tiles, apply_tile_transform, detect_tile_transform, image_size, is_editable_mesh, tile_dimensions,
    tile_uv_bounds,
)
from .mapping import connected_face_patches
from .blend_settings import (
    BLEND_B_UV, BLEND_CONTROL_FIELDS, BLEND_DIRECTIONS, BlendSettings, FIRST_OVERLAY_CODE,
    LATE_PATCH_FLOATS, OVERLAY_CELL_SIZE_ATTRIBUTE, OVERLAY_FLAG, OVERLAY_INDEX_LIMIT, PATH_FLAG, SHAPED_EDGES,
    SHARED_EDGE_UV,
    SHARED_FACTOR_UV, SHARED_MATERIAL_TAG, SHARED_PATCH_FLAGS, SHARED_PATCH_FLOATS, SHARED_PATCH_ID,
    TILE_FACE_FLAG, blend_axis, blend_nodes, check_separate_path, validate_blend_settings,
    validated_blend_settings, decode_direction_flag,
)
from .blend_plan import active_blend_material, apply_separate_texture_blend, tile_domain, validated_blend_tile
from .blend_shader import (
    check_separate_overlay, is_path, shared_edge_mapper, shared_factor_mapper, shared_nodes,
    shared_overlay_bank, validate_grass_profile, validate_shared_grass, validate_shared_path,
)
from .blend_shared import active_shared_patch, blend_copies, apply_shared_texture_blend, shared_overlay_cell_size
from .blend_alpha import shared_alpha_strength
from .blend_ops import invalidate_clipboard_slot, reset_preview_interaction
from .blend_storage import blend_uv_maps_needed, corner_uv, edit_blend_layers, is_mask_node, mask_socket
