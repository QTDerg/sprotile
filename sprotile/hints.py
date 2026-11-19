"""Help where the work happens: why a button would refuse right now, and the status line the brushes draw."""

import blf
import bpy
import textwrap


def edit_blocker(context):
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return 'Select a mesh object first'
    if obj.mode == 'VERTEX_PAINT':
        return 'This is Vertex Paint: press Tab for Edit Mode'
    if obj.mode != 'EDIT':
        return 'Press Tab for Edit Mode'
    return None


def faces_selected(context):
    return any(obj.data.total_face_sel for obj in edit_mesh_objects(context))


def action_blocker(context, action):
    """Why this action would refuse right now, in a few words, or None. Only reasons the action itself would give:
    the line under a button must never be wrong."""
    scene, obj = context.scene, context.active_object
    if action == 'paint_b':
        if obj is None or obj.type != 'MESH':
            return 'Select a mesh object first'
        if obj.mode == 'EDIT' and not obj.data.total_face_sel:
            return 'Select the faces to paint on'
        return None
    reason = edit_blocker(context)
    if reason:
        return reason
    if action == 'apply':
        if scene.sprotile_blend_a.image is None:
            return 'Capture A first (pick a tile, then Capture A)'
        if scene.sprotile_blend_b.image is None:
            return 'Capture B first (pick a tile, then Capture B)'
        if not faces_selected(context):
            return 'Select the faces to blend'
    elif action == 'path_brush':
        if scene.sprotile_blend_b.image is None:
            return 'Capture B first: the tile the path is made of'
    elif action == 'remove':
        if not faces_selected(context):
            return 'Select the blended faces first'
    return None


def draw_hint(layout, message, icon='INFO'):
    """Keep task instructions readable when the user narrows the viewport sidebar."""
    region = bpy.context.region
    width = 80
    if region is not None and region.type == 'UI':
        width = max(14, int((region.width / bpy.context.preferences.system.ui_scale - 62) / 7.5))
    lines = layout.column(align=True)
    for index, line in enumerate(textwrap.wrap(message, width=width)):
        row = lines.row()
        row.scale_y = 0.8
        row.label(text=line, icon=icon if index == 0 else 'BLANK1')


def draw_blocker(layout, reason):
    if reason:
        draw_hint(layout, reason, icon='QUESTION')


def tile_text(scene):
    """The tile the brush lays down, as the picker names it."""
    text = f'Tile ({scene.sprotile_active_col}, {scene.sprotile_active_row})'
    mode = scene.sprotile_tile_mode
    if mode != 'FULL':
        text += ' ' + mode.replace('_', ' ').lower()
    rotation, flip_u, flip_v = get_tile_transform(scene)
    if not transform_is_default(rotation, flip_u, flip_v):
        text += ' ' + transform_label(rotation, flip_u, flip_v)
    return text


def capture_text(record, slot):
    if record.image is None:
        return f'{slot} not captured'
    if record.is_sampled:
        return f'{slot} copied'
    return f'{slot} ({record.col}, {record.row})'


def paint_status(scene):
    return [f'Sprotile Paint  |  {tile_text(scene)}  |  Size {scene.sprotile_brush_radius:.0f} px',
            'Drag: paint   [ ]: size   Alt P: pipette a face']


def path_status(scene, erasing=False):
    controls = scene.sprotile_blend_defaults
    style = {'PLAIN': 'plain edge', 'GRASS': 'grass edge', 'SNOW': 'snow edge'}.get(controls.edge_style, 'plain edge')
    return [f'Path Brush{" (erasing)" if erasing else ""}  |  {capture_text(scene.sprotile_blend_b, "B")}  |  {style}'
            f'  |  Feather {controls.width * 100:.0f}%  |  Size {scene.sprotile_brush_radius:.0f} px']


def status_enabled(context):
    preferences = prefs(context)
    return preferences is None or getattr(preferences, 'show_status_line', True)


def draw_status(lines, region_width, origin=(0, 0)):
    """Lines of text centred along the bottom of a viewport. origin: the region's corner when the drawing is in
    window coordinates (a tool cursor), else (0, 0)."""
    try:
        scale = bpy.context.preferences.system.ui_scale
        font = 0
        blf_size(font, round(12 * scale))
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0, 0, 0, 0.9)
        blf.shadow_offset(font, 1, -1)
        y = origin[1] + (22 + 18 * (len(lines) - 1)) * scale
        for index, line in enumerate(lines):
            width = blf.dimensions(font, line)[0]
            blf.position(font, origin[0] + max(8.0, (region_width - width) / 2), y - index * 18 * scale, 0)
            blf.color(font, 1, 1, 1, 0.95 if index == 0 else 0.7)
            blf.draw(font, line)
        blf.disable(font, blf.SHADOW)
    except Exception:
        pass   # text must never break the viewport's drawing


from .core import prefs
from .draw import blf_size
from .tiles import edit_mesh_objects, get_tile_transform, transform_is_default, transform_label
