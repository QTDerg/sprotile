"""Constants, session-wide caches and Blender version shims shared by the whole add-on.
"""

import bpy
import bmesh


def prefs(context=None):
    """This add-on's preferences, or None when they are not available (the package is not an installed
    add-on, as in the tests, or Blender has no preferences yet). Extensions load as bl_ext.<repo>.sprotile,
    so the key is the package name, not a fixed string."""
    try:
        context = context or bpy.context
        return context.preferences.addons[__package__].preferences
    except Exception:
        return None


def feature_enabled(context, name):
    """True when the sidebar feature `name` (a SprotilePreferences toggle) is on; True when there are no preferences."""
    return bool(getattr(prefs(context), name, True))


# Blender keeps hidden per-element layers for the Python wrappers of a BMesh's faces, corners, edges and vertices.
# It frees them when the last Python reference to an edit mesh's BMesh goes away and adds them again when a script
# next touches an element, and both walk the whole mesh. Sprotile asks for the edit BMesh many times per sidebar
# redraw and per slider tick, so on a large level mesh every redraw cost tens of milliseconds (5.15.2: 80 ms for
# the Texture Blend panel on 78,000 faces; 0.4 ms with the BMesh held). So while a mesh is in Edit Mode and
# Sprotile is looking at it, Sprotile holds one reference to its BMesh. Blender marks a held BMesh invalid when the
# mesh leaves Edit Mode, on Undo and on file load, so a held one is never used after that: it is dropped.
_held_bmeshes = {}
HELD_BMESH_LIMIT = 64


def hold_edit_bmesh(mesh):
    """Keep a reference to this Edit Mode mesh's BMesh so Blender does not rebuild its Python layers on every
    call (see _held_bmeshes). Cheap enough for draw code; never raises."""
    try:
        key = mesh.as_pointer()
        held = _held_bmeshes.get(key)
        if held is not None and held.is_valid:
            return
        if len(_held_bmeshes) >= HELD_BMESH_LIMIT:
            release_held_bmeshes(only_invalid=True)
            if len(_held_bmeshes) >= HELD_BMESH_LIMIT:
                _held_bmeshes.clear()
        _held_bmeshes[key] = bmesh.from_edit_mesh(mesh)
    except Exception:
        _held_bmeshes.pop(mesh.as_pointer() if mesh is not None else None, None)


def keep_undo_in_step():
    """Make Blender's next Undo step remember everything, not only the edit mesh.

    In Edit Mode an Undo step holds the mesh alone. Blend records, materials
    and scene settings live outside it and are taken from the last full
    snapshot, which Blender only writes when it believes they changed -- and a
    Python change to them does not count. Undoing a later operator then put
    those back to an older state than the mesh: faces kept their blend while
    its record, or the grass stage of the material, was gone. Adding and
    removing a datablock is what tells Blender the snapshot is out of date.
    """
    try:
        marker = bpy.data.texts.new('.sprotile undo')
        bpy.data.texts.remove(marker)
    except Exception:
        pass   # read-only state (drawing, rendering): nothing was changed either


def undo_safe(cls):
    """Wrap an undoable operator so every finished run calls keep_undo_in_step."""
    if 'UNDO' not in getattr(cls, 'bl_options', ()):
        return cls

    def finished(result):
        if isinstance(result, set) and 'FINISHED' in result:
            keep_undo_in_step()
        return result

    def wrap(name, make):
        inner = getattr(cls, name, None)
        if inner is None or getattr(inner, 'sprotile_undo_safe', False):
            return
        outer = make(inner)
        outer.sprotile_undo_safe = True
        outer.__doc__ = inner.__doc__
        setattr(cls, name, outer)

    # Blender checks the argument count of these, so no *args.
    wrap('execute', lambda inner: lambda self, context: finished(inner(self, context)))
    wrap('invoke', lambda inner: lambda self, context, event: finished(inner(self, context, event)))
    wrap('modal', lambda inner: lambda self, context, event: finished(inner(self, context, event)))
    return cls


def release_held_bmeshes(only_invalid=False):
    """Drop the held BMesh references: all of them (file load, unregister), or only those Blender invalidated."""
    if not only_invalid:
        _held_bmeshes.clear()
        return
    for key in [key for key, held in _held_bmeshes.items() if not held.is_valid]:
        del _held_bmeshes[key]


_brush_draw_handles = []
_active_brush_operators = []
addon_keymaps = []

# Panel ID used by the old "UV Mapping" add-on and by Sprotile before 5.9.0.
LEGACY_PANEL_ID = 'VIEW3D_PT_atlas_mapper'
_preview_views = {}  # Bounded session-only view state; no RNA/GPU references.
_clipboard_previews = {}  # At most 32 small encoded thumbnails; no RNA references.
_clipboard_preview_pending = {}
RECTANGLE_CORNER_LAYER = 'sprotile_rectangle_corner'
RECTANGLE_CORNER_TAG = 'sprotile_rectangle_corner_version'

PANEL_TOP_MARGIN = 35
PANEL_MIN_WIDTH = 100
PANEL_RESIZE_HANDLE = 8
DOUBLE_CLICK_SECONDS = 0.3
WHEEL_ZOOM_STEP = 1.15  # One notch in, one notch out returns to the same zoom.
ROTATION_ORDER = ('0', '90', '180', '270')
RECENT_TILE_LIMIT = 32
RECENT_TILE_COLUMNS = 8

# Runaway guard for the brush flood fill.  The real bound is "faces touching
# the brush circle"; this only exists so a pathological mesh cannot lock the UI.
BRUSH_MAX_FACES = 20000


# Blender 5.0 made every material node based and deprecated Material.use_nodes;
# reading or writing it warns, and it is due to be removed in 6.0.
MATERIALS_ALWAYS_USE_NODES = bpy.app.version >= (5, 0, 0)


def material_nodes(material):
    """The material's shader node tree, or None when it has none to use."""
    if material is None:
        return None
    if not MATERIALS_ALWAYS_USE_NODES and not material.use_nodes:
        return None
    return material.node_tree
