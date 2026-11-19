"""The main Sprotile sidebar panel.
"""

import bpy
from . import state


# ---------------------------------------------------------------------------
# Viewport Panel (Sidebar)
# ---------------------------------------------------------------------------

class VIEW3D_PT_sprotile_main(bpy.types.Panel):
    """Open the picker, map and paint tiles, and set how tiles are placed"""
    bl_label = "Map Tiles"
    bl_idname = "VIEW3D_PT_sprotile_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = context.active_object

        if state._legacy_notice:
            box = layout.box()
            box.alert = True
            box.label(text="An old Sprotile / UV Mapping copy is loaded", icon='ERROR')
            box.label(text="Disable it in Preferences > Add-ons:")
            box.label(text=state._legacy_notice)

        in_edit = is_editable_mesh(obj)
        atlas = resolve_atlas_image(obj) if in_edit else None
        col = layout.column(align=True)
        if in_edit:
            if atlas is None:
                col.label(text="Select faces and choose an atlas image", icon='INFO')
                col.operator("mesh.sprotile_setup_atlas", text="Set Up Atlas…", icon='FILE_FOLDER')
            else:
                col.label(text=atlas.name, icon='IMAGE_DATA')
        # Keep Close Picker available when the user leaves Edit Mode.
        if state._preview_operator is None:
            if not in_edit or atlas is not None:
                col.operator("mesh.sprotile_preview_start", text="Open Picker", icon='IMAGE_DATA')
        else:
            col.operator("mesh.sprotile_preview_stop", text="Close Picker", icon='PANEL_CLOSE')
        col.prop(scene, "sprotile_show_recent_tiles")
        if not workspace_exists():
            col.operator("screen.sprotile_workspace", text="Make Sprotile Workspace", icon='WORKSPACE')

        if not in_edit:
            layout.separator()
            layout.label(text="Select a mesh and enter Edit Mode", icon='INFO')
            return

        col = layout.column(align=True)
        col.separator()
        row = col.row(align=True)
        row.prop(scene, "sprotile_tile_size", text="Tile Size")
        row.prop(scene, "sprotile_tile_mode", text="")
        col.prop(scene, "sprotile_brush_radius")

        layout.separator()
        box = layout.box()
        box.label(text="Picker Click and 4 Key", icon='UV')
        box.row(align=True).prop(scene, "sprotile_mapping_mode", expand=True)
        row = box.row(align=True)
        row.operator("mesh.sprotile_map_selected", text="Map Per Face").mapping_mode = 'PER_FACE'
        row.operator("mesh.sprotile_map_patch", text="Map as Patch")
        for name, label in (('sprotile_patch_projection', 'Patch Method'), ('sprotile_patch_fit', 'Patch Fit')):
            field = box.split(factor=0.38)
            field.label(text=label)
            field.prop(scene, name, text='')

        row = layout.row(align=True)
        row.label(text=f"Tile: ({scene.sprotile_active_col}, {scene.sprotile_active_row})", icon='PINNED')
        row.operator("mesh.sprotile_pipette", text="Pipette", icon='EYEDROPPER')
        row = layout.row(align=True)
        row.operator('mesh.sprotile_focus_atlas',text='Focus Tile',icon='ZOOM_IN').target='CURRENT'
        row.operator('mesh.sprotile_focus_atlas',text='Fit Atlas',icon='VIEWZOOM').target='ALL'
        header, box = layout.panel('sprotile_mapping_options', default_closed=True)
        header.label(text='Mapping Options')
        if box:
            if atlas is not None:
                box.operator("mesh.sprotile_setup_atlas", text="Set Up Atlas on Untextured Faces…", icon='FILE_FOLDER')
            self.draw_mapping_options(box, scene, obj)
        header, body = layout.panel('sprotile_workflow_guide', default_closed=True)
        header.label(text='Quick Help', icon='INFO')
        if body:
            for line in ('Click a picker tile to choose it.',
                         'Ctrl-click a tile or press 4 to map.',
                         'Over a tile, R turns and X or Y flips.',
                         'A and B capture tiles for a blend.',
                         'Drag with Sprotile Paint to paint faces.',
                         '[ and ] change the brush radius.',
                         'Alt+P samples the active face.',
                         'Alt+R turns the selected tiles.',
                         'Switch to Material Preview to see tiles.'):
                body.label(text=line)

    @staticmethod
    def draw_mapping_options(box, scene, obj):
        if scene.sprotile_patch_fit=='RECTANGLE':
            box.prop(scene,'sprotile_rectangle_corner_mode',text='Corners')
            box.operator('mesh.sprotile_preview_rectangle',text='Preview Rectangle Fit',icon='UV')
            if scene.sprotile_rectangle_corner_mode=='MARKED':
                row=box.row(align=True)
                for i,label in enumerate(('BL','BR','TR','TL'),1):row.operator('mesh.sprotile_mark_corner',text=label).corner=i
                box.operator('mesh.sprotile_mark_corner',text='Clear Corner Marks').clear=True
                box.label(text='Select a vertex, then assign its corner')
                box.label(text='Select patch faces before mapping')
        active_uv = obj.data.uv_layers.active
        box.label(text="UV layer: " + (active_uv.name if active_uv is not None else "created on apply"))
        render_uv = next((layer for layer in obj.data.uv_layers if layer.active_render), None)
        if active_uv is not None and render_uv is not None and render_uv != active_uv:
            box.label(text="Render UV: " + render_uv.name, icon='INFO')
        if scene.sprotile_patch_projection == 'UNFOLD':
            box.label(text="Unfold follows surface bends")
        elif scene.sprotile_patch_projection == 'EXISTING':
            box.label(text="One selected layout per atlas")
            box.label(text="Keeps its own turn; ignores R and flips")
        box.label(text="The brush always paints per face")
        box.label(text="Painting over a blend removes it")
        box.prop(scene, "sprotile_use_uv_inset")
        inset_row = box.row()
        inset_row.enabled = scene.sprotile_use_uv_inset
        inset_row.prop(scene, "sprotile_uv_inset_px")
        box.label(text="Stops the next tile bleeding in", icon='INFO')

        header = box.row(align=True)
        header.label(text="Turn and Flip:", icon='ORIENTATION_GIMBAL')
        if not transform_is_default(*get_tile_transform(scene)):
            header.operator("mesh.sprotile_reset_transform", text="", icon='LOOP_BACK')
        box.row(align=True).prop(scene, "sprotile_rotation", expand=True)
        flip_row = box.row(align=True)
        flip_row.prop(scene, "sprotile_flip_u", toggle=True)
        flip_row.prop(scene, "sprotile_flip_v", toggle=True)
        box.label(text="Applies when painting or mapping", icon='INFO')
        row = box.row(align=True)
        row.label(text="Turn selected tiles")
        for steps, icon in ((-1, 'LOOP_BACK'), (1, 'LOOP_FORWARDS')):
            row.operator('mesh.sprotile_turn_placed_tile', text='', icon=icon).steps = steps


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .workspace import workspace_exists
from .tiles import get_tile_transform, is_editable_mesh, resolve_atlas_image, transform_is_default
