"""The Texture Blend panel.
"""

import bpy
import bmesh
import math


def draw_blend_inputs(layout,scene,can_copy=True):
    if can_copy:
        layout.operator('mesh.sprotile_pick_blend',text='Copy Blend from Active Face',icon='EYEDROPPER')
    row=layout.row(align=True)
    row.operator('mesh.sprotile_capture_blend_tile',text='Capture A',icon='EYEDROPPER').slot='A'
    row.operator('mesh.sprotile_capture_blend_tile',text='Capture B',icon='EYEDROPPER').slot='B'
    row.operator('mesh.sprotile_swap_blend_tiles',text='',icon='ARROW_LEFTRIGHT')
    a,b=scene.sprotile_blend_a,scene.sprotile_blend_b
    if not a.image and not b.image:
        return
    box=layout.box()
    if a.image and a.image==b.image:box.label(text=a.image.name,icon='IMAGE_DATA')
    thumbs=box.row(align=True)
    for slot in ('A','B'):
        record=getattr(scene,'sprotile_blend_'+slot.lower());col=thumbs.column(align=True)
        if record.image:
            whole = record.is_sampled and tuple(record.sample_uv) == (1,0,0,0,1,0)
            external_overlay = slot == 'B' and scene.sprotile_blend_defaults.edge_style == 'OVERLAY' and a.image != b.image and whole
            label = 'full image' if whole else 'copied'
            if record.is_sampled:
                text = slot + ': ' + label
            else:
                text = f'{slot}: ({record.col}, {record.row})'
                rotation = int(record.rotation) % 360
                if not transform_is_default(rotation, record.flip_u, record.flip_v):
                    text += ' ' + transform_label(rotation, record.flip_u, record.flip_v)
            col.label(text=text)
            icon=clipboard_preview_icon(scene,slot.lower())
            if icon:col.template_icon(icon_value=icon.icon_id,scale=3)
            draw_turn_buttons(col, 'mesh.sprotile_turn_blend_tile', slot, stacked=True)
            if not external_overlay:
                col.operator('mesh.sprotile_focus_atlas',text='Find '+slot,icon='VIEWZOOM').target=slot
        else:col.label(text='Capture '+slot,icon='IMAGE_DATA')
    if a.image!=b.image:
        for slot,record in (('A',a),('B',b)):
            if record.image:box.label(text=slot+': '+record.image.name,icon='IMAGE_DATA')
    if a.image and a.image==b.image and a.is_sampled==b.is_sampled and (
            tuple(a.sample_uv)==tuple(b.sample_uv) if a.is_sampled else
            (a.col,a.row,a.width_px,a.height_px,a.rotation,a.flip_u,a.flip_v,a.inset_px)==(b.col,b.row,b.width_px,b.height_px,b.rotation,b.flip_u,b.flip_v,b.inset_px)):
        box.label(text='A and B are the same tile',icon='INFO')
    if a.is_sampled and b.is_sampled:
        row=layout.row(align=True)
        row.operator('mesh.sprotile_rotate_blend_copy',text='Turn Copy -90°').quarter_turns=-1
        row.operator('mesh.sprotile_rotate_blend_copy',text='Turn Copy +90°').quarter_turns=1
        layout.label(text='Turns both tiles and the transition')


def path_run_text(controls):
    """Which way a path runs: across its Direction, since Direction is the way A turns into B."""
    if controls.direction in {'LEFT_RIGHT', 'RIGHT_LEFT'}:
        return 'The path runs up the patch'
    if controls.direction in {'BOTTOM_TOP', 'TOP_BOTTOM'}:
        return 'The path runs across the patch'
    return f'The path runs at {round(math.degrees(controls.angle) + 90) % 180}°'


def active_patch_copies(context, patch):
    """How many copies share the active blend's patch id (1 normally). Cached until the mesh changes."""
    obj = context.active_object
    if obj is None or obj.data is None or not obj.data.is_editmode:
        return 1
    mesh, patch_id = obj.data, patch.patch_id

    def count():
        bm = bmesh.from_edit_mesh(mesh)
        return max(1, len(blend_copies(mesh, bm, patch_id)))
    return draw_cached('copies ' + str(patch_id), mesh, count)


BLEND_TASKS = [
    ('CUSTOM', 'Custom', 'Set every blend control yourself'),
    ('FADE', 'Fade between two tiles', 'A smooth gradient from tile A to tile B, stretched over the patch'),
    ('EDGE', 'Grass or snow edge', 'Tufts or drifts of A reach into B'),
    ('OVERLAY', 'Overlay cell', 'Lay a cut-out B over A on every face'),
    ('PATH', 'Path', 'A band of B down the middle with A on both sides'),
    ('PAINTED', 'Paint B by hand', 'Brush tile B onto faces in Vertex Paint; the faces keep their own tiles until '
                                   'you paint (experimental)'),
    ('PATH_BRUSH', 'Path brush', 'Drag over faces to make them tile B; the faces around them blend into it, like '
                                 'Spyro\'s path tiles (experimental)'),
    ('SEAM', 'Blend a seam', 'Lay your tiles down first, then select the edges where two tiles meet: the faces on '
                             'both sides fade into each other')]
# The controls each task needs; everything else goes under Advanced. Custom shows all of them.
_TASK_FIELDS = {'FADE': {'feather', 'smooth'}, 'EDGE': {'edge'}, 'OVERLAY': {'opacity', 'overlay_buttons'},
                'PATH': {'path_width', 'feather'}, 'PAINTED': {'alpha'},
                'PATH_BRUSH': {'style', 'edge', 'feather', 'path_brush'}, 'SEAM': {'feather', 'smooth', 'seam'}}


def task_fields(task):
    """The control names a task draws up front, or None for Custom (draw everything)."""
    return _TASK_FIELDS.get(task)


class BlendFeatures:
    """Which optional parts of the panel the add-on preferences leave on."""
    def __init__(self, paths=True, overlays=True):
        self.paths, self.overlays = paths, overlays

    def has_task(self, task):
        return not ((task in {'PATH', 'PATH_BRUSH'} and not self.paths) or (task == 'OVERLAY' and not self.overlays))


def update_blend_task(scene, _context=None):
    """Choosing a Quick start task sets the New Blend settings it stands for; Custom changes nothing."""
    controls = scene.sprotile_blend_defaults
    task = scene.sprotile_blend_task
    if task in {'FADE', 'EDGE', 'OVERLAY', 'PATH'}:
        controls.alpha_shaping = False
    if task == 'FADE':
        controls.edge_style, controls.tiles, controls.path = 'PLAIN', 'PATCH', False
    elif task == 'EDGE':
        controls.edge_style, controls.tiles, controls.path = 'GRASS', 'PATCH', False
    elif task == 'OVERLAY':
        controls.edge_style, controls.tiles, controls.path = 'OVERLAY', 'FACE', False
    elif task == 'PATH':
        controls.edge_style, controls.tiles, controls.path = 'PLAIN', 'PATCH', True
    elif task == 'PAINTED':
        controls.edge_style, controls.tiles, controls.path, controls.alpha_shaping = 'PLAIN', 'FACE', False, True
        scene.sprotile_blend_material_mode = 'SHARED'
    elif task == 'PATH_BRUSH':
        if controls.edge_style == 'OVERLAY':
            controls.edge_style = 'PLAIN'
        controls.tiles, controls.path = 'FACE', False
        scene.sprotile_blend_material_mode = 'SHARED'
    elif task == 'SEAM':
        scene.sprotile_blend_material_mode = 'SHARED'


# What to do after picking a task: one line, in the order the panel's buttons sit.
_TASK_HINTS = {
    'FADE': 'Capture A and B, select the faces, Apply Blend',
    'EDGE': 'A is the grass or snow tile, B the ground; select the faces, Apply Blend',
    'OVERLAY': 'Pick B in the Overlay Browser below (or Open PNG as B), select the faces, Apply Blend',
    'PATH': 'Select the row of faces the path runs along; Capture A and B, Apply Blend',
    'PAINTED': 'Capture B, select the faces in Edit Mode, click Paint B on Selected Faces, then paint',
    'PATH_BRUSH': 'Capture B (the path tile), then Path Brush and drag over the ground',
    'SEAM': 'Map your tiles first. Select the edge loop where two tiles meet (or the faces on both sides of it), '
            'then Blend This Seam. No capture needed'}


def draw_quick_start(layout, scene, allow):
    if allow.paths and allow.overlays:
        field = layout.split(factor=0.33)
        field.label(text='Quick start')
        field.prop(scene, 'sprotile_blend_task', text='')
    else:
        layout.label(text='Quick start')
        col = layout.column(align=True)
        for identifier, _name, _description in BLEND_TASKS:
            if allow.has_task(identifier):
                col.prop_enum(scene, 'sprotile_blend_task', identifier)
    hint = _TASK_HINTS.get(scene.sprotile_blend_task)
    if hint and allow.has_task(scene.sprotile_blend_task):
        draw_hint(layout, hint)


def draw_blend_fields(box, controls, editing, path, allow, task, show, first, separate=False):
    """The blend controls `show(name)` lets through; Quick start tasks call it twice, up front and under Advanced."""
    overlay = controls.edge_style == 'OVERLAY'
    if show('style'):
        if not editing:
            if allow.overlays or overlay:
                box.prop(controls, 'edge_style', text='Style')
            else:
                row = box.row(align=True)
                for item in EDGE_STYLES:
                    if item[0] != 'OVERLAY':
                        row.prop_enum(controls, 'edge_style', item[0])
        elif path:
            box.label(text={'PLAIN': 'Path, straight edges', 'GRASS': 'Path, grass edges', 'SNOW': 'Path, snow edges'}[controls.edge_style])
        else:
            box.label(text=dict((item[0], item[1]) for item in EDGE_STYLES)[controls.edge_style])
    if not overlay:
        if not editing and allow.paths and show('path'):
            box.prop(controls, 'path', text='Path: B down the middle')
        if path and show('path_width'):
            box.prop(controls, 'path_width_percent', text='Path Width', slider=True)
    if show('tiles'):
        if separate:
            # Separate Material: the face UVs hold the layout, so it changes by applying again.
            box.label(text='Tiles: ' + dict((item[0], item[1]) for item in TILE_MODES)[controls.tiles] + ', set at Apply')
        else:
            box.row().prop(controls, 'tiles', expand=True)
    if overlay:
        if show('opacity'):
            box.prop(controls, 'opacity', slider=True)
        if not editing and allow.overlays and show('overlay_buttons'):
            row = box.row(align=True)
            row.operator('mesh.sprotile_load_overlay_image', text='Open PNG as B', icon='FILE_FOLDER')
            row.operator('mesh.sprotile_make_overlay', text='Soften B', icon='BRUSH_DATA')
            box.label(text='Clear pixels in B show tile A')
    else:
        if show('feather'):
            row = box.row(align=True)
            row.prop(controls, 'feather_percent', text='Feather', slider=True)
            row.menu('VIEW3D_MT_sprotile_feather', text='', icon='DOWNARROW_HLT')
        if first and task in {'FADE', 'SEAM'} and show('smooth'):
            box.prop(controls, 'smooth', text='Smooth Feather')
        if show('position'):
            box.prop(controls, 'position', text='Centre' if path else 'Position', slider=True)
            box.prop(controls, 'direction')
            if controls.direction == 'ANGLE':
                box.prop(controls, 'angle')
        if path and show('path_width'):
            box.label(text=path_run_text(controls), icon='INFO')
    if not editing and show('alpha') and (task == 'PAINTED' or
            (not overlay and bpy.context.scene.sprotile_blend_material_mode == 'SHARED')):
        draw_alpha_controls(box, controls, bpy.context, paint_task=task == 'PAINTED')
    if not editing and task == 'PATH_BRUSH' and show('path_brush'):
        draw_path_brush_controls(box, bpy.context)
    if not editing and task == 'SEAM' and show('seam'):
        row = box.row()
        row.scale_y = 1.4
        row.operator('mesh.sprotile_blend_seam', icon='MOD_EDGESPLIT')
        box.row().prop(bpy.context.scene, 'sprotile_seam_sides', expand=True)
        draw_blocker(box, seam_blocker(bpy.context))
    if controls.edge_style in SHAPED_EDGES and show('edge'):
        box.prop(controls, 'edge_height', text='Drift Height' if controls.edge_style == 'SNOW' else 'Tuft Height', slider=True)
        box.prop(controls, 'edge_frequency', text='Wave Count' if controls.edge_style == 'SNOW' else 'Tuft Count')


class VIEW3D_PT_sprotile_blend(bpy.types.Panel):
    bl_label = 'Texture Blend'
    bl_idname = 'VIEW3D_PT_sprotile_blend'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'

    @classmethod
    def poll(cls, context):
        return feature_enabled(context, 'show_blends')

    def draw(self, context):
        layout, scene = self.layout, context.scene
        shared = scene.sprotile_blend_material_mode == 'SHARED'
        patch = active_shared_patch(context)
        material = active_blend_material(context) if patch is None else None
        has_active = patch is not None or material is not None
        editing = has_active and scene.sprotile_blend_settings_source == 'ACTIVE'
        if has_active:
            layout.row(align=True).prop(scene, 'sprotile_blend_settings_source', expand=True)
        if editing and state.last_control_refusal:
            note = layout.column(align=True)
            note.alert = True
            note.label(text=state.last_control_refusal, icon='ERROR')
        draw_paint_mode_controls(layout, context)
        if not editing:draw_quick_start(layout, scene, BlendFeatures(**{name: feature_enabled(context, 'show_' + name) for name in ('paths', 'overlays')}))
        if not editing and scene.sprotile_blend_task != 'SEAM':draw_blend_inputs(layout,scene,can_copy=has_active)
        mismatch = set()
        obj = context.active_object
        for slot in (obj.material_slots if obj is not None and obj.type == 'MESH' else ()):
            mismatch |= shared_atlas_mismatch(slot.material)
        if mismatch:
            warn = layout.column(align=True)
            warn.alert = True
            warn.label(text='Atlas images differ: ' + ', '.join(sorted(mismatch)), icon='ERROR')
            warn.operator('mesh.sprotile_use_one_atlas', text='Use One Atlas', icon='IMAGE_DATA')
        controls = scene.sprotile_blend_defaults
        if editing:
            controls = patch if patch is not None else material.sprotile_blend_controls
        box = layout.box()
        if editing:
            row=box.row(align=True)
            row.label(text=patch.name if patch is not None else material.name)
            save=row.row(align=True);save.enabled=is_editable_mesh(context.active_object);save.operator_context='INVOKE_DEFAULT'
            op=save.operator('mesh.sprotile_save_preset',text='Save as Preset');op.source='ACTIVE';op.ask_name=True
        else:box.label(text='New Blend Settings')
        copies = active_patch_copies(context, patch) if editing and patch is not None else 1
        if copies > 1:
            warn = box.column(align=True)
            warn.alert = True
            warn.label(text=f'Shared by {copies} copies (duplicated or joined)', icon='ERROR')
            warn.label(text='Changes here change every copy')
            warn.operator('mesh.sprotile_separate_blend_copies', icon='UNLINKED')
        if editing:
            box.label(text='Changes this blend immediately', icon='INFO')
        box.use_property_split = True
        box.use_property_decorate = False
        path = controls.edge_style != 'OVERLAY' and controls.path
        allow = BlendFeatures(**{name: feature_enabled(context, 'show_' + name) for name in ('paths', 'overlays')})
        task = 'CUSTOM' if editing else scene.sprotile_blend_task
        if task != 'CUSTOM' and not allow.has_task(task):
            task = 'CUSTOM'   # that feature is switched off in the add-on preferences
        main = task_fields(task)
        draw_blend_fields(box, controls, editing, path, allow, task, lambda name: main is None or name in main, True,
                           separate=editing and patch is None)
        if main is not None:
            header, advanced = box.panel('sprotile_blend_advanced', default_closed=True)
            header.label(text='Advanced')
            if advanced:
                advanced.use_property_split = True
                advanced.use_property_decorate = False
                draw_blend_fields(advanced, controls, editing, path, allow, task, lambda name: name not in main, False,
                                   separate=editing and patch is None)
        if editing:
            turns = layout.box()
            turns.label(text='Turn Textures', icon='ORIENTATION_GIMBAL')
            for slot in ('A', 'B'):
                row = turns.row()
                row.label(text='B overlay' if slot == 'B' and controls.edge_style == 'OVERLAY' else 'Texture ' + slot)
                draw_turn_buttons(row, 'mesh.sprotile_turn_blend_texture', slot)
            turns.label(text='Turns every selected blend', icon='INFO')
            layout.operator('mesh.sprotile_pick_blend',text='Copy Blend',icon='EYEDROPPER')
            layout.operator('mesh.sprotile_remove_blend', text='Remove Blend from Selection', icon='LOOP_BACK')
            header,inputs=layout.panel('sprotile_next_blend_tiles',default_closed=True)
            header.label(text='New Blend tiles')
            if inputs:
                draw_blend_inputs(inputs,scene)
                inputs.label(text='Switch to New Blend to apply them')
        elif task not in {'PAINTED', 'PATH_BRUSH', 'SEAM'}:   # those tasks have their own button in the box above
            layout.operator('mesh.sprotile_apply_blend', text='Apply Blend', icon='SHADING_RENDERED')
            draw_blocker(layout, action_blocker(context, 'apply'))
            layout.label(text='Uses the shared atlas material' if shared else 'Makes a new material on Apply', icon='MATERIAL')
        header, body = layout.panel('sprotile_blend_options', default_closed=True)
        header.label(text='Blend Options')
        if body:
            if controls.edge_style != 'OVERLAY' and task not in {'FADE', 'SEAM'}:body.prop(controls,'smooth',text='Smooth Feather')
            if editing:body.operator('mesh.sprotile_apply_blend',text='Reapply with Captured Tiles',icon='SHADING_RENDERED')
            body.prop(scene, 'sprotile_patch_projection', text='Method')
            body.prop(scene, 'sprotile_patch_fit', text='Fit')
            if scene.sprotile_patch_fit=='RECTANGLE':
                body.prop(scene,'sprotile_rectangle_corner_mode',text='Corners')
            body.prop(scene, 'sprotile_blend_material_mode', text='Material')
            objects = edit_mesh_objects(context)
            active = context.active_object
            if not objects and active is not None and active.type == 'MESH':
                objects = [active]
            meshes = {obj.data.as_pointer(): obj.data for obj in objects}
            if not meshes:
                body.label(text='Select a mesh to see its UV maps', icon='INFO')
            for mesh in meshes.values():
                used, needed = blend_uv_usage(mesh, shared, controls.edge_style in SHAPED_EDGES)
                if len(meshes) > 1:
                    body.label(text=mesh.name, icon='MESH_DATA')
                body.label(text=f'UV maps: {used} of 8, +{needed} needed')
                shortage = used + needed - 8
                if shortage > 0:
                    body.label(text=f'Needs {shortage} more UV map' + ('s' if shortage != 1 else ''), icon='ERROR')
            if not shared:
                body.prop(scene, 'sprotile_blend_use_tint')
            if not editing:
                body.operator('mesh.sprotile_remove_blend', icon='LOOP_BACK')
                draw_blocker(body, action_blocker(context, 'remove'))
            body.label(text='See blends in Material Preview or Rendered')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import feature_enabled
from . import state
from .tiles import edit_mesh_objects, is_editable_mesh, transform_is_default, transform_label
from .blend_settings import EDGE_STYLES, SHAPED_EDGES, TILE_MODES
from .blend_plan import active_blend_material
from .blend_shader import shared_atlas_mismatch
from .blend_shared import active_shared_patch, blend_copies
from .blend_list import draw_cached
from .blend_recipes import blend_uv_usage
from .blend_turns import draw_turn_buttons
from .presets import clipboard_preview_icon
from .blend_alpha import draw_alpha_controls, draw_paint_mode_controls
from .path_brush import draw_path_brush_controls
from .hints import action_blocker, draw_blocker, draw_hint
from .seam import seam_blocker
