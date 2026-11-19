"""Sprotile: map atlas tiles onto faces and blend two textures across patches, for PS1-style levels.

The add-on is a package. Modules, from the bottom up: state, core, draw, tiles, mapping, picker,
brush and panel (painting and mapping); blend_settings, blend_storage, blend_plan, blend_shader, blend_shared,
blend_recipes, blend_turns, presets, blend_list, blend_ops, overlays, overlay_browser, unreal, blend_alpha, path_brush
and blend_ui (blends);
cleanup. This file registers them.

Each module imports what it needs from the others at its end, after its own definitions, so the
modules can call each other. blend_settings is imported first because two class bodies use its
constants while their modules load.
"""

bl_info = {
    "name": "Sprotile",
    "version": (6, 0, 0),
    "author": "QTDerg, CrystalFissure",
    "blender": (4, 4, 0),
    "location": "View3D > Sidebar > Sprotile | View3D > Toolbar (Edit Mode)",
    "description": "Paint atlas tiles onto faces and blend textures across patches, paths and seams",
    "category": "UV",
}

import bpy
import sys
from bpy.app.handlers import persistent
from bpy.props import IntProperty, FloatProperty, StringProperty, EnumProperty, BoolProperty, PointerProperty, CollectionProperty


def _forget_loaded_modules():
    """When Blender reloads a changed add-on (Reload Scripts, or enabling it again after an edit) it
    reloads only this file. Forget the modules loaded with the old copy, so the imports below load
    the files as they are now instead of reusing the old ones."""
    prefix = __name__ + '.'
    for name in [name for name in sys.modules if name.startswith(prefix)]:
        sys.modules.pop(name, None)
        # `from . import x` reuses the package's attribute when it exists, so drop that too.
        globals().pop(name[len(prefix):], None)


_forget_loaded_modules()

from . import blend_settings  # first: see the module docstring
from . import state
from .core import LEGACY_PANEL_ID, PANEL_MIN_WIDTH, addon_keymaps, undo_safe
from .mapping import (
    MESH_OT_sprotile_flip, MESH_OT_sprotile_map_patch, MESH_OT_sprotile_map_selected,
    MESH_OT_sprotile_picker_map, MESH_OT_sprotile_reset_transform, MESH_OT_sprotile_rotate,
)
from .picker import (
    MESH_OT_sprotile_focus_atlas, MESH_OT_sprotile_pipette, MESH_OT_sprotile_preview_helper,
    MESH_OT_sprotile_preview_start, MESH_OT_sprotile_preview_stop,
)
from .brush import (
    MESH_OT_sprotile_brush_paint, MESH_OT_sprotile_brush_size, MESH_WT_sprotile_paint_tool, paint_tool_icon,
)
from .panel import VIEW3D_PT_sprotile_main
from .atlas_setup import MESH_OT_sprotile_setup_atlas
from .blend_settings import (
    MESH_OT_sprotile_mark_corner, SprotileBlendControls, SprotileBlendPreset, SprotileBlendTile,
    ensure_corner_overlay,
)
from .blend_shared import SprotileBlendPatch
from .blend_recipes import MESH_OT_sprotile_pick_blend, MESH_OT_sprotile_rotate_blend_copy
from .blend_turns import (
    MESH_OT_sprotile_turn_blend_texture, MESH_OT_sprotile_turn_blend_tile, MESH_OT_sprotile_turn_placed_tile,
)
from .presets import (
    MESH_OT_sprotile_find_preset, MESH_OT_sprotile_load_preset, MESH_OT_sprotile_preview_rectangle,
    MESH_OT_sprotile_refresh_presets, MESH_OT_sprotile_remove_preset, MESH_OT_sprotile_save_preset,
    SPROTILE_UL_blend_presets, VIEW3D_PT_sprotile_presets,
)
from .blend_list import (
    MESH_OT_sprotile_select_blend_patch, MESH_OT_sprotile_separate_blend_copies, SPROTILE_UL_blend_patches,
    VIEW3D_PT_sprotile_blend_list,
    _CHANGE_HANDLERS, _sprotile_data_changed, _sprotile_frame_changed,
)
from .blend_ops import (
    MESH_OT_sprotile_apply_blend, MESH_OT_sprotile_capture_blend_tile, MESH_OT_sprotile_feather,
    MESH_OT_sprotile_remove_blend, MESH_OT_sprotile_swap_blend_tiles, MESH_OT_sprotile_use_one_atlas,
    VIEW3D_MT_sprotile_feather,
)
from .overlays import MESH_OT_sprotile_load_overlay_image, MESH_OT_sprotile_make_overlay
from .overlay_browser import (
    MESH_OT_sprotile_scan_overlay_cells, MESH_OT_sprotile_use_overlay_cell, SPROTILE_UL_overlay_cells,
    SprotileOverlayCell, VIEW3D_PT_sprotile_overlay_browser, register_overlay_properties,
)
from .unreal import MESH_OT_sprotile_prepare_unreal, VIEW3D_PT_sprotile_unreal
from .blend_alpha import MESH_OT_sprotile_alpha_brush, MESH_OT_sprotile_clear_alpha, MESH_OT_sprotile_paint_alpha
from .path_brush import MESH_OT_sprotile_path_brush, MESH_OT_sprotile_path_from_selection
from .seam import MESH_OT_sprotile_blend_seam
from .blend_ui import BLEND_TASKS, VIEW3D_PT_sprotile_blend, update_blend_task
from .workspace import SCREEN_OT_sprotile_workspace
from .cleanup import force_cleanup
from .blend_storage import upgrade_file_blends


_tool_registered = False


class SprotilePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    show_blends: BoolProperty(name='Blends', description='Show Texture Blend and Blends on This Mesh', default=True)
    show_paths: BoolProperty(name='Paths', description='Show the Path option in Texture Blend', default=True)
    show_overlays: BoolProperty(name='Overlays', description='Show the Overlay style, Open PNG as B and Soften B', default=True)
    show_unreal: BoolProperty(name='Unreal Export', description='Show the Unreal Export panel', default=True)
    show_library: BoolProperty(name='Preset Library', description='Show the Blend Preset Library', default=True)
    picker_blend_keys: BoolProperty(
        name='A and B capture blend tiles in the picker',
        description='With the mouse over a tile in the picker, A captures it as blend tile A and B as blend tile B. '
                    'Off: the keys keep their usual meaning there (Select All and Box Select)', default=True)
    show_status_line: BoolProperty(
        name='Status line while painting',
        description='While the Sprotile Paint tool or the Path Brush is in use, show the current tile, the brush '
                    'size and the keys along the bottom of the viewport', default=True)

    def draw(self, context):
        layout = self.layout
        layout.label(text='Show in the sidebar')
        row = layout.row()
        for name in ('show_blends', 'show_paths', 'show_overlays', 'show_unreal', 'show_library'):
            row.prop(self, name)
        layout.prop(self, 'picker_blend_keys')
        layout.prop(self, 'show_status_line')


class SprotileAlreadyLoaded(RuntimeError):
    """Another copy of Sprotile already owns Sprotile's operators or settings."""


def _module_file(module_name):
    module = sys.modules.get(module_name)
    path = getattr(module, '__file__', None)
    return path or module_name


def _ours(module_name):
    """True for this package and its modules, where Sprotile's classes live."""
    return bool(module_name) and (module_name == __name__ or module_name.startswith(__name__ + '.'))


def _other_copy():
    """Return (module, file) of another loaded copy that would clash, or None."""
    owners = []
    for cls in classes + _BLEND_PROPERTY_CLASSES:
        existing = getattr(bpy.types, cls.__name__, None)
        if existing is None:
            idname = getattr(cls, 'bl_idname', '')
            if '.' in idname and issubclass(cls, bpy.types.Operator):
                prefix, name = idname.split('.', 1)
                existing = getattr(bpy.types, f'{prefix.upper()}_OT_{name}', None)
        owner = getattr(existing, '__module__', None)
        if owner and not _ours(owner):
            owners.append(owner)
    if not owners and hasattr(bpy.types.Scene, 'sprotile_tile_size'):
        owners.append('an unknown module')
    if not owners:
        return None
    owner = owners[0]
    # Name the add-on (the package), not the module inside it that owns the class.
    owner = getattr(sys.modules.get(owner), '__package__', None) or owner
    return owner, _module_file(owner)


def _legacy_panel_copy():
    """Return the file of an old UV Mapping / pre-5.9 panel that is still loaded."""
    legacy = getattr(bpy.types, LEGACY_PANEL_ID, None)
    owner = getattr(legacy, '__module__', None)
    if legacy is None or _ours(owner):
        return None
    return _module_file(owner) if owner else LEGACY_PANEL_ID


@persistent
def _sprotile_load_pre(_dummy):
    force_cleanup()


@persistent
def _sprotile_load_post(_dummy):
    force_cleanup()
    ensure_corner_overlay()
    try:
        upgrade_file_blends()   # 5.12 blends: A and the Mask move out of their UV maps (see blend_storage)
    except Exception as error:   # never block opening a file
        print('Sprotile: could not upgrade the blends in this file:', error)


# ---------------------------------------------------------------------------
# Registration / Unregistration
# ---------------------------------------------------------------------------

classes = (
    SprotilePreferences,
    SCREEN_OT_sprotile_workspace,
    MESH_OT_sprotile_setup_atlas,
    MESH_OT_sprotile_preview_helper,
    MESH_OT_sprotile_pipette,
    MESH_OT_sprotile_map_selected,
    MESH_OT_sprotile_map_patch,
    MESH_OT_sprotile_picker_map,
    MESH_OT_sprotile_rotate,
    MESH_OT_sprotile_turn_placed_tile,
    MESH_OT_sprotile_flip,
    MESH_OT_sprotile_reset_transform,
    MESH_OT_sprotile_preview_start,
    MESH_OT_sprotile_preview_stop,
    MESH_OT_sprotile_focus_atlas,
    MESH_OT_sprotile_brush_paint,
    MESH_OT_sprotile_brush_size,
    MESH_OT_sprotile_capture_blend_tile,
    MESH_OT_sprotile_load_overlay_image,
    MESH_OT_sprotile_make_overlay,
    MESH_OT_sprotile_prepare_unreal,
    MESH_OT_sprotile_swap_blend_tiles,
    MESH_OT_sprotile_pick_blend,
    MESH_OT_sprotile_rotate_blend_copy,
    MESH_OT_sprotile_turn_blend_tile,
    MESH_OT_sprotile_turn_blend_texture,
    MESH_OT_sprotile_apply_blend,
    MESH_OT_sprotile_remove_blend,
    MESH_OT_sprotile_use_one_atlas,
    MESH_OT_sprotile_mark_corner,
    MESH_OT_sprotile_preview_rectangle,
    SPROTILE_UL_blend_presets,
    SPROTILE_UL_blend_patches,
    SPROTILE_UL_overlay_cells,
    MESH_OT_sprotile_scan_overlay_cells,
    MESH_OT_sprotile_use_overlay_cell,
    MESH_OT_sprotile_select_blend_patch,
    MESH_OT_sprotile_separate_blend_copies,
    MESH_OT_sprotile_paint_alpha,
    MESH_OT_sprotile_alpha_brush,
    MESH_OT_sprotile_clear_alpha,
    MESH_OT_sprotile_path_brush,
    MESH_OT_sprotile_blend_seam,
    MESH_OT_sprotile_path_from_selection,
    MESH_OT_sprotile_save_preset,
    MESH_OT_sprotile_load_preset,
    MESH_OT_sprotile_remove_preset,
    MESH_OT_sprotile_refresh_presets,
    MESH_OT_sprotile_find_preset,
    MESH_OT_sprotile_feather,
    VIEW3D_MT_sprotile_feather,
    VIEW3D_PT_sprotile_main,
    VIEW3D_PT_sprotile_blend,
    VIEW3D_PT_sprotile_overlay_browser,
    VIEW3D_PT_sprotile_blend_list,
    VIEW3D_PT_sprotile_presets,
    VIEW3D_PT_sprotile_unreal,
)

_BLEND_PROPERTY_CLASSES = (SprotileBlendTile, SprotileBlendControls, SprotileBlendPatch, SprotileBlendPreset, SprotileOverlayCell)

_PROPERTY_NAMES = (
    'sprotile_blend_task',
    'sprotile_seam_sides',
    'sprotile_blend_presets',
    'sprotile_preset_index',
    'sprotile_patch_list_index',
    'sprotile_preset_name',
    'sprotile_auto_presets',
    'sprotile_overlay_kind',
    'sprotile_overlay_index',
    'sprotile_overlay_cells',
    'sprotile_overlay_scan_grid',
    'sprotile_overlay_scan_image',
    'sprotile_rectangle_corner_mode',
    "sprotile_blend_a",
    "sprotile_blend_b",
    "sprotile_blend_defaults",
    "sprotile_blend_use_tint",
    "sprotile_blend_material_mode",
    "sprotile_blend_settings_source",
    "sprotile_mapping_mode",
    "sprotile_patch_projection",
    "sprotile_patch_fit",
    "sprotile_tile_size",
    "sprotile_tile_mode",
    "sprotile_brush_radius",
    "sprotile_use_uv_inset",
    "sprotile_uv_inset_px",
    "sprotile_rotation",
    "sprotile_flip_u",
    "sprotile_flip_v",
    "sprotile_preview_width",
    "sprotile_preview_left",
    "sprotile_show_recent_tiles",
    "sprotile_active_col",
    "sprotile_active_row",
)


def _register_properties():
    register_overlay_properties()
    bpy.types.Scene.sprotile_seam_sides = EnumProperty(
        name='Sides', description='Which faces Blend This Seam changes',
        items=[('BOTH', 'Both sides', 'The faces on both sides fade into each other and meet half and half'),
               ('FIRST', 'One side', 'Only one tile\'s faces change: they fade fully into the other tile at the seam, '
                                     'and the faces across it stay as they are'),
               ('SECOND', 'Other side', 'As One side, for the faces on the other side of the seam')],
        default='BOTH')
    bpy.types.Scene.sprotile_blend_task = EnumProperty(
        name='Quick start', items=BLEND_TASKS, default='CUSTOM', update=update_blend_task,
        description='Set up New Blend for a common job; the controls it does not need move under Advanced')
    bpy.types.Scene.sprotile_rectangle_corner_mode=EnumProperty(name='Rectangle Corners',items=[
        ('AUTO','Automatic','Choose four safe boundary corners'),('MARKED','Marked Vertices','Use your BL, BR, TR and TL vertex marks')],default='AUTO')
    bpy.types.Scene.sprotile_blend_presets=CollectionProperty(type=SprotileBlendPreset)
    bpy.types.Scene.sprotile_preset_index=IntProperty(default=0,min=0)
    bpy.types.Scene.sprotile_patch_list_index=IntProperty(default=0,min=0)
    bpy.types.Scene.sprotile_preset_name=StringProperty(name='Preset name',default='New blend')
    bpy.types.Scene.sprotile_auto_presets=BoolProperty(name='Add new blends to the library',default=True)
    bpy.types.Scene.sprotile_blend_settings_source = EnumProperty(
        name='Blend Controls', default='ACTIVE', items=[
            ('ACTIVE', 'Edit Blend', 'Change the selected blend: its sliders change it straight away'),
            ('NEW', 'New Blend', 'Set up tiles and settings for the next Apply Blend; placed blends stay as they are')])
    bpy.types.Scene.sprotile_blend_a = PointerProperty(type=SprotileBlendTile)
    bpy.types.Scene.sprotile_blend_b = PointerProperty(type=SprotileBlendTile)
    bpy.types.Scene.sprotile_blend_defaults = PointerProperty(type=SprotileBlendControls)
    bpy.types.Scene.sprotile_blend_use_tint = BoolProperty(
        name="Use Existing Tint Lighting",
        description="Multiply by existing Tint RGB and the PS1 2x factor when the mesh has Tint; keep colour data unchanged",
        default=True)
    bpy.types.Material.sprotile_blend_controls = PointerProperty(type=SprotileBlendControls)
    bpy.types.Mesh.sprotile_blend_patches = CollectionProperty(type=SprotileBlendPatch)
    bpy.types.Scene.sprotile_blend_material_mode = EnumProperty(
        name="Material Mode", default='SHARED', items=[
            ('SHARED', "Shared Material", "Every blend shares the mesh's atlas material and its Tint lighting, each with its own settings"),
            ('SEPARATE', "Separate Material", "Advanced: a new material for each Apply; allows images other than the atlas")])
    bpy.types.Scene.sprotile_patch_fit = EnumProperty(
        name="Patch Fit",
        description="Fill the tile or preserve the chart's proportions in texture pixels",
        items=[('FILL', "Fill Tile", "Stretch chart bounds to the tile; irregular outlines still leave unused corners"),
               ('KEEP_SHAPE', "Keep Proportions", "Preserve chart proportions and centre it; may use only part of the tile"),
               ('RECTANGLE', "Fit Rectangle", "Stretch one continuous patch to all four tile edges; may distort the texture")],
        default='FILL',
    )
    bpy.types.Scene.sprotile_mapping_mode = EnumProperty(
        name="Click and 4 Key Map",
        description="How a picker click and the 4 key map the selected faces; the brush always paints per face",
        items=[('PER_FACE', "Per Face", "Repeat the tile separately on every selected face (original behavior)"),
               ('PATCH', "Patch", "Experimental: unfold or project one tile across each connected selected area")],
        default='PER_FACE',
    )
    bpy.types.Scene.sprotile_patch_projection = EnumProperty(
        name="Patch Method",
        description="Unfold bent faces or project onto a plane, then fit the chart to the full tile bounds",
        items=[('UNFOLD', "Unfold (Bent)", "Unwrap the connected surface before fitting the tile; respects UV seams"),
               ('EXISTING', "Existing UVs", "Fit all selected UVs per atlas, including disconnected objects; keep their orientation. "
                "The picker and 4 key ignore R / flips here; blends still apply turns captured with A and B"),
               ('SURFACE', "Surface (Planar)", "Project onto the patch's average surface plane; keep walls upright"),
               ('VIEW', "View (Planar)", "Orthographic projection along the viewport direction; turn the view face-on")],
        default='UNFOLD',
    )
    bpy.types.Scene.sprotile_show_recent_tiles = BoolProperty(
        name="Recently-used Tiles",
        description="Show up to 32 recent tiles at the bottom-right while the atlas preview is open. "
                    "History is kept per atlas for this session",
        default=True,
    )
    bpy.types.Scene.sprotile_tile_size = IntProperty(
        name="Tile Size",
        description="Base size of a full square tile in pixels",
        default=64,
        min=1,
        soft_max=1024,
    )
    bpy.types.Scene.sprotile_tile_mode = EnumProperty(
        name="Tile Mode",
        description="Shape of the tile to paint",
        items=[
            ('FULL', "Full Tile", "Full square tile (e.g. 64x64)"),
            ('HALF_WIDE', "Half Tile - Wide", "Landscape half tile (e.g. 64x32)"),
            ('HALF_TALL', "Half Tile - Tall", "Portrait half tile (e.g. 32x64)"),
        ],
        default='FULL',
    )
    bpy.types.Scene.sprotile_brush_radius = FloatProperty(
        name="Brush Radius",
        description="Viewport radius of the brush in screen pixels",
        default=30.0,
        min=1.0,
        max=500.0,
    )
    bpy.types.Scene.sprotile_use_uv_inset = BoolProperty(
        name="UV Inset",
        description="Leave a centered buffer inside each tile to reduce texture bleeding. "
                    "Applies when painting or mapping; disable for continuous tiling",
        default=True,
    )
    bpy.types.Scene.sprotile_uv_inset_px = FloatProperty(
        name="Inset per Side (px)",
        description="Buffer on each side in atlas pixels: 0.25 maps a 64px tile to 63.5px. "
                    "Clamped below half the smaller tile dimension; re-map faces to apply",
        default=0.25,
        min=0.0,
        soft_max=2.0,
        precision=2,
        step=5,
    )
    bpy.types.Scene.sprotile_rotation = EnumProperty(
        name="Turn",
        description="Turn the tile as it is painted or mapped onto faces. "
                    "Faces already mapped are not changed - re-map them to apply it",
        items=[
            ('0', "0", "No rotation"),
            ('90', "90", "Quarter turn"),
            ('180', "180", "Half turn"),
            ('270', "270", "Three-quarter turn"),
        ],
        default='0',
    )
    bpy.types.Scene.sprotile_flip_u = BoolProperty(
        name="Flip U",
        description="Mirror the tile horizontally as it is painted",
        default=False,
    )
    bpy.types.Scene.sprotile_flip_v = BoolProperty(
        name="Flip V",
        description="Mirror the tile vertically as it is painted",
        default=False,
    )
    bpy.types.Scene.sprotile_preview_width = IntProperty(
        name="Preview Width",
        description="Width of the texture preview column in pixels",
        default=300,
        min=PANEL_MIN_WIDTH,
        soft_max=1200,
    )
    bpy.types.Scene.sprotile_preview_left = IntProperty(
        name="Preview Left Offset",
        description="Horizontal spacing from the left screen edge in pixels",
        default=50,
        min=0,
        soft_max=400,
    )
    bpy.types.Scene.sprotile_active_col = IntProperty(
        name="Active Column",
        default=0,
        min=0,
    )
    bpy.types.Scene.sprotile_active_row = IntProperty(
        name="Active Row",
        default=0,
        min=0,
    )


def register():
    global _tool_registered

    clash = _other_copy()
    if clash is not None:
        module, path = clash
        message = (f"Sprotile could not start because another copy is already loaded "
                   f"({module}, from {path}). Open Edit > Preferences > Add-ons, disable or "
                   f"remove that copy, then enable Sprotile again.")
        print(message)
        raise SprotileAlreadyLoaded(message)
    state._legacy_notice = _legacy_panel_copy()
    if state._legacy_notice:
        print(f"Sprotile: an old UV Mapping or Sprotile panel is still loaded from {state._legacy_notice}; "
              "disable it in Edit > Preferences > Add-ons.")

    for cls in _BLEND_PROPERTY_CLASSES:
        bpy.utils.register_class(cls)
    _register_properties()

    for cls in classes:
        if issubclass(cls, bpy.types.Operator):
            undo_safe(cls)
        bpy.utils.register_class(cls)
    ensure_corner_overlay()

    try:
        MESH_WT_sprotile_paint_tool.bl_icon = paint_tool_icon()
        bpy.utils.register_tool(
            MESH_WT_sprotile_paint_tool,
            after={"builtin.select_circle"},
            separator=True,
            group=True,
        )
        _tool_registered = True
    except Exception as exc:
        _tool_registered = False
        print(f"Sprotile: could not register workspace tool: {exc}")

    # Keymaps: native addon config, remappable in Preferences > Keymap.
    addon_keymaps.clear()
    wm = bpy.context.window_manager
    if wm is not None and wm.keyconfigs is not None:
        kc = wm.keyconfigs.addon
        if kc is not None:
            km = kc.keymaps.new(name='Mesh', space_type='EMPTY')
            def bind(operator, key, **modifiers):
                item = km.keymap_items.new(operator, key, 'PRESS', **modifiers)
                addon_keymaps.append((km, item))
                return item

            bind(MESH_OT_sprotile_map_selected.bl_idname, 'FOUR')
            bind(MESH_OT_sprotile_pipette.bl_idname, 'P', alt=True)
            for shift, steps in ((False, 1), (True, -1)):
                item = bind(MESH_OT_sprotile_turn_placed_tile.bl_idname, 'R', alt=True, shift=shift)
                item.properties.steps = steps

    if _sprotile_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_sprotile_load_pre)
    if _sprotile_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sprotile_load_post)
    for name in _CHANGE_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _sprotile_data_changed not in handlers:
            handlers.append(_sprotile_data_changed)
    if _sprotile_frame_changed not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_sprotile_frame_changed)


def unregister():
    global _tool_registered

    # Kill running modal operators and their draw handlers *before* the classes
    # they belong to go away, otherwise Blender dereferences freed types.
    force_cleanup()

    if _sprotile_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_sprotile_load_pre)
    if _sprotile_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sprotile_load_post)
    for name in _CHANGE_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _sprotile_data_changed in handlers:
            handlers.remove(_sprotile_data_changed)
    if _sprotile_frame_changed in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_sprotile_frame_changed)

    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()

    if _tool_registered:
        try:
            bpy.utils.unregister_tool(MESH_WT_sprotile_paint_tool)
        except Exception as exc:
            print(f"Sprotile: could not unregister workspace tool: {exc}")
        _tool_registered = False

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            print(f"Sprotile: could not unregister {cls.__name__}: {exc}")

    for name in _PROPERTY_NAMES:
        if hasattr(bpy.types.Scene, name):
            try:
                delattr(bpy.types.Scene, name)
            except Exception:
                pass

    if hasattr(bpy.types.Material, 'sprotile_blend_controls'):
        del bpy.types.Material.sprotile_blend_controls
    if hasattr(bpy.types.Mesh, 'sprotile_blend_patches'):
        del bpy.types.Mesh.sprotile_blend_patches
    for cls in reversed(_BLEND_PROPERTY_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            print(f"Sprotile: could not unregister {cls.__name__}: {exc}")


if __name__ == "__main__":
    register()
