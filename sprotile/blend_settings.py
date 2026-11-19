"""Blend settings: UV map and attribute names, styles, flags, validation, the property groups, the
Separate Material graph and rectangle corners.
"""

import bpy
import bmesh
import blf
import math
import struct
from collections import namedtuple
from bpy.props import IntProperty, FloatProperty, FloatVectorProperty, StringProperty, EnumProperty, BoolProperty, PointerProperty
from . import state


# ---------------------------------------------------------------------------
# Live two-texture blending
# ---------------------------------------------------------------------------

# Where a blend lives (5.13, see blend_storage): tile A in the render UV map; B, Factor and Edge in UV maps; the
# patch Mask and each blended face's previous tile in corner attributes, which FBX export leaves out.
BLEND_B_UV = 'SprotileBlend_B'
SHARED_FACTOR_UV = 'SprotileBlend_Factor'
SHARED_EDGE_UV = 'SprotileBlend_Edge'
BLEND_MASK_ATTRIBUTE = 'sprotile_blend_mask'
BLEND_BASE_ATTRIBUTE = 'sprotile_blend_base'
# Sprotile 5.12 and earlier kept A and the Mask in UV maps; blend_storage upgrades such files.
LEGACY_A_UV, LEGACY_MASK_UV = 'SprotileBlend_A', 'SprotileBlend_Mask'
# UV maps that are Sprotile's own data, never the map a user paints tiles into.
INTERNAL_BLEND_UVS = (LEGACY_A_UV, BLEND_B_UV, LEGACY_MASK_UV, SHARED_FACTOR_UV, SHARED_EDGE_UV)
SHARED_PATCH_ID = 'sprotile_blend_patch_id'
PATH_PAINT_ATTRIBUTE = 'sprotile_path_paint'
SHARED_PATCH_FLOATS = {'position': 'sprotile_blend_position', 'width': 'sprotile_blend_width',
                       'angle': 'sprotile_blend_angle', 'edge_height':'sprotile_edge_height', 'edge_frequency':'sprotile_edge_frequency',
                       'path_width': 'sprotile_blend_path_width'}
# Fields whose face attribute may be missing in older files; they are created when first edited.
LATE_PATCH_FLOATS = {'angle': 0.0, 'edge_height': .12, 'edge_frequency': 8., 'path_width': .3}
SHARED_PATCH_FLAGS = 'sprotile_blend_flags'
# Integer hints only: never retain BMesh/RNA wrappers across Undo or topology
# changes. Each lookup validates current membership, with a bounded fallback.
_shared_patch_hints = {}
SHARED_HINT_LIMIT = 512
BlendSettings = namedtuple('BlendSettings', ('direction', 'position', 'width', 'smooth', 'angle','edge_style','edge_height','edge_frequency','opacity',
                                             'path','path_width','tiles','alpha_shaping','alpha_strength'), defaults=(0.0,'PLAIN',.12,8.,100.,False,.3,'PATCH',False,2.0))
SHARED_MATERIAL_TAG = 'sprotile_shared_blend'
BLEND_DIRECTIONS = [
    ('LEFT_RIGHT', "Left to Right", "Texture A at low U, texture B at high U"),
    ('RIGHT_LEFT', "Right to Left", "Texture A at high U, texture B at low U"),
    ('BOTTOM_TOP', "Bottom to Top", "Texture A at low V, texture B at high V"),
    ('TOP_BOTTOM', "Top to Bottom", "Texture A at high V, texture B at low V"),
    ('ANGLE', "Angle", "Rotate the transition: 0 degrees left to right, 90 degrees bottom to top"),
    ('PAINTED', "Painted Only", "No gradient: Position sets one amount of B for the whole patch (0 shows A), and "
                                "Tint alpha painted with Paint B shows B where you paint")]
BLEND_MESH_TAG = 'sprotile_blend_uv_version'
BLEND_ORIGINAL_MATERIAL = 'sprotile_blend_previous_material'
EDGE_STYLES=[('PLAIN','Straight','Standard straight transition'),('GRASS','Grass Edge','Pointed tufts from texture A extend into B'),
             ('SNOW','Snow Drift','Rounded rises and dips from texture A into B; use a low Wave Count for broad drifts'),
             ('OVERLAY','Overlay','Lay B over A using the alpha channel of B')]
SHAPED_EDGES = {'GRASS', 'SNOW'}
OVERLAY_FLAG = 64
OVERLAY_CELL_SIZE_ATTRIBUTE = 'sprotile_overlay_cell_size'
OVERLAY_CELL_SIZE_FLAG = 1 << 15
FIRST_OVERLAY_CODE = 3  # Factor V 0, 1 and 2 already mean ordinary, linear and smooth.
# Paths put B in a band with A on both sides. Factor U holds the distance from the path's centre line in units of
# its outer half-width (the band plus half the feather), so |U| <= 1 covers the band and its fade. Factor V is 1 or 2
# (linear or smooth) plus a fraction .05-.45 that stores where the fade starts, k in [-1, 1), as .05 + .2 * (k + 1).
# Integer modes and overlay codes have no fraction, and the values survive Unreal's 16-bit UVs to about .005 of
# the band. Shaders: x = (1 - |U|) / (1 - k), then the usual clamp, curve and tufts.
PATH_FLAG = 1 << 12
PATH_FRACTION_MIN, PATH_FRACTION_SCALE = .05, .2
# Tiles: Per Face. A and B fill each face's own share of the patch layout (its box in the Mask UVs), so they repeat
# face by face like Map Per Face, while the Mask, and so the transition, still runs across the whole patch. Only the
# A and B UVs differ, so neither the Blender shader nor the Unreal material changes.
TILE_FACE_FLAG = 1 << 13
# Direction Painted Only (5.14.4): Factor U is the patch's Position everywhere, so only painted Tint alpha moves it.
PAINTED_FLAG = 1 << 14
OVERLAY_INDEX_LIMIT = 32   # An overlay's index sits in flag bits 7-11; bit 12 is PATH_FLAG (64 would have collided, 5.14.3).
TILE_MODES = [('PATCH', 'Stretch', 'Stretch one A and one B tile over the whole patch'),
              ('FACE', 'Per Face', 'Give every face the whole A and B tile, like Map Per Face; the transition '
                                   'still runs across the whole patch')]
BLEND_CONTROL_FIELDS = BlendSettings._fields
BLEND_TILE_FIELDS = ('image', 'col', 'row', 'width_px', 'height_px',
                     'atlas_width', 'atlas_height', 'rotation', 'flip_u', 'flip_v', 'inset_px',
                     'is_sampled', 'sample_uv')


def validate_blend_settings(settings):
    """One boundary check for saved recipes, sampled patches and application."""
    settings.setdefault('path', False)
    settings.setdefault('path_width', .3)
    settings.setdefault('tiles', 'PATCH')
    settings.setdefault('alpha_shaping', False)
    settings.setdefault('alpha_strength', 2.0)
    limits={'position':(0.,1.),'width':(0.,2.),'angle':(0.,struct.unpack('f',struct.pack('f',math.tau))[0]),
            'edge_height':(0.,.5),'edge_frequency':(1.,64.),'opacity':(0.,100.),'path_width':(0.,1.),'alpha_strength':(0.,8.)}
    for name,(low,high) in limits.items():
        value=settings[name]
        if not math.isfinite(value) or not low<=value<=high:
            raise ValueError('Blend '+name.replace('_',' ')+' is invalid or outside its range; restore the settings or save a new preset')
    if settings['direction'] not in {item[0] for item in BLEND_DIRECTIONS} or settings['edge_style'] not in {item[0] for item in EDGE_STYLES}:
        raise ValueError('Blend direction or edge style is invalid; restore the settings')
    if settings['tiles'] not in {item[0] for item in TILE_MODES}:
        raise ValueError('Blend tile layout is invalid; choose Stretch or Per Face')
    # Overlays cover the whole patch, and Painted Only has no centre line, so a Path tick left on from another
    # style means nothing for them.
    settings['path'] = bool(settings['path']) and settings['edge_style'] != 'OVERLAY' and settings['direction'] != 'PAINTED'
    return settings


def validated_blend_settings(controls):
    return validate_blend_settings({name:getattr(controls,name) for name in BLEND_CONTROL_FIELDS})


def blend_nodes(material):
    if material_nodes(material) is None:
        return {}
    return {node.get('sprotile_blend_role'): node for node in material.node_tree.nodes
            if node.get('sprotile_blend_role')}


def blend_axis(controls):
    """Linear UV projection and reversal, preserving the four legacy directions."""
    if controls.direction == 'PAINTED':
        return 0.0, 0.0, 0.0, False   # no gradient; shared_factor_mapper writes Position directly
    if controls.direction == 'ANGLE':
        x, y = math.cos(controls.angle), math.sin(controls.angle)
        # Exact cardinal coefficients avoid visible noise at narrow widths.
        x, y = (0.0 if abs(value) < 1e-6 else value for value in (x, y))
        return x, y, .5 - .5 * (x + y), False
    horizontal = controls.direction in {'LEFT_RIGHT', 'RIGHT_LEFT'}
    return (1.0 if horizontal else 0.0), (0.0 if horizontal else 1.0), 0.0, controls.direction in {'RIGHT_LEFT', 'TOP_BOTTOM'}


def blend_direction_flag(index):
    # Bit 2 already stores smoothing in older files; custom angle uses bit 3, Painted Only bit 14.
    return 8 if index == 4 else PAINTED_FLAG if index == 5 else index


DIRECTION_BITS = 11 | PAINTED_FLAG


def decode_direction_flag(bits):
    """The BLEND_DIRECTIONS index stored in a patch's flags."""
    bits = int(bits)
    return 5 if bits & PAINTED_FLAG else 4 if bits & 8 else bits & 3


def separate_path_base(material, source, controls):
    """(centre + half path width) - |coordinate - centre|: above the centre inside the band, below it outside."""
    links = material.node_tree.links
    def math_node(role, operation, a, b):
        node = edge_node(material, 'sprotile_blend_role', 'ShaderNodeMath', role)
        node.operation = operation; node.use_clamp = False
        for i, value in enumerate((a, b)):
            if isinstance(value, (int, float)):
                node.inputs[i].default_value = value
            else:
                links.new(value, node.inputs[i])
        return node.outputs[0]
    offset = math_node('path_offset', 'SUBTRACT', source, controls.position)
    distance = math_node('path_distance', 'ABSOLUTE', offset, 0.)
    return math_node('path_base', 'SUBTRACT', controls.position + controls.path_width * .5, distance)


def check_separate_path(material, source, controls):
    """The separate-material path nodes for these controls; returns the band output."""
    nodes = blend_nodes(material)
    f32 = lambda v: struct.unpack('f', struct.pack('f', v))[0]
    offset, distance, base = (nodes.get(n) for n in ('path_offset', 'path_distance', 'path_base'))
    def ok(node, operation, first, second):
        if node is None or node.bl_idname != 'ShaderNodeMath' or node.operation != operation or node.mute or node.use_clamp:
            return False
        for socket, value in zip(node.inputs, (first, second)):
            if value is None:
                continue
            if isinstance(value, float):
                if socket.is_linked or socket.default_value != f32(value):
                    return False
            elif len(socket.links) != 1 or socket.links[0].from_socket != value:
                return False
        return True
    if not (ok(offset, 'SUBTRACT', source, controls.position)
            and ok(distance, 'ABSOLUTE', offset.outputs[0] if offset else None, None)
            and ok(base, 'SUBTRACT', controls.position + controls.path_width * .5, distance.outputs[0] if distance else None)):
        raise ValueError('The path nodes differ from their controls; reapply this blend before copying')
    return base.outputs[0]


def update_blend_graph(controls, _context=None):
    """Material-owned controls stay live without changing mesh UVs or colours."""
    material = controls.id_data
    if not isinstance(material, bpy.types.Material) or not material.get('sprotile_blend_material'):
        return
    nodes = blend_nodes(material)
    gradient, axis = nodes.get('gradient'), nodes.get('mask_axis')
    if gradient is None or axis is None:
        return  # A user may have edited the generated graph manually.
    if controls.edge_style == 'OVERLAY':
        update_separate_overlay(material, nodes, controls.opacity)
        return
    if controls.direction == 'PAINTED':
        return   # Separate Material has no painted alpha; Apply refuses it, so leave the graph as it is
    if 'overlay_amount' in nodes and nodes.get('texture_mix') is not None:
        material.node_tree.links.new(gradient.outputs[0], nodes['texture_mix'].inputs[0])
        material.node_tree.links.new(nodes['texture_b'].outputs['Color'], nodes['texture_mix'].inputs[2])
    x, y, offset, reverse = blend_axis(controls)
    links = material.node_tree.links
    if controls.direction == 'ANGLE':
        if not axis.inputs[0].is_linked:
            return
        source = axis.inputs[0].links[0].from_socket
        dot, shift = nodes.get('angle_dot'), nodes.get('angle_offset')
        if dot is None:
            dot = material.node_tree.nodes.new('ShaderNodeVectorMath')
            dot['sprotile_blend_role'] = 'angle_dot'
            dot.label = 'Blend Angle'; dot.location = (-660, -420)
        if shift is None:
            shift = material.node_tree.nodes.new('ShaderNodeMath')
            shift['sprotile_blend_role'] = 'angle_offset'
            shift.label = 'Angle Centre'; shift.location = (-430, -420)
        dot.operation = 'DOT_PRODUCT'; shift.operation = 'ADD'
        dot.inputs[1].default_value = (x, y, 0.0)
        shift.inputs[1].default_value = offset
        links.new(source, dot.inputs[0]); links.new(dot.outputs['Value'], shift.inputs[0])
        output = shift.outputs[0]
    else:
        output = axis.outputs[0 if x else 1]
    if is_path(controls):
        # A path is symmetric: B inside the band, A outside on both sides, so no reversal.
        output, reverse = separate_path_base(material, output, controls), False
    if controls.edge_style in SHAPED_EDGES:
        mask=nodes.get('mask_uv')
        if mask is None:return
        mask_output=mask_socket(mask)
        node=lambda kind,role: edge_node(material,'sprotile_blend_role',kind,role)
        dot=node('ShaderNodeVectorMath','edge_tangent_dot');dot.operation='DOT_PRODUCT'
        tx,ty=(-y,x) if not reverse else (y,-x)
        tx*=controls.edge_frequency;ty*=controls.edge_frequency
        dot.inputs[1].default_value=(tx,ty,0);links.new(mask_output,dot.inputs[0])
        center=node('ShaderNodeMath','edge_tangent_center');center.operation='ADD';center.inputs[1].default_value=-.5*(tx+ty)
        links.new(dot.outputs['Value'],center.inputs[0])
        amount=node('ShaderNodeValue','edge_strength');amount.outputs[0].default_value=controls.edge_height*(-1 if reverse else 1)
        output=edge_profile_nodes(material,'sprotile_blend_role',center.outputs[0],amount.outputs[0],output,snow=controls.edge_style=='SNOW')
    for link in list(gradient.inputs[0].links):
        links.remove(link)
    links.new(output, gradient.inputs[0])
    width = max(controls.width, 1e-6)
    gradient.inputs[1].default_value = controls.position - width * 0.5
    gradient.inputs[2].default_value = controls.position + width * 0.5
    gradient.inputs[3].default_value = 1.0 if reverse else 0.0
    gradient.inputs[4].default_value = 0.0 if reverse else 1.0
    gradient.clamp = True
    gradient.interpolation_type = 'SMOOTHSTEP' if controls.smooth else 'LINEAR'
    material.update_tag()


class SprotileBlendTile(bpy.types.PropertyGroup):
    image: PointerProperty(type=bpy.types.Image)
    col: IntProperty(default=0, min=0)
    row: IntProperty(default=0, min=0)
    width_px: IntProperty(default=0, min=0)
    height_px: IntProperty(default=0, min=0)
    atlas_width: IntProperty(default=0, min=0)
    atlas_height: IntProperty(default=0, min=0)
    rotation: IntProperty(default=0)
    flip_u: BoolProperty(default=False)
    flip_v: BoolProperty(default=False)
    inset_px: FloatProperty(default=0.0, min=0.0)
    is_sampled: BoolProperty(default=False)
    sample_uv: FloatVectorProperty(size=6, default=(1,0,0,0,1,0))


class SprotileBlendControls(bpy.types.PropertyGroup):
    opacity: FloatProperty(name='Opacity', description='Strength of the overlay over the base tile',
        subtype='PERCENTAGE', min=0, max=100, default=100, update=update_blend_graph)
    feather_percent: FloatProperty(name='Feather',description='Total fade width as a percentage of the patch; 2% fades about 1% on each side of the edge',
        subtype='PERCENTAGE',min=0,max=200,get=lambda self:self.width*100,set=lambda self,value:setattr(self,'width',value/100))
    direction: EnumProperty(name="Direction", items=BLEND_DIRECTIONS,
        default='TOP_BOTTOM', update=update_blend_graph)
    position: FloatProperty(name="Position", description="Transition centre in patch UV space",
                            default=0.5, min=0.0, max=1.0, update=update_blend_graph)
    width: FloatProperty(name="Blend Width", description="Transition width as a fraction of the patch",
                         default=0.35, min=0.0, max=2.0, update=update_blend_graph)
    smooth: BoolProperty(name="Smooth Feather", default=True, update=update_blend_graph)
    angle: FloatProperty(name="Angle", description="Blend direction in patch UVs: 0 degrees right, 90 degrees up",
                         subtype='ANGLE', default=0.0, min=0.0, max=math.tau, update=update_blend_graph)
    edge_style: EnumProperty(name='Style',items=EDGE_STYLES,default='PLAIN',update=update_blend_graph)
    edge_height: FloatProperty(name='Tuft Height',default=.12,min=0,max=.5,update=update_blend_graph)
    edge_frequency: FloatProperty(name='Count',default=8,min=1,max=64,update=update_blend_graph)
    path: BoolProperty(name='Path', description='Put B in a band through the middle of the patch, with A on both sides '
                       '(a cobble path with grass either side). Position is the centre line', default=False,
                       update=update_blend_graph)
    path_width: FloatProperty(name='Path Width', description='Width of the B band as a fraction of the patch',
                              default=.3, min=0., max=1., update=update_blend_graph)
    path_width_percent: FloatProperty(name='Path Width', description='Width of the B band as a percentage of the patch',
        subtype='PERCENTAGE', min=0, max=100, get=lambda self: self.path_width*100,
        set=lambda self, value: setattr(self, 'path_width', value/100))
    # Material-owned controls only record it: the face UVs carry the layout, so a Separate Material blend changes
    # it by applying again. Shared patches switch live (SprotileBlendPatch.tiles).
    tiles: EnumProperty(name='Tiles', items=TILE_MODES, default='PATCH')
    alpha_shaping: BoolProperty(name='Painted Alpha', description='Let the Tint attribute\'s alpha bend the blend edge: paint alpha '
                                'below 1 where B should push out (experimental; Unreal reads the same alpha)', default=False,
                                update=update_blend_graph)
    alpha_strength: FloatProperty(name='Strength', description='How far fully erased alpha pushes the edge, in patch widths',
                                  default=2.0, min=0., max=8., update=update_blend_graph)


def read_rectangle_corners(mesh,bm,faces):
    layer=bm.verts.layers.int.get(RECTANGLE_CORNER_LAYER)
    if layer is None or not mesh.get(RECTANGLE_CORNER_TAG):
        raise ValueError('Assign BL, BR, TR and TL in Vertex Select mode before using marked corners')
    found={}
    for vertex in {v for face in faces for v in face.verts}:
        corner=vertex[layer]
        if corner:
            if corner not in (1,2,3,4) or corner in found:
                raise ValueError('Corner markers are duplicated or invalid; clear and reassign them')
            found[corner]=vertex
    if len(found)!=4:raise ValueError('The selected patch must include all four marked corners')
    return tuple(found[i] for i in (1,2,3,4))


def draw_rectangle_corners():
    context=bpy.context
    try:
        if (context.area is None or context.area.type!='VIEW_3D' or context.region is None
                or context.region.type!='WINDOW' or context.scene.sprotile_rectangle_corner_mode!='MARKED'
                or not is_editable_mesh(context.active_object)):return
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        obj=context.active_object;bm=bmesh.from_edit_mesh(obj.data);layer=bm.verts.layers.int.get(RECTANGLE_CORNER_LAYER)
        if layer is None or not obj.data.get(RECTANGLE_CORNER_TAG):return
        # region_data is the view being drawn; space_data.region_3d is always
        # the camera quad, which misplaced labels in the other Quad View views.
        view=context.region_data or context.space_data.region_3d
        for vertex in bm.verts:
            corner=vertex[layer]
            if corner not in (1,2,3,4) or vertex.hide:continue
            point=location_3d_to_region_2d(context.region,view,obj.matrix_world@vertex.co)
            if point is None:continue
            blf_size(0,13);blf.color(0,1.,.72,.18,1.);blf.position(0,point.x+7,point.y+7,0)
            blf.draw(0,('BL','BR','TR','TL')[corner-1])
    except (ReferenceError,RuntimeError,AttributeError):return


def ensure_corner_overlay():
    if state._corner_draw_handle is None and not bpy.app.background:
        state._corner_draw_handle=bpy.types.SpaceView3D.draw_handler_add(draw_rectangle_corners,(),'WINDOW','POST_PIXEL')


class MESH_OT_sprotile_mark_corner(bpy.types.Operator):
    """Assign the active vertex to a tile corner; then select the patch faces and map"""
    bl_idname='mesh.sprotile_mark_corner';bl_label='Mark Rectangle Corner';bl_options={'REGISTER','UNDO'}
    # SKIP_SAVE: the corner buttons never set clear. Without it, one Clear
    # Corner Marks click made every later BL/BR/TR/TL click clear instead.
    # HIDDEN keeps the redo panel closed; REGISTER keeps F9 from undoing it.
    corner: IntProperty(default=1,min=1,max=4,options={'HIDDEN','SKIP_SAVE'})
    clear: BoolProperty(default=False,options={'HIDDEN','SKIP_SAVE'})
    @classmethod
    def poll(cls,context):return is_editable_mesh(context.active_object)
    def execute(self,context):
        obj=context.active_object;mesh=obj.data;bm=bmesh.from_edit_mesh(mesh)
        layer=bm.verts.layers.int.get(RECTANGLE_CORNER_LAYER)
        if mesh.attributes.get(RECTANGLE_CORNER_LAYER) is not None and (layer is None or not mesh.get(RECTANGLE_CORNER_TAG)):
            self.report({'WARNING'},'Reserved corner attribute already exists; rename that user attribute first');return {'CANCELLED'}
        if self.clear:
            if layer is not None:bm.verts.layers.int.remove(layer)
            context.scene.sprotile_rectangle_corner_mode='AUTO'
        else:
            vertex=bm.select_history.active
            if not isinstance(vertex,bmesh.types.BMVert) or not vertex.select or vertex.hide:
                vertices=[v for v in bm.verts if v.select and not v.hide]
                if len(vertices)!=1:
                    self.report({'WARNING'},'In Vertex Select mode, select one vertex or make the intended vertex active');return {'CANCELLED'}
                vertex=vertices[0]
            bm.verts.index_update();index=vertex.index
            if layer is None:layer=bm.verts.layers.int.new(RECTANGLE_CORNER_LAYER)
            bm.verts.ensure_lookup_table()
            for v in bm.verts:
                if v[layer]==self.corner:v[layer]=0
            bm.verts[index][layer]=self.corner;mesh[RECTANGLE_CORNER_TAG]=1
            context.scene.sprotile_rectangle_corner_mode='MARKED';context.scene.sprotile_patch_fit='RECTANGLE'
            self.report({'INFO'},('BL','BR','TR','TL')[self.corner-1]+' assigned; mark the other corners, then select patch faces')
        bmesh.update_edit_mesh(mesh,loop_triangles=False,destructive=False);ensure_corner_overlay();tag_redraw_view3d();return {'FINISHED'}


class SprotileBlendPreset(bpy.types.PropertyGroup):
    name: StringProperty(name='Preset name',default='Blend preset')
    uid: StringProperty()
    preview_data: StringProperty()
    a: PointerProperty(type=SprotileBlendTile)
    b: PointerProperty(type=SprotileBlendTile)
    settings: PointerProperty(type=SprotileBlendControls)
    material_mode: EnumProperty(items=[('SHARED','Shared Material',''),('SEPARATE','Separate Material','')],default='SHARED')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import RECTANGLE_CORNER_LAYER, RECTANGLE_CORNER_TAG, material_nodes
from .draw import blf_size, tag_redraw_view3d
from .tiles import is_editable_mesh
from .blend_shader import edge_node, edge_profile_nodes, is_path, update_separate_overlay
from .blend_storage import mask_socket
