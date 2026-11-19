"""Painted-alpha shaping (experimental, 5.14).

The Tint colour attribute's alpha channel bends a blend's edge by hand: paint alpha below 1 where tile B
should push further out, and the fade follows the brush. Alpha 1 (the default everywhere) changes nothing,
so meshes that were never painted render exactly as before, in Blender and in Unreal.

How it enters the shader: after the path band and before the grass or snow tufts, the factor base x becomes
x + (1 - alpha) * strength. Unreal's SprotileBlend.hlsl does the same with the vertex colour's alpha, so the
FBX export must carry Tint (Vertex Colors on, Tint the active colour attribute), as BHR exports already do.
"""

import bpy
import bmesh
from array import array
from bpy.props import BoolProperty, FloatProperty

ALPHA_STRENGTH_DEFAULT = 2.0
TINT_ATTRIBUTE = 'Tint'


def alpha_consumers(roles):
    """Inputs that read the shaped factor: the tuft chain's base, or the two gradients without tufts."""
    if 'edge_result' in roles:
        return [roles['edge_result'].inputs[0]]
    return [roles[name].inputs['Value'] for name in ('gradient_linear', 'gradient_smoothstep')]


def alpha_source(roles):
    """What the alpha stage reads: the path band when it exists, else Factor U."""
    return roles['path_result'].outputs[0] if 'path_result' in roles else roles['factor_axes'].outputs['X']


def validate_shared_alpha(material):
    roles = shared_nodes(material)
    required = {'alpha_tint', 'alpha_inverse', 'alpha_scaled', 'alpha_result'}
    if not required.issubset(roles):
        raise ValueError('Painted alpha shader support is missing; Apply a blend with Painted Alpha on')
    tint, inverse, scaled, result = (roles[n] for n in ('alpha_tint', 'alpha_inverse', 'alpha_scaled', 'alpha_result'))
    if (tint.type != 'VERTEX_COLOR' or tint.layer_name != TINT_ATTRIBUTE or tint.mute
            or inverse.type != 'MATH' or inverse.operation != 'SUBTRACT' or inverse.mute
            or scaled.type != 'MATH' or scaled.operation != 'MULTIPLY' or scaled.mute
            or result.type != 'MATH' or result.operation != 'ADD' or result.mute or result.use_clamp
            or linked_output(inverse.inputs[1]) != tint.outputs['Alpha']
            or inverse.inputs[0].is_linked or inverse.inputs[0].default_value != 1.0
            or linked_output(scaled.inputs[0]) != inverse.outputs[0] or scaled.inputs[1].is_linked
            or linked_output(result.inputs[0]) != alpha_source(roles)
            or linked_output(result.inputs[1]) != scaled.outputs[0]):
        raise ValueError('Painted alpha shader nodes were edited; restore them before applying')
    if any(len(s.links) != 1 or s.links[0].from_socket != result.outputs[0] for s in alpha_consumers(roles)):
        raise ValueError('Painted alpha shader connections were edited; restore them before applying')


def check_shared_alpha_support(material):
    """Raise ValueError if ensure_shared_alpha_support would refuse this material; changes nothing."""
    roles = shared_nodes(material)
    if 'alpha_result' in roles:
        validate_shared_alpha(material)
        return roles
    if 'edge_result' in roles:
        validate_shared_grass(material)
    try:
        source = alpha_source(roles)
        destinations = alpha_consumers(roles)
    except KeyError:
        raise ValueError(f'{material.name} is not a complete shared blend material; Apply a blend to it first') from None
    if any(len(s.links) != 1 or s.links[0].from_socket != source for s in destinations):
        raise ValueError('The shared gradient was edited; restore it before adding painted alpha')
    return roles


def ensure_shared_alpha_support(material, strength):
    """Insert the alpha stage between the path/factor base and its readers. Return a rollback journal, or None."""
    roles = check_shared_alpha_support(material)
    if 'alpha_result' in roles:
        roles['alpha_scaled'].inputs[1].default_value = strength
        return None
    source = alpha_source(roles)
    destinations = alpha_consumers(roles)
    links = material.node_tree.links
    journal = dict(material=material, nodes={n.as_pointer() for n in material.node_tree.nodes},
                   links=[(source, d) for d in destinations], active=material.node_tree.nodes.active)
    try:
        key = 'sprotile_shared_role'
        tint = edge_node(material, key, 'ShaderNodeVertexColor', 'alpha_tint')
        tint.layer_name = TINT_ATTRIBUTE
        inverse = edge_node(material, key, 'ShaderNodeMath', 'alpha_inverse')
        inverse.operation = 'SUBTRACT'; inverse.use_clamp = False
        inverse.inputs[0].default_value = 1.0
        links.new(tint.outputs['Alpha'], inverse.inputs[1])
        scaled = edge_node(material, key, 'ShaderNodeMath', 'alpha_scaled')
        scaled.operation = 'MULTIPLY'; scaled.use_clamp = False
        links.new(inverse.outputs[0], scaled.inputs[0])
        scaled.inputs[1].default_value = strength
        result = edge_node(material, key, 'ShaderNodeMath', 'alpha_result')
        result.operation = 'ADD'; result.use_clamp = False
        links.new(source, result.inputs[0])
        links.new(scaled.outputs[0], result.inputs[1])
        for destination in destinations:
            links.new(result.outputs[0], destination)
        material.node_tree.nodes.active = journal['active']
        return journal
    except Exception:
        restore_edge_support(journal)
        raise


def shared_alpha_strength(material):
    """The strength a shared material was built with, or 0 when it has no alpha stage."""
    roles = shared_nodes(material)
    node = roles.get('alpha_scaled')
    return float(node.inputs[1].default_value) if node is not None else 0.0


def mesh_has_tint_alpha(mesh):
    """True when the mesh carries a Tint colour attribute (alpha is painted into it)."""
    return mesh.color_attributes.get(TINT_ATTRIBUTE) is not None


def ensure_tint_attribute(obj):
    """Give the mesh a Tint byte colour attribute (white, alpha 1) when it has none, so alpha can be painted."""
    mesh = obj.data
    if mesh.color_attributes.get(TINT_ATTRIBUTE) is not None:
        return False
    if obj.mode == 'EDIT':
        # A BMesh colour layer would be FLOAT_COLOR; the game's Tint is byte colour, so make it in Object Mode.
        bpy.ops.object.mode_set(mode='OBJECT')
    attribute = mesh.color_attributes.new(TINT_ATTRIBUTE, 'BYTE_COLOR', 'CORNER')
    attribute.data.foreach_set('color', [1.0] * (4 * len(mesh.loops)))
    return True


PAINT_AREA_OVERRIDES = dict(direction='PAINTED', position=0.0, alpha_shaping=True, edge_style='PLAIN', path=False,
                            tiles='FACE', smooth=True)


def apply_paint_area(context, faces_by_mesh):
    """Turn faces into a Painted Only patch: they keep their tile as A, B is the captured tile, and nothing shows
    until Tint alpha is painted. Falls back from Existing UVs to Unfold when a face's own UVs are collapsed.
    The faces are named to Apply directly, so the selection (vertices and edges included) stays as it was."""
    def run(method):
        return apply_shared_texture_blend(
            context, overrides=PAINT_AREA_OVERRIDES, keep_a=True, method=method, fit='FILL', patch_name='Paint area',
            controls=context.scene.sprotile_blend_defaults, faces=faces_by_mesh)
    try:
        return run('EXISTING')
    except ValueError as error:
        if 'Existing UVs' not in str(error):
            raise
        return run('UNFOLD')


def shared_materials_of(obj, faces):
    """The shared blend materials the given faces use."""
    found = []
    for face in faces:
        if face.material_index < len(obj.material_slots):
            material = obj.material_slots[face.material_index].material
            if material is not None and material.get(SHARED_MATERIAL_TAG) and material not in found:
                found.append(material)
    return found


def set_alpha_brush(context, reveal=True):
    """Point the active Vertex Paint brush at Tint alpha: erase shows B, add hides it. False if no brush."""
    brush = context.tool_settings.vertex_paint.brush if context.tool_settings.vertex_paint else None
    if brush is None:
        return False
    try:
        brush.blend = 'ERASE_ALPHA' if reveal else 'ADD_ALPHA'
        if brush.strength < .05 or brush.strength > .99:
            brush.strength = 0.5
    except (AttributeError, TypeError):
        return False
    return True


class MESH_OT_sprotile_paint_alpha(bpy.types.Operator):
    """In Edit Mode: make the selected faces a paint area (their tiles stay as they are) and switch to Vertex Paint,
    where painting shows tile B. Faces already in a blend keep it, and painting bends that blend's edge"""
    bl_idname = 'mesh.sprotile_paint_alpha'
    bl_label = 'Paint B'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        scene = context.scene
        strength = scene.sprotile_blend_defaults.alpha_strength   # 0 is a real choice: painting then shows nothing
        added = reused = 0
        materials = []
        try:
            if obj.mode == 'EDIT':
                # Vertex Paint works on one object, so Paint B prepares only the active one.
                others = 0
                for item in edit_mesh_objects(context):
                    if item.data != mesh:
                        others += sum(1 for face in bmesh.from_edit_mesh(item.data).faces if face.select and not face.hide)
                bm = bmesh.from_edit_mesh(mesh)
                bm.faces.ensure_lookup_table()
                bm.faces.index_update()   # faces made in this Edit Mode session can still read -1
                patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
                selected = [face for face in bm.faces if face.select and not face.hide]
                fresh = [face.index for face in selected if patch is None or not face[patch]]
                kept = [face for face in selected if patch is not None and face[patch]]
                reused = len(kept)
                if not selected:
                    self.report({'WARNING'}, 'Select the faces to paint on first'
                                + (' (Paint B works on the active object)' if others else ''))
                    return {'CANCELLED'}
                if fresh and scene.sprotile_blend_b.image is None:
                    self.report({'WARNING'}, 'Capture B first: the tile that painting shows')
                    return {'CANCELLED'}
                # Check every material before changing anything, so a refusal leaves the mesh as it was.
                materials = shared_materials_of(obj, kept)
                for material in materials:
                    check_shared_alpha_support(material)
                if fresh:
                    apply_paint_area(context, {mesh.as_pointer(): fresh})
                    added = len(fresh)
                    bm = bmesh.from_edit_mesh(mesh)
                    bm.faces.ensure_lookup_table()
                    materials += shared_materials_of(obj, [bm.faces[i] for i in fresh])
                if others:
                    self.report({'WARNING'}, f'{others} selected face{"s" if others != 1 else ""} on other objects left '
                                'alone: Paint B works on the active object')
            else:
                patch = mesh.attributes.get(SHARED_PATCH_ID)
                ids = array('i', [0]) * len(mesh.polygons)
                if (patch is not None and patch.domain == 'FACE' and patch.data_type == 'INT'
                        and len(patch.data) == len(ids)):
                    patch.data.foreach_get('value', ids)
                if not any(ids):
                    self.report({'WARNING'}, 'Nothing to paint on yet: in Edit Mode select the faces, then click Paint B')
                    return {'CANCELLED'}
                slots = array('i', [0]) * len(ids)
                mesh.polygons.foreach_get('material_index', slots)
                used = sorted({slot for slot, value in zip(slots, ids) if value})
                materials = [obj.material_slots[slot].material for slot in used if slot < len(obj.material_slots)]
                materials = [m for m in dict.fromkeys(materials) if m is not None and m.get(SHARED_MATERIAL_TAG)]
            for material in dict.fromkeys(materials):
                ensure_shared_alpha_support(material, strength)
        except (ValueError, RuntimeError) as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        made = ensure_tint_attribute(obj)
        mesh.color_attributes.active_color = mesh.color_attributes[TINT_ATTRIBUTE]
        if obj.mode != 'VERTEX_PAINT':
            bpy.ops.object.mode_set(mode='VERTEX_PAINT')
        note = (f'{added} face{"s" if added != 1 else ""} ready. ' if added else '') + ('Added Tint. ' if made else '')
        if reused:
            # Otherwise a newly captured B seems to be ignored: these faces show the B of the blend they are in.
            note += (f'{reused} face{"s" if reused != 1 else ""} already in a blend keep that blend\'s B '
                     '(Remove Blend first to paint the captured B there). ')
        if set_alpha_brush(context, True):
            self.report({'INFO'}, note + 'Paint to show B. Hide B in the Sprotile panel switches the brush to take it back')
        else:
            self.report({'WARNING'}, note + 'Set the brush Blend to Erase Alpha yourself; a normal stroke repaints the Tint colour')
        return {'FINISHED'}


class MESH_OT_sprotile_alpha_brush(bpy.types.Operator):
    """Switch the Vertex Paint brush between showing B (Erase Alpha) and hiding it again (Add Alpha)"""
    bl_idname = 'mesh.sprotile_alpha_brush'
    bl_label = 'Paint B Brush'
    bl_options = {'REGISTER'}
    reveal: BoolProperty(default=True, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.mode == 'VERTEX_PAINT'

    def execute(self, context):
        mesh = context.active_object.data
        tint = mesh.color_attributes.get(TINT_ATTRIBUTE)
        if tint is not None:
            mesh.color_attributes.active_color = tint
        if not set_alpha_brush(context, self.reveal):
            self.report({'WARNING'}, 'No Vertex Paint brush is active; pick one in the tool header')
            return {'CANCELLED'}
        return {'FINISHED'}


class MESH_OT_sprotile_clear_alpha(bpy.types.Operator):
    """Set Tint alpha back to 1 (nothing painted) on the selected faces, or on the whole mesh when none are
    selected; the Tint colour stays"""
    bl_idname = 'mesh.sprotile_clear_alpha'
    bl_label = 'Clear Painting'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.data.color_attributes.get(TINT_ATTRIBUTE) is not None

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        count = 0
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            layer = bm.loops.layers.color.get(TINT_ATTRIBUTE) or bm.loops.layers.float_color.get(TINT_ATTRIBUTE)
            if layer is None:
                self.report({'WARNING'}, 'Tint is not a corner colour attribute')
                return {'CANCELLED'}
            faces = [face for face in bm.faces if face.select and not face.hide] or [f for f in bm.faces if not f.hide]
            for face in faces:
                for loop in face.loops:
                    colour = loop[layer]
                    if colour[3] != 1.0:
                        colour[3] = 1.0
                        loop[layer] = colour
                        count += 1
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        else:
            tint = mesh.color_attributes[TINT_ATTRIBUTE]
            if tint.domain != 'CORNER' and tint.domain != 'POINT':
                self.report({'WARNING'}, 'Tint is not a corner or vertex colour attribute')
                return {'CANCELLED'}
            # One read of every alpha finds the painted corners (a handful on a level mesh of 100,000), and only
            # those are written, each on its own: writing the whole array back would round-trip every byte colour.
            total = len(tint.data)
            colours = array('f', [0.0]) * (4 * total)
            tint.data.foreach_get('color', colours)
            painted = [index for index in range(total) if colours[4 * index + 3] != 1.0]
            if painted and mesh.use_paint_mask:
                selected = [face for face in mesh.polygons if face.select and not face.hide]
                if selected:
                    if tint.domain == 'CORNER':
                        allowed = {index for face in selected for index in face.loop_indices}
                    else:
                        allowed = {vertex for face in selected for vertex in face.vertices}
                    painted = [index for index in painted if index in allowed]
            for index in painted:
                colour = tint.data[index].color
                tint.data[index].color = (colour[0], colour[1], colour[2], 1.0)
                count += 1
            mesh.update()
        self.report({'INFO'}, f'Cleared painting on {count} corner{"s" if count != 1 else ""}')
        return {'FINISHED'}


def alpha_brush_reveals(context):
    brush = context.tool_settings.vertex_paint.brush if context.tool_settings.vertex_paint else None
    return None if brush is None else brush.blend == 'ERASE_ALPHA' if brush.blend in {'ERASE_ALPHA', 'ADD_ALPHA'} else None


def draw_paint_mode_controls(layout, context):
    """While in Vertex Paint: which way the brush works, and whether it is limited to the selected faces."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH' or obj.mode != 'VERTEX_PAINT':
        return False
    box = layout.box()
    box.label(text='Painting Tint alpha', icon='BRUSH_DATA')
    state = alpha_brush_reveals(context)
    row = box.row(align=True)
    row.operator('mesh.sprotile_alpha_brush', text='Show B', depress=state is True).reveal = True
    row.operator('mesh.sprotile_alpha_brush', text='Hide B', depress=state is False).reveal = False
    if state is None:
        box.label(text='The brush paints colour, not alpha', icon='ERROR')
    active = obj.data.color_attributes.active_color
    if active is None or active.name != TINT_ATTRIBUTE:
        box.label(text='Tint is not the active colour attribute', icon='ERROR')
    row = box.row(align=True)
    row.prop(obj.data, 'use_paint_mask', text='Only Selected Faces')
    row.operator('mesh.sprotile_clear_alpha', text='Clear', icon='X')
    box.label(text='B shows only on blended faces', icon='INFO')
    return True


def draw_alpha_controls(layout, settings, context, paint_task=False):
    """The Painted Alpha controls inside the Texture Blend panel. paint_task: the Paint B Quick start."""
    box = layout.box()
    if paint_task:
        box.prop(settings, 'alpha_strength', text='Strength')
        row = box.row()
        row.scale_y = 1.4
        row.operator('mesh.sprotile_paint_alpha', text='Paint B on Selected Faces', icon='BRUSH_DATA')
        draw_blocker(box, action_blocker(context, 'paint_b'))
    else:
        row = box.row(align=True)
        row.prop(settings, 'alpha_shaping', text='Painted Alpha (experimental)')
        if settings.alpha_shaping:
            box.prop(settings, 'alpha_strength', text='Strength')
            box.operator('mesh.sprotile_paint_alpha', text='Paint Blend Edge', icon='BRUSH_DATA')
    obj = context.active_object
    if obj is not None and obj.type == 'MESH' and not mesh_has_tint_alpha(obj.data):
        box.label(text='No Tint attribute yet: painting adds one', icon='INFO')


# Names from other Sprotile modules, used when functions run (see __init__.py).
from .blend_shader import edge_node, linked_output, restore_edge_support, shared_nodes, validate_shared_grass
from .blend_settings import SHARED_MATERIAL_TAG, SHARED_PATCH_ID
from .tiles import edit_mesh_objects
from .hints import action_blocker, draw_blocker
from .blend_shared import apply_shared_texture_blend
