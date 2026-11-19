"""Shut down Sprotile's modal operators and draw handlers when a file loads or the add-on unregisters.
"""

import bpy
from . import state


# ---------------------------------------------------------------------------
# Cleanup / handlers
# ---------------------------------------------------------------------------

def force_cleanup():
    """Shut down every modal operator we own and remove its draw handler.

    Modal operators are killed without calling cancel() in several situations
    (loading a file, reloading scripts, closing the window).  Without this the
    overlay keeps drawing forever and the picker can never be reopened.
    """
    if state._corner_draw_handle is not None:
        try:bpy.types.SpaceView3D.draw_handler_remove(state._corner_draw_handle,'WINDOW')
        except (ValueError,ReferenceError):pass
        state._corner_draw_handle=None

    if state._preview_operator is not None:
        try:
            state._preview_operator.stop_requested = True
            state._preview_operator.draw_handle = None
            state._preview_operator.view_key = None
            state._preview_operator.grid_batch = None
        except Exception:
            pass
        state._preview_operator = None

    if state._preview_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(state._preview_draw_handle, 'WINDOW')
        except Exception:
            pass
        state._preview_draw_handle = None

    for operator in list(_active_brush_operators):
        try:
            operator.stop_requested = True
            operator.cleanup()
        except Exception:
            pass
    _active_brush_operators.clear()

    for handle in list(_brush_draw_handles):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
        except Exception:
            pass
    _brush_draw_handles.clear()

    state._image_shader = None
    release_held_bmeshes()
    forget_occluders()
    _draw_cache.clear()
    _scan_cache.clear()
    clear_overlay_thumbnail_cache()
    _recent_tiles.clear()
    _shared_patch_hints.clear()
    _preview_views.clear()
    if bpy.app.timers.is_registered(process_clipboard_previews):bpy.app.timers.unregister(process_clipboard_previews)
    _clipboard_previews.clear();_clipboard_preview_pending.clear()
    clear_preset_previews()
    tag_redraw_view3d()


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import (
    _active_brush_operators, _brush_draw_handles, _clipboard_preview_pending, _clipboard_previews,
    _preview_views, release_held_bmeshes,
)
from .draw import tag_redraw_view3d
from .tiles import _recent_tiles
from .blend_settings import _shared_patch_hints
from .presets import clear_preset_previews, process_clipboard_previews
from .blend_list import _draw_cache
from .overlay_scan import _scan_cache, clear_overlay_thumbnail_cache
from .brush import forget_occluders
