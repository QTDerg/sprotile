"""The atlas picker overlay, the pipette, Focus Atlas, and starting and stopping the picker.
"""

import bpy
import bmesh
import gpu
import blf
import math
import time
import traceback
from collections import deque
from gpu_extras.batch import batch_for_shader
from bpy.props import EnumProperty
from . import state


def area_is_alive(area):
    """True while `area` still exists somewhere in the file.

    Comparing bpy_struct wrappers only compares the underlying pointer, so this
    stays safe even for an area that has already been closed.
    """
    if area is None:
        return False
    try:
        for screen in bpy.data.screens:
            for other in screen.areas:
                if other == area:
                    return other.type == 'VIEW_3D'
    except Exception:
        return False
    return False


def get_window_region(area):
    if area is None:
        return None
    try:
        for region in area.regions:
            if region.type == 'WINDOW' and region.width > 0 and region.height > 0:
                return region
    except Exception:
        return None
    return None


def visible_overlap_region(area, window_x, window_y):
    """The toolbar, sidebar, header or redo panel drawn over this point, if any.

    Coordinates are window space, like Region.x/y. Blender hides the Adjust
    Last Operation panel by zeroing its position but keeps its old size, so a
    hidden panel still reports a rectangle at the window's bottom-left corner.
    Treating that phantom as real made the picker pass scroll and clicks
    through to the viewport there. Hidden side regions shrink to one pixel.
    """
    if area is None:
        return None
    try:
        left, bottom = area.x, area.y
        right, top = left + area.width, bottom + area.height
        for region in area.regions:
            if region.type == 'WINDOW' or region.width <= 1 or region.height <= 1:
                continue
            x, y = region.x, region.y
            # A visible redo panel always keeps a margin inside its editor.
            if region.type == 'HUD' and x == 0 and y == 0:
                continue
            # Stale geometry outside this editor cannot be covering it.
            if x < left or y < bottom or x + region.width > right + 1 or y + region.height > top + 1:
                continue
            if x <= window_x < x + region.width and y <= window_y < y + region.height:
                return region
    except (ReferenceError, AttributeError):
        return None
    return None


def zoom_direction_preferences():
    """(invert wheel zoom, invert drag/trackpad zoom) from Preferences > Input."""
    try:
        inputs = bpy.context.preferences.inputs
        return bool(inputs.invert_zoom_wheel), bool(inputs.invert_mouse_zoom)
    except AttributeError:
        return False, False


# Wheel and trackpad gestures that zoom or pan the picker.
PICKER_VIEW_EVENTS = frozenset({'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'})


def is_quad_view(area):
    """True when the viewport is split into four views sharing one region.

    The brush projects through a single view matrix, so in Quad View it would
    raycast with the wrong one and paint faces nowhere near the cursor.  Better
    to say so than to silently misbehave.
    """
    try:
        space = area.spaces.active
        return bool(space is not None and space.type == 'VIEW_3D'
                    and len(space.region_quadviews) > 0)
    except Exception:
        return False


def get_region_3d(area):
    """Fetch the view matrix holder fresh - never cache it across events."""
    try:
        space = area.spaces.active
        if space is not None and space.type == 'VIEW_3D':
            return space.region_3d
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Persistent Preview Background Helper Operator
# ---------------------------------------------------------------------------

def remember_preview_view(preview):
    key = getattr(preview, 'view_key', None)
    if key is not None:
        if key not in _preview_views and len(_preview_views) >= 64:
            _preview_views.pop(next(iter(_preview_views)))
        _preview_views[key] = (preview.zoom, preview.offset_x, preview.offset_y)


def set_preview_image(preview, scene, image):
    width, height = image_size(image)
    key = (scene.session_uid, image.session_uid, width, height) if image is not None else None
    if key != getattr(preview, 'view_key', None):
        remember_preview_view(preview)
        preview.zoom, preview.offset_x, preview.offset_y = _preview_views.get(key, (1.0, 0.0, 0.0))
        preview.view_key = key
    preview.image = image
    preview.atlas_width, preview.atlas_height = width, height


def visible_atlas_grid(rect, tile_width, tile_height, cols, rows, rows_total, clip):
    """Generate only visible lines, omitting an unreadably dense zoomed-out grid."""
    if min(tile_width, tile_height) < 4.0:
        return ()
    x0, y0, x1, y1 = clip
    left, top = rect[0], rect[1] + rect[3]
    bottom, right = top - rows*tile_height, left + cols*tile_width
    lo_x, hi_x, lo_y, hi_y = max(left,x0), min(right,x1), max(bottom,y0), min(top,y1)
    if lo_x > hi_x or lo_y > hi_y:
        return ()
    result = []
    for i in range(max(0,math.ceil((lo_x-left)/tile_width)), min(cols,math.floor((hi_x-left)/tile_width))+1):
        x = left + i*tile_width
        result.extend(((x,lo_y),(x,hi_y)))
    for i in range(max(0,math.ceil((top-hi_y)/tile_height)), min(rows,math.floor((top-lo_y)/tile_height))+1):
        y = top - i*tile_height
        result.extend(((lo_x,y),(hi_x,y)))
    return tuple(result)


BLEND_TAG_COLOURS = {'A': (1.0, 0.62, 0.25), 'B': (0.45, 0.95, 0.5)}


def blend_keys_enabled(context):
    """Whether A and B capture blend tiles in the picker: blends are shown, and the keys are not switched off."""
    return feature_enabled(context, 'show_blends') and feature_enabled(context, 'picker_blend_keys')


def captured_blend_tiles(context, scene, image, tile_w_px, tile_h_px):
    """[(slot, column, row)] for the captured blend tiles A and B that are cells of this atlas at this tile size
    (not a copied blend, an overlay image, or a tile of another size)."""
    if image is None or not feature_enabled(context, 'show_blends'):
        return []
    found = []
    for slot in ('A', 'B'):
        record = getattr(scene, 'sprotile_blend_' + slot.lower(), None)
        if (record is not None and record.image == image and not record.is_sampled
                and (record.width_px, record.height_px) == (tile_w_px, tile_h_px)):
            found.append((slot, record.col, record.row))
    return found


class MESH_OT_sprotile_preview_helper(bpy.types.Operator):
    """Manages the persistent texture atlas sidebar and its zoom/pan/click interactions"""
    bl_idname = "mesh.sprotile_preview_helper"
    bl_label = "Picker"
    bl_options = {'INTERNAL'}

    # -- lifecycle ----------------------------------------------------------

    def invoke(self, context, event):

        if state._preview_operator is not None:
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Start the Sprotile preview from a 3D Viewport")
            return {'CANCELLED'}

        self.area = context.area
        self.stop_requested = False
        self.is_panning = False
        self.panel_drag = None
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.hover_col = -1
        self.hover_row = -1
        self.mouse_pos = (-1, -1)
        self.image = None
        self.atlas_width = 0
        self.atlas_height = 0
        self.last_click_time = 0.0
        self.last_click_tile = (-1, -1)
        self.draw_handle = None

        # View state lives on the operator, not on the Scene: writing Scene
        # properties on every scroll/pan event dirtied the file and fought
        # with the undo system.
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.view_key = None
        self.grid_key = None
        self.grid_batch = None
        set_preview_image(self, context.scene, resolve_atlas_image(context.active_object))

        self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, (), 'WINDOW', 'POST_PIXEL'
        )
        state._preview_draw_handle = self.draw_handle
        state._preview_operator = self

        context.window_manager.modal_handler_add(self)
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def finish(self, context=None):
        remember_preview_view(self)
        self.grid_batch = None
        self.grid_key = None
        if getattr(self, 'draw_handle', None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            except Exception:
                pass
            if state._preview_draw_handle is self.draw_handle:
                state._preview_draw_handle = None
            self.draw_handle = None
        if state._preview_operator is self:
            state._preview_operator = None
        self.panel_drag = None
        self.image = None
        tag_redraw_view3d()

    def cancel(self, context):
        # Called by Blender whenever the modal operator is torn down for us.
        self.finish(context)

    # -- layout -------------------------------------------------------------

    def atlas_rect(self, scene, region):
        """Where the atlas image is drawn: (x, y, w, h) or None.

        Single source of truth so hit testing and drawing can never drift.
        """
        if self.atlas_width <= 0 or self.atlas_height <= 0:
            return None

        left, width, top = preview_panel_rect(scene, region)
        margin = 20
        max_w = max(1.0, width - margin * 2)
        max_h = max(1.0, top - margin * 2)

        aspect = self.atlas_width / self.atlas_height
        base_w = max_w
        base_h = base_w / aspect
        if base_h > max_h:
            base_h = max_h
            base_w = base_h * aspect

        display_width = base_w * self.zoom
        display_height = base_h * self.zoom
        if display_width <= 0.0 or display_height <= 0.0:
            return None

        center_x = left + (width / 2.0)
        center_y = top / 2.0
        start_x = center_x - (display_width / 2.0) + self.offset_x
        start_y = center_y - (display_height / 2.0) + self.offset_y
        return start_x, start_y, display_width, display_height

    def tile_metrics(self, scene, rect):
        """Tile geometry on screen: (cols, rows, rows_total, tile_w, tile_h).

        Tiles are drawn at their true size relative to the atlas, anchored to
        its left and top edges - matching exactly what map_face_to_tile() does
        in UV space.  The grid used to be stretched to fill the whole image
        instead, so on an atlas whose size is not an exact multiple of the tile
        size the picker drew tile boundaries where the UV maths did not agree,
        and clicking selected the wrong tile.

        `rows_total` is deliberately fractional: row 0 sits flush against the
        top of the atlas and any remainder falls off the bottom, which is the
        convention the UV mapping and the pipette both already use.
        """
        if self.atlas_width <= 0 or self.atlas_height <= 0:
            return 0, 0, 0.0, 0.0, 0.0
        display_width, display_height = rect[2], rect[3]
        tile_w_px, tile_h_px = get_tile_dims(scene)
        cols = max(0, int(self.atlas_width / tile_w_px))
        rows = max(0, int(self.atlas_height / tile_h_px))
        rows_total = self.atlas_height / tile_h_px
        tile_w_disp = display_width * tile_w_px / self.atlas_width
        tile_h_disp = display_height * tile_h_px / self.atlas_height
        return cols, rows, rows_total, tile_w_disp, tile_h_disp

    @staticmethod
    def tile_origin(rect, rows_total, tile_w_disp, tile_h_disp, col, row):
        """Bottom-left corner of one tile, in region pixels."""
        return (rect[0] + col * tile_w_disp,
                rect[1] + (rows_total - 1 - row) * tile_h_disp)

    # -- interaction --------------------------------------------------------

    def update_hover(self, context, mouse_x, mouse_y):
        self.hover_col = -1
        self.hover_row = -1

        if self.image is None:
            return

        region = get_window_region(self.area)
        if region is None:
            return

        rect = self.atlas_rect(context.scene, region)
        if rect is None:
            return
        start_x, start_y, display_width, display_height = rect

        if (mouse_x < start_x or mouse_x > start_x + display_width or
                mouse_y < start_y or mouse_y > start_y + display_height):
            return

        cols, rows, rows_total, tile_w_disp, tile_h_disp = self.tile_metrics(context.scene, rect)
        if cols <= 0 or rows <= 0 or tile_w_disp <= 0.0 or tile_h_disp <= 0.0:
            return

        # Same formulas the pipette uses, so what you click is what gets mapped.
        col = int((mouse_x - start_x) / tile_w_disp)
        row = int(rows_total - ((mouse_y - start_y) / tile_h_disp))

        # Outside the gridded region (the remainder strip on an atlas that is
        # not an exact multiple of the tile size) selects nothing.
        if not (0 <= col < cols and 0 <= row < rows):
            return

        self.hover_col = col
        self.hover_row = row

    def mouse_over_other_region(self, event):
        """True if the mouse sits over a visible non-WINDOW region (toolbar,
        N-panel, header, the floating 'Adjust Last Operation' panel...).  Those
        overlap the preview rectangle and keep their own clicks and keys.

        The redo panel is the exception for plain scrolling: it has no wheel
        action of its own, and it opens right over the picker's lower-left
        corner after many operators. Ctrl+wheel still edits its fields."""
        region = visible_overlap_region(self.area, event.mouse_x, event.mouse_y)
        if region is None:
            return False
        if region.type == 'HUD' and event.type in PICKER_VIEW_EVENTS and not event.ctrl:
            return False
        return True

    def map_selected(self, context):
        """Run the mapping as a real operator so it lands on the undo stack.

        An operator called from Python skips its undo push unless asked, so the
        old nested call changed UVs with no step of its own and Ctrl+Z undid
        the previous action as well. The internal operator pushes one step and
        never opens the redo panel over the picker.
        """
        try:
            if bpy.ops.mesh.sprotile_picker_map.poll():
                bpy.ops.mesh.sprotile_picker_map('EXEC_DEFAULT', True)
        except Exception:
            traceback.print_exc()

    def capture_hovered(self, context, slot, mouse_x, mouse_y):
        """Make the tile under the mouse the active tile and capture it as blend tile A or B."""
        scene = context.scene
        scene.sprotile_active_col, scene.sprotile_active_row = self.hover_col, self.hover_row
        _recent_tiles.remember(scene, self.image, self.hover_col, self.hover_row)
        try:
            # A real operator call, so the capture is an Undo step of its own (see map_selected).
            bpy.ops.mesh.sprotile_capture_blend_tile('EXEC_DEFAULT', True, slot=slot)
        except Exception:
            traceback.print_exc()
        # The capture starts a new picker interaction, which clears the hover; the mouse has not moved.
        self.update_hover(context, mouse_x, mouse_y)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            # An exception escaping modal() tears the operator down without
            # ever removing the draw handler - that is the stuck overlay.
            traceback.print_exc()
            self.finish(context)
            return {'CANCELLED'}

    def _modal(self, context, event):
        if self.stop_requested:
            self.finish(context)
            return {'FINISHED'}

        # The viewport we were launched in may have been closed, replaced by a
        # different editor, or had its whole workspace swapped out.  Bail
        # cleanly instead of poking a dangling pointer.
        if not area_is_alive(self.area):
            self.finish(context)
            return {'FINISHED'}

        region = get_window_region(self.area)
        if region is None:
            return {'PASS_THROUGH'}

        # A new navigation gesture ends a previous mouse drag, even if a
        # popup or another modal operator intercepted its button-up event.
        if event.type in PICKER_VIEW_EVENTS:
            self.is_panning = False
            self.panel_drag = None

        # Only act on events that belong to our own viewport.
        if context.area is None or context.area != self.area:
            if not self.is_panning and getattr(self, 'panel_drag', None) is None:
                if self.mouse_pos != (-1, -1):
                    self.mouse_pos = (-1, -1)
                    self.area.tag_redraw()
                self.hover_col = -1
                self.hover_row = -1
                return {'PASS_THROUGH'}

        scene = context.scene
        # Unrelated keyboard/timer events need no atlas lookup or redraw.
        if (event.type not in PICKER_VIEW_EVENTS and event.type not in {
                'MOUSEMOVE', 'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE',
                'R', 'X', 'Y', 'A', 'B', 'HOME', 'ESC', 'WINDOW_DEACTIVATE'}):
            return {'PASS_THROUGH'}
        if not self.is_panning and getattr(self, 'panel_drag', None) is None:
            left, width, top = preview_panel_rect(scene, region)
            mx, my = event.mouse_x-region.x, event.mouse_y-region.y
            recent = _recent_tiles.layout(scene, region, self.area, self.image)
            over = (left <= mx <= left+width and 0 <= my <= top) or (recent is not None and recent.contains(mx,my))
            if self.mouse_over_other_region(event) or not over:
                if self.mouse_pos != (-1, -1):
                    self.mouse_pos = (-1, -1)
                    self.hover_col = self.hover_row = -1
                    self.area.tag_redraw()
                return {'PASS_THROUGH'}
        self.area.tag_redraw()

        # event.mouse_region_* is relative to whichever region produced the
        # event (sidebar, toolbar, another area...).  Derive our own region
        # coordinates from window space so the picker never jumps.
        mouse_x = event.mouse_x - region.x
        mouse_y = event.mouse_y - region.y
        self.mouse_pos = (mouse_x, mouse_y)

        # A layout drag owns events until release, even over the sidebar or
        # another viewport. No tile picking, mapping or image panning can fire.
        drag = getattr(self, 'panel_drag', None)
        if drag is not None:
            if event.type == 'MOUSEMOVE':
                drag.update(event.mouse_x)
            elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                drag.update(event.mouse_x)
                drag.commit()
                self.panel_drag = None
            elif (event.type in {'ESC', 'WINDOW_DEACTIVATE'} or
                  (event.type == 'RIGHTMOUSE' and event.value == 'PRESS')):
                self.panel_drag = None
            return {'RUNNING_MODAL'}

        set_preview_image(self, scene, resolve_atlas_image(context.active_object))
        if self.image is None:
            self.atlas_width = 0
            self.atlas_height = 0
            self.hover_col = -1
            self.hover_row = -1
            if self.is_panning and event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'} and event.value == 'RELEASE':
                self.is_panning = False
        else:
            self.atlas_width, self.atlas_height = image_size(self.image)

        # Drag panning owns the mouse until the button comes back up.
        if self.is_panning:
            if event.type == 'MOUSEMOVE':
                self.offset_x += event.mouse_x - self.pan_start_x
                self.offset_y += event.mouse_y - self.pan_start_y
                self.pan_start_x = event.mouse_x
                self.pan_start_y = event.mouse_y
                return {'RUNNING_MODAL'}
            if event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'} and event.value == 'RELEASE':
                self.is_panning = False
                return {'RUNNING_MODAL'}
            if event.type in {'ESC', 'WINDOW_DEACTIVATE'}:
                # Never stay stuck in pan mode if the release is swallowed.
                self.is_panning = False
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if self.mouse_over_other_region(event):
            self.hover_col = -1
            self.hover_row = -1
            self.mouse_pos = (-1, -1)
            return {'PASS_THROUGH'}

        left, width, top = preview_panel_rect(scene, region)
        in_region = 0 <= mouse_x <= region.width and 0 <= mouse_y <= region.height
        in_preview = in_region and (left <= mouse_x <= left + width) and (0 <= mouse_y <= top)
        recent_layout = _recent_tiles.layout(scene, region, self.area, self.image)
        in_recent = in_region and recent_layout is not None and recent_layout.contains(mouse_x, mouse_y)

        if not in_preview or in_recent:
            self.hover_col = -1
            self.hover_row = -1
        if not in_preview and not in_recent:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            if not in_recent:
                self.update_hover(context, mouse_x, mouse_y)
                if mouse_x >= left + width - PANEL_RESIZE_HANDLE:
                    self.hover_col = self.hover_row = -1
            return {'RUNNING_MODAL'}

        if event.type in PICKER_VIEW_EVENTS:
            if in_preview:
                self.view_gesture(scene, region, event, mouse_x, mouse_y)
                self.last_click_tile = (-1, -1)
                self.last_click_time = 0.0
                if not in_recent:
                    self.update_hover(context, mouse_x, mouse_y)
            return {'RUNNING_MODAL'}

        if in_recent and event.type in {'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE'}:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                tile = recent_layout.tile_at(mouse_x, mouse_y)
                if tile is not None:
                    _recent_tiles.select(scene, self.image, tile)
                    self.last_click_tile = (-1, -1)
                    self.last_click_time = 0.0
                    if event.ctrl:
                        self.map_selected(context)
            # Consume the panel background, gaps and releases as well.
            return {'RUNNING_MODAL'}

        if event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.is_panning = True
            self.pan_start_x = event.mouse_x
            self.pan_start_y = event.mouse_y
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self.update_hover(context, mouse_x, mouse_y)
                resizing = mouse_x >= left + width - PANEL_RESIZE_HANDLE
                if resizing or self.hover_col < 0 or self.hover_row < 0:
                    self.panel_drag = PreviewPanelDrag(
                        scene, region, event.mouse_x, 'RESIZE' if resizing else 'MOVE',
                    )
                    self.hover_col = self.hover_row = -1
                    self.last_click_tile = (-1, -1)
                    self.last_click_time = 0.0
                else:
                    self.on_click(context, event, mouse_x, mouse_y)
            # Swallow the release too, otherwise the viewport (or the Sprotile
            # brush tool) reacts to a click that was meant for the picker.
            return {'RUNNING_MODAL'}

        # Tile orientation, only while the cursor is over the picker - R, X and
        # Y keep their normal viewport meaning everywhere else.
        if event.value == 'PRESS':
            picker_key = not (event.alt or event.ctrl or event.oskey)
            if event.type == 'R' and picker_key:
                cycle_rotation(scene, -1 if event.shift else 1)
                return {'RUNNING_MODAL'}
            if event.type == 'X' and picker_key and not event.shift:
                scene.sprotile_flip_u = not scene.sprotile_flip_u
                return {'RUNNING_MODAL'}
            if event.type == 'Y' and picker_key and not event.shift:
                scene.sprotile_flip_v = not scene.sprotile_flip_v
                return {'RUNNING_MODAL'}
            # A and B over a tile capture it for a blend, as Capture A and Capture B do. Anywhere else (off the
            # grid, or with blends switched off in the preferences) the keys keep their normal meaning.
            if (event.type in {'A', 'B'} and picker_key and not event.shift
                    and self.hover_col >= 0 and self.hover_row >= 0 and blend_keys_enabled(context)):
                self.capture_hovered(context, event.type, mouse_x, mouse_y)
                return {'RUNNING_MODAL'}
            if event.type == 'ESC':
                self.finish(context)
                return {'FINISHED'}
            if event.type == 'HOME':
                self.zoom = 1.0
                self.offset_x = 0.0
                self.offset_y = 0.0
                return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}

    def on_click(self, context, event, mouse_x, mouse_y):
        scene = context.scene
        self.update_hover(context, mouse_x, mouse_y)
        if self.hover_col < 0 or self.hover_row < 0:
            return

        tile = (self.hover_col, self.hover_row)
        scene.sprotile_active_col = self.hover_col
        scene.sprotile_active_row = self.hover_row
        _recent_tiles.remember(scene, self.image, *tile)

        if event.ctrl:
            self.map_selected(context)
            return

        # Double click only counts as one when both clicks land on the same
        # tile - browsing quickly across the atlas used to fire a mapping.
        now = time.time()
        if (tile == self.last_click_tile
                and now - self.last_click_time < DOUBLE_CLICK_SECONDS):
            self.map_selected(context)
            self.last_click_time = 0.0
            self.last_click_tile = (-1, -1)
        else:
            self.last_click_time = now
            self.last_click_tile = tile

    def view_gesture(self, scene, region, event, mouse_x, mouse_y):
        """Wheel zooms. A trackpad pans with two fingers, zooms with a pinch,
        or zooms with Ctrl held, matching Blender's Image Editor."""
        invert_wheel, invert_drag = zoom_direction_preferences()
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            zoom_in = (event.type == 'WHEELUPMOUSE') != invert_wheel
            self.zoom_at(scene, region, mouse_x, mouse_y,
                         WHEEL_ZOOM_STEP if zoom_in else 1.0 / WHEEL_ZOOM_STEP)
            return
        # Trackpad events carry their motion as the gap to the previous point.
        dx = event.mouse_x - event.mouse_prev_x
        dy = event.mouse_y - event.mouse_prev_y
        if event.type == 'TRACKPADZOOM' or event.ctrl:
            delta = -(dx + dy)
            if invert_drag:
                delta = -delta
            self.zoom_at(scene, region, mouse_x, mouse_y, max(0.5, min(2.0, 1.0 + delta / 300.0)))
        else:
            self.offset_x += dx
            self.offset_y += dy

    def zoom_at(self, scene, region, mouse_x, mouse_y, factor):
        new_zoom = max(0.1, min(100.0, self.zoom * factor))
        if new_zoom == self.zoom:
            return
        rect = self.atlas_rect(scene, region)
        if rect is not None:
            x, y, w, h = rect
            # Keep the image point under the pointer fixed. Over blank margin
            # use the nearest edge, or a narrow atlas can zoom out of sight.
            anchor_x = min(max(mouse_x, x), x + w)
            anchor_y = min(max(mouse_y, y), y + h)
            scale = new_zoom / self.zoom
            self.offset_x += (anchor_x - (x + w / 2)) * (1 - scale)
            self.offset_y += (anchor_y - (y + h / 2)) * (1 - scale)
        self.zoom = new_zoom

    # -- drawing ------------------------------------------------------------

    def draw_panel_handle(self, shader, left, width, top):
        edge = left + width
        drag = getattr(self, 'panel_drag', None)
        hovered = edge - PANEL_RESIZE_HANDLE <= self.mouse_pos[0] <= edge and 0 <= self.mouse_pos[1] <= top
        active = hovered or (drag is not None and drag.mode == 'RESIZE')
        shader.bind()
        shader.uniform_float("color", (0.12, 0.12, 0.12, 1.0))
        draw_rect_fill(shader, edge - PANEL_RESIZE_HANDLE, 0, edge, top)
        shader.uniform_float("color", (0.15, 0.75, 1.0, 1.0) if active else (0.5, 0.5, 0.5, 1.0))
        for offset in (3, 6):
            draw_line(shader, edge - offset, max(0, top / 2 - 18),
                      edge - offset, min(top, top / 2 + 18))

    def draw_orientation_marker(self, shader, x, y, w, h, rotation, flip_u, flip_v):
        """Little arrow showing which way up the tile will be painted."""
        if w < 16 or h < 16:
            return
        pos = []
        for lu, lv in ((0.5, 0.80), (0.35, 0.58), (0.65, 0.58)):
            tu, tv = apply_tile_transform(lu, lv, rotation, flip_u, flip_v)
            pos.append((x + tu * w, y + tv * h))
        batch = batch_for_shader(shader, 'TRIS', {"pos": pos})
        batch.draw(shader)

    def draw_callback(self):
        """Runs for every 3D viewport - bail out unless it is ours.

        No `context` is captured here.  Storing the invoke-time context and
        using it later is undefined behaviour in Blender and was a direct
        cause of hard crashes.
        """
        scissor_on = False
        blend_on = False
        try:
            context = bpy.context
            if context.area is None or context.area != self.area:
                return
            region = context.region
            if region is None or region.type != 'WINDOW':
                return
            if region.width <= 0 or region.height <= 0:
                return
            # Quad View has four WINDOW regions. Clicks and scrolling are
            # tested against one of them, so draw the picker only there.
            if region != get_window_region(self.area):
                return

            scene = context.scene
            left, width, top = preview_panel_rect(scene, region)

            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.bind()

            gpu.state.blend_set('ALPHA')
            blend_on = True

            # 1. Base panel
            shader.uniform_float("color", (0.07, 0.07, 0.07, 0.96))
            draw_rect_fill(shader, left, 0, left + width, top)

            shader.uniform_float("color", (0.2, 0.2, 0.2, 1.0))
            draw_line(shader, left, 0, left, top)
            draw_line(shader, left + width, 0, left + width, top)
            self.draw_panel_handle(shader, left, width, top)

            # Reserve the grip and clip the atlas and labels to the panel as
            # it shrinks. Recent tiles are drawn after this scissor is removed.
            sc_x = max(0, min(int(left), region.width))
            sc_w = max(0, min(int(width) - PANEL_RESIZE_HANDLE, region.width - sc_x))
            sc_h = max(0, min(int(top), region.height))
            if sc_w > 0 and sc_h > 0:
                gpu.state.scissor_test_set(True)
                gpu.state.scissor_set(sc_x, 0, sc_w, sc_h)
                scissor_on = True

            set_preview_image(self, scene, resolve_atlas_image(context.active_object))
            if self.image is not None:
                self.atlas_width, self.atlas_height = image_size(self.image)
            else:
                self.atlas_width = 0
                self.atlas_height = 0

            if self.image is None or self.atlas_width <= 0 or self.atlas_height <= 0:
                font_id = 0
                blf_size(font_id, 11)
                blf.color(font_id, 0.5, 0.5, 0.5, 1.0)
                blf.position(font_id, left + 20, top // 2, 0)
                blf.draw(font_id, "No active image found.")
                blf.position(font_id, left + 20, (top // 2) - 20, 0)
                blf.draw(font_id, "Select a mesh with a texture.")
                return

            rect = self.atlas_rect(scene, region)
            if rect is None:
                return
            start_x, start_y, display_width, display_height = rect

            # 2. Atlas texture
            try:
                texture = gpu.texture.from_image(self.image)
                tex_shader = get_image_shader()
                tex_shader.bind()
                tex_shader.uniform_sampler("image", texture)
                draw_image_quad(tex_shader, start_x, start_y,
                                start_x + display_width, start_y + display_height)
            except Exception as exc:
                print(f"Sprotile: texture drawing error: {exc}")

            tile_w_px, tile_h_px = get_tile_dims(scene)
            rotation, flip_u, flip_v = get_tile_transform(scene)
            keeps_layout = (scene.sprotile_mapping_mode == 'PATCH'
                            and scene.sprotile_patch_projection == 'EXISTING')
            cols, rows, rows_total, tile_w_disp, tile_h_disp = self.tile_metrics(scene, rect)
            blend_tags = []

            shader.bind()

            # 3. Grid, drawn at true tile size and anchored top-left.  Any
            # remainder is left ungridded rather than stretched over.
            if cols > 0 and rows > 0 and tile_w_disp > 0.0 and tile_h_disp > 0.0:
                shader.uniform_float("color", (1.0, 1.0, 1.0, 0.2))
                grid_key = (rect, tile_w_disp, tile_h_disp, cols, rows, rows_total, left, width, top)
                if grid_key != self.grid_key:
                    grid_vertices = visible_atlas_grid(rect, tile_w_disp, tile_h_disp, cols, rows, rows_total,
                                                       (left,0,left+width-PANEL_RESIZE_HANDLE,top))
                    self.grid_batch = batch_for_shader(shader, 'LINES', {"pos": grid_vertices}) if grid_vertices else None
                    self.grid_key = grid_key
                if self.grid_batch is not None:
                    self.grid_batch.draw(shader)

                # 4. Active (locked-in) tile - cyan, with its orientation arrow
                active_col = scene.sprotile_active_col
                active_row = scene.sprotile_active_row
                if 0 <= active_col < cols and 0 <= active_row < rows:
                    tx, ty = self.tile_origin(rect, rows_total, tile_w_disp, tile_h_disp,
                                              active_col, active_row)
                    shader.uniform_float("color", (0.0, 0.6, 1.0, 0.35))
                    draw_rect_fill(shader, tx, ty, tx + tile_w_disp, ty + tile_h_disp)
                    shader.uniform_float("color", (0.0, 0.8, 1.0, 0.95))
                    draw_rect_outline(shader, tx, ty, tx + tile_w_disp, ty + tile_h_disp)
                    if not keeps_layout and not transform_is_default(rotation, flip_u, flip_v):
                        shader.uniform_float("color", (0.6, 0.95, 1.0, 0.9))
                        self.draw_orientation_marker(
                            shader, tx, ty, tile_w_disp, tile_h_disp,
                            rotation, flip_u, flip_v,
                        )

                # 5. Hover tile - yellow
                if 0 <= self.hover_col < cols and 0 <= self.hover_row < rows:
                    tx, ty = self.tile_origin(rect, rows_total, tile_w_disp, tile_h_disp,
                                              self.hover_col, self.hover_row)
                    shader.uniform_float("color", (1.0, 0.9, 0.0, 0.3))
                    draw_rect_fill(shader, tx, ty, tx + tile_w_disp, ty + tile_h_disp)

                # 5b. The tiles captured as blend A and B: a corner tag each, so both can be picked here
                # without looking at the sidebar.
                for slot, col, row in captured_blend_tiles(context, scene, self.image, tile_w_px, tile_h_px):
                    if not (0 <= col < cols and 0 <= row < rows):
                        continue
                    tx, ty = self.tile_origin(rect, rows_total, tile_w_disp, tile_h_disp, col, row)
                    size = max(9.0, min(16.0, tile_h_disp * 0.42, tile_w_disp * 0.42))
                    x0 = tx + (size + 1 if slot == 'B' else 0)   # B sits beside A when both are one tile
                    colour = BLEND_TAG_COLOURS[slot]
                    shader.uniform_float("color", colour + (0.95,))
                    draw_rect_outline(shader, tx, ty, tx + tile_w_disp, ty + tile_h_disp)
                    draw_rect_fill(shader, x0, ty + tile_h_disp - size, x0 + size, ty + tile_h_disp)
                    blend_tags.append((slot, x0, ty + tile_h_disp - size, size))

            # 6. Header text
            font_id = 0
            for slot, x0, y0, size in blend_tags:
                blf_size(font_id, size * 0.8)
                blf.color(font_id, 0.05, 0.05, 0.05, 1.0)
                text_w, text_h = blf.dimensions(font_id, slot)
                blf.position(font_id, x0 + (size - text_w) / 2, y0 + (size - text_h) / 2, 0)
                blf.draw(font_id, slot)
            blf_size(font_id, 11)

            title = f"Tile: {scene.sprotile_active_col}, {scene.sprotile_active_row} ({tile_w_px}x{tile_h_px})"
            if keeps_layout:
                title += "  Existing UV layout"
            elif not transform_is_default(rotation, flip_u, flip_v):
                title += f"  {transform_label(rotation, flip_u, flip_v)}"
            blf.color(font_id, 0.95, 0.95, 0.95, 1.0)
            blf.position(font_id, left + 10, region.height - 65, 0)
            blf.draw(font_id, title)

            name = self.image.name
            if len(name) > 22:
                name = name[:21] + "..."
            blf.color(font_id, 0.55, 0.8, 1.0, 1.0)
            blf.position(font_id, left + 10, region.height - 85, 0)
            blf.draw(font_id, f"Atlas: {name}  {cols}x{rows} tiles")

            y = region.height - 105

            # An atlas that is not a whole number of tiles loses its last
            # column/row.  Say so rather than quietly showing a short grid.
            remainder_w = self.atlas_width - cols * tile_w_px
            remainder_h = self.atlas_height - rows * tile_h_px
            if cols <= 0 or rows <= 0:
                blf.color(font_id, 1.0, 0.55, 0.4, 1.0)
                blf.position(font_id, left + 10, y, 0)
                blf.draw(font_id, f"Tile {tile_w_px}x{tile_h_px} is larger than the atlas")
                y -= 20
            elif remainder_w or remainder_h:
                blf.color(font_id, 1.0, 0.75, 0.3, 1.0)
                blf.position(font_id, left + 10, y, 0)
                blf.draw(font_id, f"{self.atlas_width}x{self.atlas_height} leaves {remainder_w}x{remainder_h}px unused")
                y -= 20

            blf.color(font_id, 0.6, 0.6, 0.6, 1.0)
            blf.position(font_id, left + 10, y, 0)
            blf.draw(font_id, "MMB/RMB: Pan | Wheel: Zoom | Home: Reset")
            blf.position(font_id, left + 10, y - 20, 0)
            blf.draw(font_id, "R / Shift+R: Rotate | X / Y: Flip")
            blf.position(font_id, left + 10, y - 40, 0)
            blf.draw(font_id, "Ctrl+LMB / Double-Click: Map | Esc: Close")
            blf.position(font_id, left + 10, y - 60, 0)
            blf.draw(font_id, "LMB Drag empty space: Move | Right edge: Resize")
            if blend_keys_enabled(context):
                blf.position(font_id, left + 10, y - 80, 0)
                blf.draw(font_id, "A / B over a tile: capture blend tile A / B")

            if scissor_on:
                gpu.state.scissor_test_set(False)
                scissor_on = False

            # Draw after removing the atlas scissor so this independent panel
            # remains visible at the bottom-right of the viewport.
            _recent_tiles.draw(scene, region, self.area, self.image, self.mouse_pos)

        except Exception:
            # An exception escaping a draw handler leaves the GPU state broken
            # and repeats on every single redraw.
            traceback.print_exc()
        finally:
            try:
                if scissor_on:
                    gpu.state.scissor_test_set(False)
                if blend_on:
                    gpu.state.blend_set('NONE')
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sprotile Pipette (Eyedropper) Operator
# ---------------------------------------------------------------------------

def tile_cell_from_uvs(uvs, tile_w, tile_h, atlas_w, atlas_h, tolerance_px=.01):
    """Find the single atlas cell containing every loop, allowing float storage error."""
    validate_tile_dimensions(tile_w, tile_h, atlas_w, atlas_h)
    if not uvs or not all(math.isfinite(value) for uv in uvs for value in uv):
        raise ValueError('Face has invalid UV coordinates; repair or remap it first.')
    u = sum(uv.x for uv in uvs) / len(uvs)
    v = sum(uv.y for uv in uvs) / len(uvs)
    col = max(0, min(atlas_w // tile_w - 1, int(u * atlas_w / tile_w)))
    row = max(0, min(atlas_h // tile_h - 1, int((1 - v) * atlas_h / tile_h)))
    x, y, w, h = tile_uv_bounds(col, row, tile_w, tile_h, atlas_w, atlas_h)
    tol_u, tol_v = tolerance_px / atlas_w, tolerance_px / atlas_h
    if any(not (x-tol_u <= uv.x <= x+w+tol_u and y-tol_v <= uv.y <= y+h+tol_v) for uv in uvs):
        raise ValueError('Face UVs do not fit one tile; check Tile Size or choose another face.')
    return col, row


def pipette_uv_layer(mesh, bm, texture):
    """Read the atlas sampler's UV input, without changing the mesh's edit UV."""
    if texture.projection != 'FLAT':
        raise ValueError('This atlas uses projected coordinates; Pipette needs a flat UV mapping.')
    socket = texture.inputs['Vector']
    name = ''
    visited = set()
    while socket.is_linked:
        link = socket.links[0]
        if not link.is_valid or socket.as_pointer() in visited:
            raise ValueError('The atlas UV input has an invalid link.')
        visited.add(socket.as_pointer())
        source = link.from_socket
        node = source.node
        if node.type == 'REROUTE':
            socket = node.inputs[0]
            continue
        if node.type == 'UVMAP' and not node.from_instancer:
            name = node.uv_map
        elif node.type != 'TEX_COORD' or source.name != 'UV' or node.from_instancer:
            raise ValueError('Cannot identify a direct atlas UV input; Pipette left the brush unchanged.')
        break
    if not name:
        name = next((uv.name for uv in mesh.uv_layers if uv.active_render), '')
    layer = bm.loops.layers.uv.get(name)
    if layer is None:
        raise ValueError('The UV map used by the atlas is missing.')
    return layer


def painted_tile_transform(uvs, reference, col, row, dims, atlas):
    """Recognise a per-face mapping and fit its original equal-pixel inset."""
    if reference is None:
        return None
    x, y, w, h = tile_uv_bounds(col, row, *dims, *atlas)
    local = [((uv.x-x)/w, (uv.y-y)/h) for uv in uvs]
    transform = detect_tile_transform(local, reference)
    canonical = [apply_tile_transform(*uv, *transform) for uv in reference]
    # In atlas pixels, adding inset moves a corner from t*size to
    # t*size + inset*(1-2*t). This also works for non-corner n-gon UVs.
    pairs = [((1-2*t), (actual-t)*size)
             for point, expected in zip(local, canonical)
             for actual, t, size in zip(point, expected, dims)]
    weight = sum(k*k for k, _ in pairs)
    if not weight:
        return None
    inset = sum(k*delta for k, delta in pairs) / weight
    inset = min(max(inset, 0.), min(dims)*.499)
    tolerance = max(.001, max(atlas)*1.2e-7)
    if all(abs(delta-k*inset) <= tolerance for k, delta in pairs):
        return transform, (0. if inset < tolerance else inset)
    # Imported corners can reach one texel across a grid line on just one
    # side. They establish the tile and turn, but have no uniform inset.
    if min(dims) >= 8 and all(abs(delta) <= 1+tolerance for _, delta in pairs):
        return transform, None
    return None


def uv_extents(points):
    return (min(p.x for p in points), max(p.x for p in points),
            min(p.y for p in points), max(p.y for p in points))


def cell_margins(extents, rect, atlas):
    lo_u, hi_u, lo_v, hi_v = extents
    x, y, w, h = rect
    return ((lo_u-x)*atlas[0], (x+w-hi_u)*atlas[0], (lo_v-y)*atlas[1], (y+h-hi_v)*atlas[1])


def pipette_tile_grid(face, layer, obj, size, atlas):
    """Infer Full / Half Wide / Half Tall at the atlas's configured base size."""
    uvs = [loop[layer].uv.copy() for loop in face.loops]
    if not all(math.isfinite(v) for uv in uvs for v in uv):
        raise ValueError('Face has invalid UVs; repair or remap it first.')
    if any(not (-.01/atlas[0] <= uv.x <= 1+.01/atlas[0] and
                -.01/atlas[1] <= uv.y <= 1+.01/atlas[1]) for uv in uvs):
        raise ValueError('Face UVs extend outside the atlas.')
    origin = uvs[0]
    if sum((a.x-origin.x)*(b.y-origin.y) - (a.y-origin.y)*(b.x-origin.x)
           for a, b in zip(uvs, uvs[1:]+uvs[:1])) == 0:
        raise ValueError('The sampled UVs have zero area; choose another face.')
    try:
        reference = upright_face_uvs(face, paint_orientation_matrix(obj))
    except ValueError:
        reference = None
    extents = uv_extents(uvs)
    candidates = {}
    bounds = {}
    for mode in ('FULL','HALF_WIDE','HALF_TALL'):
        dims = tile_dimensions(size, mode)
        if dims in candidates:
            continue  # Both half modes equal Full at Size 1.
        try:
            col, row = tile_cell_from_uvs(uvs, *dims, *atlas, tolerance_px=1.01)
        except ValueError:
            continue
        rect = tile_uv_bounds(col, row, *dims, *atlas)
        margins = cell_margins(extents, rect, atlas)
        # One-pixel export overshoot is credible only when the UV footprint
        # reaches all four expected edges, including on a skewed triangle.
        if min(margins) < -.01 and (min(dims) < 8 or max(map(abs,margins)) > 1.01):
            continue
        placement = painted_tile_transform(uvs, reference, col, row, dims, atlas)
        candidates[dims] = (mode, col, row, placement)
        bounds[dims] = rect
    if not candidates:
        raise ValueError('Face UVs do not fit one Full or Half tile. Check the atlas Tile Size.')
    if len(candidates) == 1:
        return next(iter(candidates.values()))

    # A partial face may lie inside more than one grid. Follow continuous UV
    # edges inside the candidate cells to see how much of the tile its patch
    # uses. Never grow across an atlas cell, seam, or unrelated material.
    points = list(uvs)
    seen = {face}
    queue = deque([face])
    while queue and len(seen) <= 256:
        current = queue.popleft()
        for loop in current.loops:
            other = loop.link_loop_radial_next
            if (other.face in seen or loop.edge.seam or not loop.edge.is_manifold
                    or other.face.material_index != face.material_index):
                continue
            end = loop.link_loop_next
            start2, end2 = other, other.link_loop_next
            if start2.vert != loop.vert:
                start2, end2 = end2, start2
            if loop[layer].uv != start2[layer].uv or end[layer].uv != end2[layer].uv:
                continue
            more = [l[layer].uv.copy() for l in other.face.loops]
            if any(all(x-.01/atlas[0] <= uv.x <= x+w+.01/atlas[0] and
                       y-.01/atlas[1] <= uv.y <= y+h+.01/atlas[1] for uv in more)
                   for x,y,w,h in bounds.values()):
                points.extend(more)
                queue.append(other.face)
            seen.add(other.face)
    if queue:
        raise ValueError('Too many connected faces to identify a single tile reliably. Sample a fuller face.')
    # Imported skewed layouts can reach all tile edges even when they do
    # not match our per-face orientation. Use their bounds, not a guessed turn.
    extents = uv_extents(points)
    matches = []
    containing = []
    for dim, rect in bounds.items():
        margins = cell_margins(extents, rect, atlas)
        sample = candidates[dim]
        aligned = min(dim) >= 8 and max(map(abs,margins)) <= 1.01
        if min(margins) >= -.01 or aligned:
            containing.append(sample)
            if aligned or max(margins)-min(margins) <= .02:
                matches.append(containing[-1])
    certain = [sample for sample in containing if sample[3] is not None]
    if len(certain) == 1:
        return certain[0]
    if len(containing) == 1:
        return containing[0]
    if len(matches) == 1:
        return matches[0]
    raise ValueError('Partial UVs fit multiple tile shapes. Sample a fuller face of the tile or pick it in the atlas.')


class MESH_OT_sprotile_pipette(bpy.types.Operator):
    """Sample the active face's tile and orientation, or copy its A/B blend for the next Apply"""
    bl_idname = "mesh.sprotile_pipette"
    bl_label = "Pipette"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        obj = context.active_object
        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            self.report({'WARNING'}, "Mesh has no UV map to sample; map a tile first.")
            return {'CANCELLED'}

        # Prefer the active face - that is the one you clicked last.  Otherwise
        # fall back to the original rule of exactly one selected face.
        face = bm.faces.active
        if face is None or not face.is_valid or not face.select or face.hide:
            selected_faces = [f for f in bm.faces if f.select and not f.hide]
            if len(selected_faces) != 1:
                self.report({'WARNING'}, "Select one face (or make the face you want active) to pipette from.")
                return {'CANCELLED'}
            face = selected_faces[0]

        material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
        membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
        if material and (material.get('sprotile_blend_material') or
                         (material.get(SHARED_MATERIAL_TAG) and membership is not None and face[membership] > 0)):
            try:follows = copy_blend_to_scene(context)
            except ValueError as error:
                self.report({'WARNING'},str(error));return {'CANCELLED'}
            tag_redraw_view3d()
            self.report({'INFO'},'Copied the blend into New Blend' + ('; the picker shows its tile A' if follows else '')
                        + '. Select the target faces and Apply Blend')
            return {'FINISHED'}

        # Sample against the atlas this face actually uses, not just the first
        # image node on the object.
        texture = atlas_node_from_material(material)
        if texture is None:
            self.report({'ERROR'}, "No active image found inside materials.")
            return {'CANCELLED'}

        image = texture.image
        atlas_width, atlas_height = image_size(image)
        if atlas_width <= 0 or atlas_height <= 0:
            self.report({'ERROR'}, "Atlas image has no pixel data.")
            return {'CANCELLED'}

        scene = context.scene
        try:
            uv_layer = pipette_uv_layer(me, bm, texture)
            mode, col, row, placement = pipette_tile_grid(
                face, uv_layer, obj, scene.sprotile_tile_size, (atlas_width, atlas_height))
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}

        # Commit only after the source atlas, UV layer and grid all agree.
        bm.faces.active = face
        scene.sprotile_tile_mode = mode
        scene.sprotile_active_col = col
        scene.sprotile_active_row = row
        orientation = 'partial layout; current orientation and inset kept'
        if placement is not None:
            (rotation, flip_u, flip_v), inset = placement
            scene.sprotile_rotation = str(rotation)
            scene.sprotile_flip_u = flip_u
            scene.sprotile_flip_v = flip_v
            if inset is not None:
                scene.sprotile_use_uv_inset = bool(inset)
                if inset:
                    scene.sprotile_uv_inset_px = inset
            orientation = transform_label(rotation, flip_u, flip_v)
        _recent_tiles.remember(scene, image, col, row)

        self.report({'INFO'}, f"Pipetted {mode.replace('_', ' ').title()} ({col}, {row}) {orientation}")
        tag_redraw_view3d()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Start / Stop Operators
# ---------------------------------------------------------------------------

def focus_preview_tile(preview, scene, region, col, row, tile_width, tile_height):
    if region is None or region.width <= 1 or region.height <= 1:
        raise ValueError('The 3D Viewport is unavailable; reopen the atlas in a visible viewport')
    validate_tile_dimensions(tile_width,tile_height,preview.atlas_width,preview.atlas_height)
    # Match painting after the grid size changes and leaves a stale tile index.
    col = max(0,min(preview.atlas_width//tile_width-1,col))
    row = max(0,min(preview.atlas_height//tile_height-1,row))
    bounds = tile_uv_bounds(col,row,tile_width,tile_height,preview.atlas_width,preview.atlas_height)
    focus_preview_bounds(preview,scene,region,bounds)


def focus_preview_bounds(preview,scene,region,bounds):
    if region is None or region.width <= 1 or region.height <= 1:
        raise ValueError('The 3D Viewport is unavailable')
    preview.zoom, preview.offset_x, preview.offset_y = 1.0, 0.0, 0.0
    rect = preview.atlas_rect(scene,region)
    if rect is None:
        return
    left,width,top = preview_panel_rect(scene,region)
    target = max(16.0,min(width-40.0,top-40.0)*.7)
    preview.zoom = max(.1,min(100.0,target/max(rect[2]*bounds[2],rect[3]*bounds[3])))
    rect = preview.atlas_rect(scene,region)
    preview.offset_x = left+width/2 - (rect[0]+(bounds[0]+bounds[2]/2)*rect[2])
    preview.offset_y = top/2 - (rect[1]+(bounds[1]+bounds[3]/2)*rect[3])


class MESH_OT_sprotile_focus_atlas(bpy.types.Operator):
    """Find a tile in the atlas without changing its selection or mapping"""
    bl_idname = 'mesh.sprotile_focus_atlas'
    bl_label = 'Focus Tile'
    target: EnumProperty(items=[('CURRENT','Current','Current tile'),('A','A','Captured A'),
                                ('B','B','Captured B'),('ALL','All','Fit the complete atlas')],default='CURRENT')

    def execute(self,context):
        image = resolve_atlas_image(context.active_object)
        if image is None or context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'},'Select a textured mesh in a 3D Viewport')
            return {'CANCELLED'}
        scene = context.scene
        sampled_bounds = None
        if self.target in {'A','B'}:
            try:tile = validated_blend_tile(getattr(scene,'sprotile_blend_'+self.target.lower()))
            except ValueError as error:
                self.report({'WARNING'},str(error));return {'CANCELLED'}
            if tile['image'] != image:
                self.report({'WARNING'},'This capture uses another atlas; select a mesh using that atlas first')
                return {'CANCELLED'}
            col,row,tw,th = tile['col'],tile['row'],tile['width_px'],tile['height_px']
            if tile.get('is_sampled',False):sampled_bounds = sample_uv_bounds(tile['sample_uv'])
        else:
            col,row = scene.sprotile_active_col,scene.sprotile_active_row
            tw,th = get_tile_dims(scene)
        region = get_window_region(context.area)
        if region is None or region.width <= 1 or region.height <= 1:
            self.report({'WARNING'},'The 3D Viewport is unavailable')
            return {'CANCELLED'}
        try:
            if self.target != 'ALL' and sampled_bounds is None:
                validate_tile_dimensions(tw,th,*image_size(image))
            elif min(image_size(image)) <= 0:
                raise ValueError('The atlas image has no pixel data')
        except ValueError as error:
            self.report({'WARNING'},str(error));return {'CANCELLED'}
        if state._preview_operator is None:
            bpy.ops.mesh.sprotile_preview_start('INVOKE_DEFAULT')
        preview = state._preview_operator
        if preview is None or preview.area != context.area:
            self.report({'WARNING'},'Use Focus in the viewport where the atlas preview is open')
            return {'CANCELLED'}
        set_preview_image(preview,scene,image)
        if self.target == 'ALL':
            preview.zoom,preview.offset_x,preview.offset_y = 1.0,0.0,0.0
        else:
            try:
                if sampled_bounds is not None:focus_preview_bounds(preview,scene,region,sampled_bounds)
                else:focus_preview_tile(preview,scene,region,col,row,tw,th)
            except ValueError as error:
                self.report({'WARNING'},str(error));return {'CANCELLED'}
        reset_preview_interaction()
        return {'FINISHED'}


class MESH_OT_sprotile_preview_start(bpy.types.Operator):
    """Open the atlas picker in the 3D Viewport"""
    bl_idname = "mesh.sprotile_preview_start"
    bl_label = "Open Picker"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        if state._preview_operator is not None:
            # Stale global from a run that died without cleanup - reset it so
            # the picker is not locked out for the rest of the session.
            if area_is_alive(getattr(state._preview_operator, 'area', None)):
                self.report({'INFO'}, "The Sprotile preview is already open in another viewport")
                return {'CANCELLED'}
            force_cleanup()
        bpy.ops.mesh.sprotile_preview_helper('INVOKE_DEFAULT')
        return {'FINISHED'}


class MESH_OT_sprotile_preview_stop(bpy.types.Operator):
    """Close the atlas picker"""
    bl_idname = "mesh.sprotile_preview_stop"
    bl_label = "Close Picker"

    def execute(self, context):
        if state._preview_operator is not None:
            state._preview_operator.stop_requested = True
            # If the modal no longer receives events (its area went away) it
            # will never see the flag - tear it down directly.
            if not area_is_alive(getattr(state._preview_operator, 'area', None)):
                force_cleanup()
        else:
            force_cleanup()
        tag_redraw_view3d()
        return {'FINISHED'}


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import DOUBLE_CLICK_SECONDS, PANEL_RESIZE_HANDLE, WHEEL_ZOOM_STEP, _preview_views, feature_enabled
from .draw import (
    blf_size, draw_image_quad, draw_line, draw_rect_fill, draw_rect_outline, get_image_shader,
    tag_redraw_view3d,
)
from .tiles import (
    PreviewPanelDrag, _recent_tiles, apply_tile_transform, atlas_node_from_material, cycle_rotation,
    detect_tile_transform, get_tile_dims, get_tile_transform, image_size, is_editable_mesh,
    preview_panel_rect, resolve_atlas_image, tile_dimensions, tile_uv_bounds, transform_is_default,
    transform_label, validate_tile_dimensions,
)
from .mapping import paint_orientation_matrix, upright_face_uvs
from .blend_settings import SHARED_MATERIAL_TAG, SHARED_PATCH_ID
from .blend_plan import validated_blend_tile
from .blend_recipes import copy_blend_to_scene, sample_uv_bounds
from .blend_ops import reset_preview_interaction
from .cleanup import force_cleanup
