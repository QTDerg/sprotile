"""The Sprotile workspace: the current workspace duplicated, with the atlas image editor beside the 3D view."""

import bpy

WORKSPACE_NAME = 'Sprotile'
_pending = []   # window pointers whose layout timer has not run yet
IMAGE_EDITOR_SHARE = 0.35   # the right-hand share of the 3D view given to the atlas


def workspace_exists():
    return WORKSPACE_NAME in bpy.data.workspaces


def _atlas_image(context):
    obj = context.active_object
    if obj is None:
        return None
    try:
        return image_from_material(obj.active_material) if obj.active_material is not None else None
    except Exception:
        return None


def _main_view3d(screen):
    areas = [area for area in screen.areas if area.type == 'VIEW_3D']
    return max(areas, key=lambda area: area.width * area.height) if areas else None


def _window_region(area):
    return next((region for region in area.regions if region.type == 'WINDOW'), None)


def layout_workspace(window, image=None):
    """Split the biggest 3D view of the window's screen: the atlas in an image editor taking about a third, sidebar open.

    The narrower half becomes the image editor, whichever side it lands on."""
    screen = window.screen
    area = _main_view3d(screen)
    if area is None:
        return False
    # The duplicated Layout screen brings a timeline; this workspace has no use for it.
    for other in list(screen.areas):
        if other.type == 'DOPESHEET_EDITOR' and other.height < area.height and other.width >= area.width:
            try:
                with bpy.context.temp_override(window=window, screen=screen, area=other, region=_window_region(other)):
                    bpy.ops.screen.area_close()
            except Exception:
                pass
    area = _main_view3d(screen)
    before = {a.as_pointer() for a in screen.areas}
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=_window_region(area)):
        # From a script the new area takes the LEFT `factor` share of the width, so ask for the atlas's share.
        bpy.ops.screen.area_split(direction='VERTICAL', factor=IMAGE_EDITOR_SHARE)
    created = [a for a in screen.areas if a.as_pointer() not in before]
    if created:
        pair = sorted((area, created[0]), key=lambda a: a.width)
        narrow, wide = pair[0], pair[1]          # the atlas takes the narrower half, wherever it landed
        with bpy.context.temp_override(window=window, screen=screen, area=narrow, region=_window_region(narrow)):
            narrow.type = 'IMAGE_EDITOR'
            if image is not None:
                narrow.spaces.active.image = image
        area = wide
    space = area.spaces.active
    # Setting show_region_ui outside a window context crashes Blender (ED_region_visibility_change_update reads the
    # window's active workspace), so the override is required here even though it looks redundant.
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=_window_region(area)):
        space.show_region_ui = True
        try:
            # Read-only in Blender 5.x; the sidebar then opens on whichever tab it had.
            next(region for region in area.regions if region.type == 'UI').active_panel_category = 'Sprotile'
        except Exception:
            pass
    return True


class SCREEN_OT_sprotile_workspace(bpy.types.Operator):
    """Duplicate this workspace as "Sprotile": the 3D view with the Sprotile sidebar, and the atlas image beside it"""
    bl_idname = 'screen.sprotile_workspace'
    bl_label = 'Sprotile Workspace'
    bl_options = {'REGISTER'}

    def execute(self, context):
        window = context.window
        if window is None or context.workspace is None or not len(bpy.data.workspaces):
            self.report({'ERROR'}, 'There is no workspace to copy yet')
            return {'CANCELLED'}
        if workspace_exists():
            window.workspace = bpy.data.workspaces[WORKSPACE_NAME]
            return {'FINISHED'}
        if _pending:
            self.report({'INFO'}, 'The Sprotile workspace is being made')
            return {'CANCELLED'}
        names = {workspace.name for workspace in bpy.data.workspaces}
        try:
            bpy.ops.workspace.duplicate()
        except RuntimeError as error:
            self.report({'ERROR'}, 'Could not copy the workspace: ' + str(error))
            return {'CANCELLED'}
        created = [workspace for workspace in bpy.data.workspaces if workspace.name not in names]
        if not created:
            self.report({'ERROR'}, 'Blender did not make a new workspace')
            return {'CANCELLED'}
        created[0].name = WORKSPACE_NAME
        window.workspace = bpy.data.workspaces[WORKSPACE_NAME]
        image, window_id = _atlas_image(context), window.as_pointer()

        _pending.append(window_id)

        def finish():
            # The new screen only becomes the window's after this operator returns.
            try:
                for candidate in bpy.context.window_manager.windows:
                    if candidate.as_pointer() == window_id and candidate.workspace.name == WORKSPACE_NAME:
                        try:
                            layout_workspace(candidate, image)
                        except Exception as error:
                            print('Sprotile: could not lay out the workspace:', error)
            finally:
                _pending.clear()
            return None
        bpy.app.timers.register(finish, first_interval=0.1)
        self.report({'INFO'}, 'Made the Sprotile workspace')
        return {'FINISHED'}


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .tiles import image_from_material
