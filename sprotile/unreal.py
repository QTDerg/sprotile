"""Unreal Export: UV map order, the material instance report and Prepare UVs for Unreal.
"""

import bmesh
import bpy
from array import array
# Needed while this module loads (class bodies).
from .blend_settings import BLEND_B_UV, SHARED_EDGE_UV, SHARED_FACTOR_UV


# ---------------------------------------------------------------------------
# Unreal: UV order and material settings for the Sprotile master material
# ---------------------------------------------------------------------------

# After the render map (UV0, which holds tile A on blended faces). Edge is exported only when grass or snow uses it.
UNREAL_UV_ORDER = (BLEND_B_UV, SHARED_FACTOR_UV, SHARED_EDGE_UV)
UNREAL_OVERLAY_SLOTS = 8


def _blend_data(mesh):
    """What the Unreal report needs from a mesh, read from the edit BMesh in Edit Mode (Mesh arrays are empty
    there) or from the Mesh otherwise: per face the material slot, patch id, Separate Material history, patch flags
    and Factor V of its first corner, and whether any corner carries an Edge amount."""
    def face_ints_object(name):
        attribute = mesh.attributes.get(name)
        if attribute is None or attribute.domain != 'FACE' or attribute.data_type != 'INT':
            return None
        values = array('i', [0]) * len(mesh.polygons)
        attribute.data.foreach_get('value', values)
        return values
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        ints, uvs = bm.faces.layers.int, bm.loops.layers.uv
        def face_ints(name):
            layer = ints.get(name)
            return None if layer is None else [face[layer] for face in bm.faces]
        factor, edge = uvs.get(SHARED_FACTOR_UV), uvs.get(SHARED_EDGE_UV)
        return dict(count=len(bm.faces), slots=[face.material_index for face in bm.faces],
                    patch=face_ints(SHARED_PATCH_ID), previous=face_ints(BLEND_ORIGINAL_MATERIAL),
                    flags=face_ints(SHARED_PATCH_FLAGS),
                    modes=[face.loops[0][factor].uv.y for face in bm.faces] if factor is not None else None,
                    edge_used=edge is not None and any(loop[edge].uv.y for face in bm.faces for loop in face.loops))
    count = len(mesh.polygons)
    slots, starts = array('i', [0]) * count, array('i', [0]) * count
    mesh.polygons.foreach_get('material_index', slots)
    mesh.polygons.foreach_get('loop_start', starts)
    modes = None
    if SHARED_FACTOR_UV in mesh.uv_layers:
        factor = array('f', [0.0]) * (2 * len(mesh.loops))
        mesh.uv_layers[SHARED_FACTOR_UV].data.foreach_get('uv', factor)
        modes = [factor[2*start+1] for start in starts]
    edge_used = False
    if SHARED_EDGE_UV in mesh.uv_layers:
        edge = array('f', [0.0]) * (2 * len(mesh.loops))
        mesh.uv_layers[SHARED_EDGE_UV].data.foreach_get('uv', edge)
        edge_used = any(edge[1::2])
    return dict(count=count, slots=slots, patch=face_ints_object(SHARED_PATCH_ID),
                previous=face_ints_object(BLEND_ORIGINAL_MATERIAL), flags=face_ints_object(SHARED_PATCH_FLAGS),
                modes=modes, edge_used=edge_used)


def blended_face_count(mesh, data=None):
    """Faces that show a blend: shared patch faces and Separate Material faces."""
    data = data or _blend_data(mesh)
    found = [values for values in (data['patch'], data['previous']) if values is not None]
    return sum(any(values[index] > 0 for values in found) for index in range(data['count'])) if found else 0


def edge_in_use(mesh, data=None):
    """Whether any corner carries a grass or snow amount (Edge V), so Unreal needs the Edge map."""
    return (data or _blend_data(mesh))['edge_used']


def owned_unreal_maps(mesh):
    if SHARED_FACTOR_UV not in mesh.uv_layers:
        return ()
    bm = bmesh.from_edit_mesh(mesh) if mesh.is_editmode else None
    version = blend_mesh_version(mesh, bm)
    if version >= 3:
        return UNREAL_UV_ORDER
    if version == 2:
        return (BLEND_B_UV, SHARED_FACTOR_UV)
    return ()


def unreal_uv_order(mesh, data=None):
    """The maps Unreal's Sprotile material reads, in order: render map (A), B, Factor, and Edge when used.
    Other user maps follow; an unused Edge map is left out (Prepare removes it)."""
    names = [layer.name for layer in mesh.uv_layers]
    render = render_uv_name(mesh)
    owned = owned_unreal_maps(mesh)
    if not owned:
        return names
    used = SHARED_EDGE_UV in owned and edge_in_use(mesh, data)
    wanted = [render] + [name for name in owned if name in names and (name != SHARED_EDGE_UV or used)]
    return wanted + [name for name in names if name != render and name not in owned]


def unreal_report(mesh):
    """What a mesh needs before FBX export, or None when it has no shared blends."""
    names = [layer.name for layer in mesh.uv_layers]
    if mesh_is_legacy(mesh):
        names = [name for name in names if name not in (LEGACY_A_UV, LEGACY_MASK_UV)]
        legacy = True
    else:
        legacy = False
    owned = owned_unreal_maps(mesh)
    if not owned:
        return None
    if render_uv_name(mesh) in owned:
        return dict(problem=f'{mesh.name}: set the atlas UV map as the render map before preparing UVs')
    data = _blend_data(mesh)
    if not blended_face_count(mesh, data):
        leftovers = [name for name in names if name in owned]
        return dict(names=names, order=[name for name in names if name not in owned], ordered=False,
                    too_many=False, shaped=False, overlays=[], leftover=leftovers, legacy=legacy)
    order = unreal_uv_order(mesh, data)
    if legacy:
        order = [name for name in order if name not in (LEGACY_A_UV, LEGACY_MASK_UV)]
    # Read the factor V that the shader reads: removed blends can leave stale face flags.
    slots, modes = data['slots'], data['modes'] or [0.0] * data['count']
    used = {(slot, round(mode)) for slot, mode in zip(slots, modes) if mode > FIRST_OVERLAY_CODE - .5}
    shaped = False
    if data['flags'] is not None and data['patch'] is not None:
        shaped = any(value & 48 and identifier > 0 and .5 < mode < FIRST_OVERLAY_CODE - .5
                     for value, identifier, mode in zip(data['flags'], data['patch'], modes))
    overlays = []
    alpha = {}
    for index, material in enumerate(mesh.materials):
        if material is None or not material.get(SHARED_MATERIAL_TAG):
            continue
        strength = shared_alpha_strength(material)
        if strength > 0:
            alpha[material.name] = strength
        try:
            bank = shared_overlay_bank(material)
        except ValueError:
            bank = {}
        for code, sampler in sorted(bank.items()):
            overlays.append((material.name, code - FIRST_OVERLAY_CODE + 1,
                             sampler.image.name if sampler.image else '(missing)', (index, code) in used))
    return dict(names=names, order=order, ordered=not legacy and names == order, too_many=len(order) > 8,
                shaped=shaped, overlays=overlays, leftover=[], legacy=legacy, alpha=alpha,
                tint=tint_export_problem(mesh) if alpha else None)


def tint_export_problem(mesh):
    """Why painted alpha would not reach Unreal as things stand: (is an error, text), or None when it will.

    Unreal reads the first vertex colour set of the FBX. Blender's FBX export writes the colour attributes in the
    order of the mesh's list, or the active one first when its Prioritize Active Color option is ticked, so Tint
    has to be the first colour attribute or the active one."""
    colours = mesh.color_attributes
    tint = colours.get('Tint')
    if tint is None:
        return True, 'No Tint colour attribute: nothing is painted yet'
    if tint.domain not in {'CORNER', 'POINT'}:
        return True, 'Tint is not a colour attribute Blender exports'
    if len(colours) and colours[0].name == 'Tint':
        return None
    active = colours.active_color
    if active is not None and active.name == 'Tint':
        return False, 'FBX export: tick Prioritize Active Color'
    return True, 'Tint is neither the first nor the active colour'


def strip_leftover_blend_maps(mesh):
    """A mesh whose blends were all removed exports as a plain mesh: drop B, Factor and Edge."""
    removed = False
    for name in owned_unreal_maps(mesh):
        if name in mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[name])
            removed = True
    if removed:
        mesh.update()
    return removed


def reorder_uvs_for_unreal(mesh):
    """Rewrite the UV maps in Unreal's order, dropping an unused Edge map. Values follow names.

    Returns True when the maps changed."""
    upgraded = False
    owned = owned_unreal_maps(mesh)
    if not owned:
        return False
    if render_uv_name(mesh) in owned:
        raise ValueError(f'{mesh.name}: set the atlas UV map as the render map before preparing UVs')
    if mesh_is_legacy(mesh):
        upgrade_file_blends()
        upgraded = True
    if SHARED_FACTOR_UV in mesh.uv_layers and not blended_face_count(mesh):
        return strip_leftover_blend_maps(mesh) or upgraded
    names = [layer.name for layer in mesh.uv_layers]
    order = unreal_uv_order(mesh)
    if names == order:
        return upgraded
    if len(order) > 8:
        raise ValueError(mesh.name + ' would need more than eight UV maps; remove an unused map first')
    render, active = render_uv_name(mesh), mesh.uv_layers.active.name if mesh.uv_layers.active else None
    clone = names[mesh.uv_layer_clone_index]
    stencil = names[mesh.uv_layer_stencil_index]
    count = 2 * len(mesh.loops)
    values = {}
    flags = {}
    for name in order:
        buffer = array('f', [0.0]) * count
        mesh.uv_layers[name].data.foreach_get('uv', buffer)
        values[name] = buffer
        # Blender stores UV pins as corner booleans alongside the coordinates.
        # 4.4 also stores UV vertex/edge selection per map. 5.x shares that selection across maps.
        for prefix in ('.pn.', '.vs.', '.es.'):
            attribute = mesh.attributes.get(prefix + name)
            if attribute is not None and (attribute.domain, attribute.data_type) == ('CORNER', 'BOOLEAN'):
                bits = array('b', [0]) * len(mesh.loops)
                attribute.data.foreach_get('value', bits)
                flags[attribute.name] = bits
    # Remove every map, then recreate them in order. Look layers up by name after each
    # change: Blender 4.x reallocates attribute storage, which empties older references.
    while len(mesh.uv_layers):
        mesh.uv_layers.remove(mesh.uv_layers[0])
    for name in order:
        mesh.uv_layers.new(name=name, do_init=False)
        layer = mesh.uv_layers[name]
        layer.data.foreach_set('uv', values[name])
    for name, bits in flags.items():
        attribute = mesh.attributes.get(name)
        if attribute is None:
            attribute = mesh.attributes.new(name, 'BOOLEAN', 'CORNER')
        attribute.data.foreach_set('value', bits)
    mesh.uv_layers[render].active_render = True
    mesh.uv_layers.active = mesh.uv_layers[active if active in mesh.uv_layers else render]
    mesh.uv_layer_clone_index = order.index(clone if clone in order else render)
    mesh.uv_layer_stencil_index = order.index(stencil if stencil in order else render)
    mesh.update()
    return True


class MESH_OT_sprotile_prepare_unreal(bpy.types.Operator):
    """Put the selected meshes' UV maps in the order Unreal's Sprotile material reads: render map (texture A), B, Factor, and Edge only when grass or snow uses it"""
    bl_idname = 'mesh.sprotile_prepare_unreal'
    bl_label = 'Prepare UVs for Unreal'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' and obj.library is None and obj.data.library is None
                   for obj in unreal_objects(context))

    def execute(self, context):
        objects = [obj for obj in unreal_objects(context) if obj.type == 'MESH']
        meshes = {obj.data.as_pointer(): obj.data for obj in objects if obj.data.library is None}
        editing = [obj for obj in objects if obj.mode == 'EDIT']
        if editing:
            bpy.ops.object.mode_set(mode='OBJECT')
        changed, skipped, plain = 0, 0, 0
        try:
            reports = [(mesh, unreal_report(mesh)) for mesh in meshes.values()]
            for mesh, report in reports:
                if report and report.get('problem'):
                    raise ValueError(report['problem'])
            for mesh, report in reports:
                if report is None:
                    skipped += 1
                    continue
                plain += bool(report['leftover'])
                changed += reorder_uvs_for_unreal(mesh)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        finally:
            if editing:
                bpy.ops.object.mode_set(mode='EDIT')
        ready = len(meshes) - skipped
        if not ready:
            self.report({'INFO'}, 'No shared Sprotile blends on the selected meshes; nothing to prepare')
        else:
            text = f'{ready} mesh(es) ready for Unreal; {changed} changed'
            if plain:
                text += f'; {plain} without blends now export as plain meshes (use the plain material)'
            self.report({'INFO'}, text)
        tag_redraw_view3d()
        return {'FINISHED'}


def unreal_objects(context):
    selected = list(getattr(context, 'selected_objects', ()) or ())
    active = context.active_object
    if active is not None and active not in selected:
        selected.append(active)
    return selected


class VIEW3D_PT_sprotile_unreal(bpy.types.Panel):
    bl_label = 'Unreal Export'
    bl_idname = 'VIEW3D_PT_sprotile_unreal'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return feature_enabled(context, 'show_unreal')

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            layout.label(text='Select a mesh', icon='INFO')
            return
        mesh = obj.data
        report = draw_cached('unreal', mesh, lambda: unreal_report(mesh))
        if report is None:
            layout.label(text='No shared blends: export as usual', icon='CHECKMARK')
            return
        if report.get('problem'):
            layout.label(text=report['problem'], icon='ERROR')
            return
        if report['leftover']:
            box = layout.box()
            box.label(text='No blends left on this mesh', icon='INFO')
            box.label(text='Prepare removes the leftover blend maps')
            layout.operator('mesh.sprotile_prepare_unreal', icon='EXPORT')
            return
        box = layout.box()
        if report['too_many']:
            box.label(text='More than 8 UV maps; remove one', icon='ERROR')
        elif report['legacy']:
            box.label(text='Saved by Sprotile 5.12: Prepare upgrades it', icon='ERROR')
        elif report['ordered']:
            box.label(text='UV maps in Unreal order', icon='CHECKMARK')
        else:
            box.label(text='UV maps need reordering', icon='ERROR')
        roles = {render_uv_name(mesh): 'texture A', BLEND_B_UV: 'texture B', SHARED_FACTOR_UV: 'blend',
                 SHARED_EDGE_UV: 'grass / snow'}
        sprotile = sum(name in roles for name in report['order'])
        for index, name in enumerate(report['order'][:sprotile]):
            box.label(text=f'UV{index}: {name} ({roles[name]})')
        layout.operator('mesh.sprotile_prepare_unreal', icon='EXPORT')
        settings = layout.box()
        settings.label(text='Set on the Unreal material instance:')
        # Sprotile Blends defaults to 1 on the 5.13 master, which also draws meshes without blends.
        needed = False
        if report['shaped']:
            settings.label(text='Sprotile Shaped Edges = 1')
            settings.label(text='(import without generated lightmap UVs)')
            needed = True
        materials = {entry[0] for entry in report['overlays'] if entry[3]}
        for material, slot, image, used in report['overlays']:
            if not used:
                continue
            settings.label(text=(material + ': ' if len(materials) > 1 else '') + f'Overlay {slot} = {image}',
                           icon='IMAGE_DATA')
            if slot > UNREAL_OVERLAY_SLOTS:
                settings.label(text=f'Unreal has {UNREAL_OVERLAY_SLOTS} overlay slots', icon='ERROR')
            needed = True
        alpha = report.get('alpha') or {}
        for material, strength in sorted(alpha.items()):
            settings.label(text=(material + ': ' if len(alpha) > 1 else '') + f'Sprotile Alpha Strength = {strength:g}')
            needed = True
        if alpha:
            settings.label(text='(export with Vertex Colors, Tint active)')
            if report.get('tint'):
                problem, text = report['tint']
                warn = settings.column(align=True)
                warn.alert = problem
                warn.label(text=text, icon='ERROR' if problem else 'INFO')
                warn.label(text='Painted B would not show in Unreal' if problem
                           else '(Tint is not the first colour attribute)')
        if not needed:
            settings.label(text='Nothing to set: the defaults work', icon='CHECKMARK')
        unused = sum(not entry[3] for entry in report['overlays'])
        if unused:
            settings.label(text=f'{unused} overlay source(s) unused by this mesh: leave those slots empty')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import feature_enabled
from .draw import tag_redraw_view3d
from .blend_shader import shared_overlay_bank
from .blend_alpha import shared_alpha_strength
from .blend_list import draw_cached
from .blend_settings import (
    BLEND_ORIGINAL_MATERIAL, FIRST_OVERLAY_CODE, LEGACY_A_UV, LEGACY_MASK_UV, SHARED_MATERIAL_TAG,
    SHARED_PATCH_FLAGS, SHARED_PATCH_ID,
)
from .blend_storage import mesh_is_legacy, render_uv_name, upgrade_file_blends
from .blend_plan import blend_mesh_version
