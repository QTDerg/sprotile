"""Browse the scanned overlay cells of an atlas and use a visible cell as texture B."""

import bpy
from bpy.props import (CollectionProperty, EnumProperty, FloatVectorProperty, IntProperty,
                       IntVectorProperty, PointerProperty, StringProperty)

# Keep the original Python entry points available to scripts that browse an atlas.
from .overlay_scan import classify_colour, overlay_cells, scan_key, scan_overlay_cells

OVERLAY_KINDS = [('grass', 'Grass', 'Green, olive and yellow-green fringes'),
                 ('snow', 'Snow', 'White, pale blue or pale violet cells'),
                 ('extra', 'Extra', 'Any other overlay cell')]
KIND_FILTER_ITEMS = [('ALL', 'All', 'Every overlay cell')] + OVERLAY_KINDS


def overlay_atlas_image(context):
    """The atlas to browse: the New Blend A tile's image, else the active face's material atlas."""
    scene = context.scene
    image = scene.sprotile_blend_a.image
    if image is not None and min(image.size) > 0:
        return image
    return resolve_atlas_image(context.active_object)


class SprotileOverlayCell(bpy.types.PropertyGroup):
    col: IntProperty()
    row: IntProperty()
    kind: StringProperty()
    color: FloatVectorProperty(size=3, subtype='COLOR', min=0., max=1., default=(0., 0., 0.))


def scan_matches(scene, image):
    return (image is not None and scene.sprotile_overlay_scan_image == image and
            tuple(scene.sprotile_overlay_scan_grid) == (*image.size, scene.sprotile_tile_size))


def matches_kind(item, kind):
    return kind == 'ALL' or item.kind == kind


def selected_overlay_cell(scene):
    cells = scene.sprotile_overlay_cells
    index = scene.sprotile_overlay_index
    if 0 <= index < len(cells) and matches_kind(cells[index], scene.sprotile_overlay_kind):
        return cells[index]
    return None


def select_visible_cell(scene, _context):
    if selected_overlay_cell(scene) is None:
        scene.sprotile_overlay_index = next((i for i, item in enumerate(scene.sprotile_overlay_cells)
                                             if matches_kind(item, scene.sprotile_overlay_kind)), 0)


class SPROTILE_UL_overlay_cells(bpy.types.UIList):
    """Overlay cells found in the atlas, filtered by the kind chosen above the list"""

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        kind = context.scene.sprotile_overlay_kind
        flags = [self.bitflag_filter_item if matches_kind(item, kind) else 0 for item in items]
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        swatch = row.row(align=True)
        swatch.enabled = False
        swatch.prop(item, 'color', text='')
        row.label(text=f'{item.kind.title()}  {item.col},{item.row}')


class MESH_OT_sprotile_scan_overlay_cells(bpy.types.Operator):
    """Find the cells of the atlas that mix clear and solid pixels"""
    bl_idname = 'mesh.sprotile_scan_overlay_cells'
    bl_label = 'Scan Overlay Cells'
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        image = overlay_atlas_image(context)
        if image is None or min(image.size) <= 0:
            self.report({'WARNING'}, 'Select a mesh with an atlas, or capture A, first')
            return {'CANCELLED'}
        try:
            cells = scan_overlay_cells(image, scene.sprotile_tile_size, force=True)
        except (RuntimeError, MemoryError, ValueError) as error:
            self.report({'WARNING'}, 'Could not read the atlas pixels: ' + str(error))
            return {'CANCELLED'}
        scene.sprotile_overlay_cells.clear()
        for col, row, kind, rgb in cells:
            item = scene.sprotile_overlay_cells.add()
            item.col, item.row, item.kind, item.color = col, row, kind, rgb
        scene.sprotile_overlay_scan_image = image
        scene.sprotile_overlay_scan_grid = (*image.size, scene.sprotile_tile_size)
        scene.sprotile_overlay_index = 0
        select_visible_cell(scene, context)
        self.report({'INFO'}, f'{len(cells)} overlay cell(s) in {image.name}')
        return {'FINISHED'}


class MESH_OT_sprotile_use_overlay_cell(bpy.types.Operator):
    """Use the selected overlay cell as texture B, as an Overlay laid per face"""
    bl_idname = 'mesh.sprotile_use_overlay_cell'
    bl_label = 'Use as B'
    bl_options = {'UNDO'}

    def execute(self, context):
        scene = context.scene
        image = overlay_atlas_image(context)
        if not scan_matches(scene, image):
            self.report({'WARNING'}, 'Scan the current atlas and tile size first')
            return {'CANCELLED'}
        item = selected_overlay_cell(scene)
        if item is None:
            self.report({'WARNING'}, 'Select a visible overlay cell first')
            return {'CANCELLED'}
        try:
            use_cell_as_b(scene, image, item.col, item.row)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'B: overlay cell {item.col},{item.row}')
        return {'FINISHED'}


def use_cell_as_b(scene, image, col, row):
    """Set B to a whole cell, as Capture B would, and make the next blend a per-face Overlay."""
    width, height = image_size(image)
    if image is None or min(width, height) <= 0:
        raise ValueError('Select a mesh with an atlas image first')
    cell = scene.sprotile_tile_size
    validate_tile_dimensions(cell, cell, width, height)
    if not (0 <= col < width // cell and 0 <= row < height // cell):
        raise ValueError('This overlay cell is outside the atlas; scan it again')
    record = scene.sprotile_blend_b
    record.image = image
    record.col, record.row = col, row
    record.width_px = record.height_px = cell
    record.atlas_width, record.atlas_height = width, height
    record.rotation, record.flip_u, record.flip_v = 0, False, False
    record.inset_px = get_uv_inset(scene)
    record.is_sampled = False
    record.sample_uv = (1, 0, 0, 0, 1, 0)
    controls = scene.sprotile_blend_defaults
    controls.edge_style = 'OVERLAY'
    controls.tiles = 'FACE'
    scene.sprotile_blend_settings_source = 'NEW'
    invalidate_clipboard_slot(scene, 'b')
    reset_preview_interaction()


class VIEW3D_PT_sprotile_overlay_browser(bpy.types.Panel):
    bl_label = 'Overlay Browser (experimental)'
    bl_idname = 'VIEW3D_PT_sprotile_overlay_browser'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'
    bl_parent_id = 'VIEW3D_PT_sprotile_blend'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return feature_enabled(context, 'show_overlays') and feature_enabled(context, 'show_blends')

    def draw(self, context):
        layout, scene = self.layout, context.scene
        image = overlay_atlas_image(context)
        if image is None:
            layout.label(text='Capture A or select a mesh with an atlas', icon='INFO')
            return
        layout.label(text=f'{image.name} ({scene.sprotile_tile_size} px tiles)', icon='IMAGE_DATA')
        layout.operator('mesh.sprotile_scan_overlay_cells', icon='VIEWZOOM')
        if not scan_matches(scene, image):
            layout.label(text='Scan this atlas to list its overlay cells', icon='INFO')
            return
        if not len(scene.sprotile_overlay_cells):
            layout.label(text='No cells contain both clear and solid pixels', icon='INFO')
            return
        layout.prop(scene, 'sprotile_overlay_kind', text='Kind')
        if selected_overlay_cell(scene) is None and not any(
                matches_kind(item, scene.sprotile_overlay_kind) for item in scene.sprotile_overlay_cells):
            layout.label(text='No cells match this kind', icon='INFO')
            return
        layout.template_list('SPROTILE_UL_overlay_cells', '', scene, 'sprotile_overlay_cells',
                             scene, 'sprotile_overlay_index', rows=6)
        row = layout.row()
        row.enabled = selected_overlay_cell(scene) is not None
        row.operator('mesh.sprotile_use_overlay_cell', icon='IMAGE_RGB_ALPHA')


def register_overlay_properties():
    bpy.types.Scene.sprotile_overlay_scan_image = PointerProperty(type=bpy.types.Image)
    bpy.types.Scene.sprotile_overlay_scan_grid = IntVectorProperty(size=3)
    bpy.types.Scene.sprotile_overlay_kind = EnumProperty(
        name='Overlay Kind', items=KIND_FILTER_ITEMS, default='ALL', update=select_visible_cell,
        description='Show only overlay cells of this kind')
    bpy.types.Scene.sprotile_overlay_index = IntProperty(default=0, min=0)
    bpy.types.Scene.sprotile_overlay_cells = CollectionProperty(type=SprotileOverlayCell)


# Names from other Sprotile modules, used when functions run (see __init__.py).
from .tiles import resolve_atlas_image, image_size, validate_tile_dimensions, get_uv_inset
from .core import feature_enabled
from .blend_ops import invalidate_clipboard_slot, reset_preview_interaction
