"""Atlas tiles: turns and flips, tile sizes and UV bounds, finding the atlas on a material, editable
meshes and the Recent Tiles strip.
"""

import bmesh
import gpu
import blf
import math
from collections import defaultdict, namedtuple
from . import state


# ---------------------------------------------------------------------------
# Tile transform (rotation / flip)
#
# The transform is applied to the tile's local 0-1 coordinates *before* they
# are scaled into the atlas, so it can never bleed into a neighbouring tile and
# it behaves identically for tris, quads and n-gons.
#
# Note: this is applied when a face is *painted*.  Changing the rotation does
# not restyle faces that were mapped earlier - re-map them (press 4, or
# Ctrl+click the tile) to apply the new orientation.
# ---------------------------------------------------------------------------

# Exact cos/sin for the four quarter turns - avoids 6.1e-17 dust in the UVs.
_ROT_COS_SIN = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}


def apply_tile_transform(u, v, rotation, flip_u, flip_v):
    """Rotate about the tile centre, then flip.  Input and output are 0-1."""
    c, s = _ROT_COS_SIN.get(int(rotation) % 360, (1.0, 0.0))
    du, dv = u - 0.5, v - 0.5
    u = 0.5 + du * c - dv * s
    v = 0.5 + du * s + dv * c
    if flip_u:
        u = 1.0 - u
    if flip_v:
        v = 1.0 - v
    return u, v


def base_local_uv(i, num_verts):
    """Untransformed 0-1 coordinate within the tile for loop `i`."""
    if num_verts == 3:
        return ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))[i % 3]
    if num_verts == 4:
        return ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))[i % 4]
    angle = (i / num_verts) * math.tau
    return (0.5 + 0.5 * math.cos(angle), 0.5 + 0.5 * math.sin(angle))


def get_tile_transform(scene):
    return (int(scene.sprotile_rotation),
            bool(scene.sprotile_flip_u),
            bool(scene.sprotile_flip_v))


def transform_label(rotation, flip_u, flip_v):
    parts = [f"{rotation}deg"]
    if flip_u:
        parts.append("flipU")
    if flip_v:
        parts.append("flipV")
    return " ".join(parts)


def transform_is_default(rotation, flip_u, flip_v):
    return rotation == 0 and not flip_u and not flip_v


def cycle_rotation(scene, delta_steps):
    i = ROTATION_ORDER.index(scene.sprotile_rotation)
    scene.sprotile_rotation = ROTATION_ORDER[(i + delta_steps) % len(ROTATION_ORDER)]


def detect_tile_transform(local_uvs, reference_uvs=None):
    """Best-matching (rotation, flip_u, flip_v) for a face's local tile UVs.

    Every candidate is generated with the exact same code that paints them, so
    a tile painted by Sprotile reads back as the transform it was painted with.
    Ties resolve toward 0 degrees / no flips because that is the first
    candidate tried and later ones must beat it outright.
    """
    n = len(local_uvs)
    reference_uvs = reference_uvs if reference_uvs is not None else [base_local_uv(i, n) for i in range(n)]
    best = (0, False, False)
    best_err = None
    for flip_v in (False, True):
        for flip_u in (False, True):
            for rotation in (0, 90, 180, 270):
                err = 0.0
                for i, (au, av) in enumerate(local_uvs):
                    bu, bv = reference_uvs[i]
                    bu, bv = apply_tile_transform(bu, bv, rotation, flip_u, flip_v)
                    err += (au - bu) ** 2 + (av - bv) ** 2
                if best_err is None or err < best_err - 1e-9:
                    best_err = err
                    best = (rotation, flip_u, flip_v)
    return best


# ---------------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------------

def get_tile_dims(scene):
    """Tile size in pixels, guaranteed non-zero.

    A 1px tile in one of the half modes used to collapse to 0px and raise
    ZeroDivisionError deep inside drawing.
    """
    return tile_dimensions(scene.sprotile_tile_size, scene.sprotile_tile_mode)


def tile_dimensions(size, mode):
    s = max(1, size)
    if mode == 'HALF_WIDE':
        return s, max(1, s // 2)
    if mode == 'HALF_TALL':
        return max(1, s // 2), s
    return s, s


def viewport_visible_width(region, area):
    """Window-region width excluding an overlapping right sidebar."""
    right = region.width
    if area is not None:
        for other in area.regions:
            if other.type == 'UI' and other.width > 1 and other.x > region.x:
                right = min(right, other.x - region.x)
    return right


def preview_available_width(region):
    area = getattr(state._preview_operator, 'area', None)
    return viewport_visible_width(region, area if get_window_region(area) == region else None)


def preview_panel_rect(scene, region):
    """Clamped picker rectangle in region pixels: (left, width, top).

    One definition, used by the overlay, the hit test and the brush dead zone -
    if these ever drift apart the panel and the area it blocks stop matching.
    """
    left, width = scene.sprotile_preview_left, scene.sprotile_preview_width
    drag = getattr(state._preview_operator, 'panel_drag', None)
    if drag is not None and drag.scene == scene and drag.region == region:
        left, width = drag.left, drag.width
    available = preview_available_width(region)
    minimum = min(PANEL_MIN_WIDTH, available)
    left = max(0, min(int(left), max(0, available - minimum)))
    width = max(minimum, min(int(width), available - left))
    top = max(1, region.height - PANEL_TOP_MARGIN)
    return left, width, top


class PreviewPanelDrag:
    """Transient horizontal layout; commit Scene properties only on release."""

    def __init__(self, scene, region, mouse_x, mode):
        self.scene = scene
        self.region = region
        self.mode = mode
        self.start_x = mouse_x  # Window space, stable when crossing UI regions.
        self.start_left, self.start_width, _ = preview_panel_rect(scene, region)
        self.left, self.width = self.start_left, self.start_width

    def update(self, mouse_x):
        delta = mouse_x - self.start_x
        available = preview_available_width(self.region)
        minimum = min(PANEL_MIN_WIDTH, available)
        if self.mode == 'RESIZE':
            self.left = min(self.start_left, max(0, available - minimum))
            self.width = max(minimum, min(self.start_width + delta, available - self.left))
        else:
            self.width = min(self.start_width, available)
            self.left = max(0, min(self.start_left + delta, available - self.width))

    def commit(self):
        self.scene.sprotile_preview_left = round(self.left)
        self.scene.sprotile_preview_width = round(self.width)


def image_size(image):
    """Safe size read; a freed or unloaded image raises instead of crashing."""
    try:
        return int(image.size[0]), int(image.size[1])
    except (ReferenceError, AttributeError, IndexError):
        return 0, 0


def atlas_node_from_material(mat):
    if material_nodes(mat) is None:
        return None

    # Node order is unrelated to what the material displays. Follow the active
    # output graph, including group sockets, so a spare/normal-map image cannot
    # displace the atlas wired into Base Color.
    connected, base_color = [], []
    visited = set()

    def loaded_image(node):
        image = getattr(node, 'image', None)
        return image if image is not None and min(image_size(image)) > 0 else None

    # socket.links searches every link of the tree each time it is read, and this walk reads it for every input
    # on the way (about a thousand reads for a blended atlas material, on every picker redraw). One pass per node
    # tree gives the same links, in the same order.
    link_tables = {}

    def incoming(socket):
        tree = socket.id_data
        table = link_tables.get(tree.as_pointer())
        if table is None:
            table = link_tables[tree.as_pointer()] = defaultdict(list)
            for link in tree.links:
                table[link.to_socket.as_pointer()].append(link)
        return table.get(socket.as_pointer(), ())

    def walk_input(socket, groups=(), prefer_base=False):
        for link in incoming(socket):
            if link.is_valid:
                walk_output(link.from_socket, groups, prefer_base)

    def walk_output(socket, groups, prefer_base):
        key = (socket.as_pointer(), tuple(group.as_pointer() for group in groups), prefer_base)
        if key in visited:
            return
        visited.add(key)
        node = socket.node
        if node.type == 'TEX_IMAGE':
            if loaded_image(node) is not None:
                if node not in connected:
                    connected.append(node)
                if prefer_base and node not in base_color:
                    base_color.append(node)
            return
        if node.type == 'GROUP' and node.node_tree is not None:
            index = list(node.outputs).index(socket)
            outputs = [n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT']
            active = [n for n in outputs if getattr(n, 'is_active_output', False)]
            for output in active or outputs[:1]:
                if index < len(output.inputs):
                    walk_input(output.inputs[index], groups + (node,), prefer_base)
            return
        if node.type == 'GROUP_INPUT':
            if groups:
                group = groups[-1]
                index = list(node.outputs).index(socket)
                if index < len(group.inputs):
                    walk_input(group.inputs[index], groups[:-1], prefer_base)
            return
        if node.type == 'BSDF_PRINCIPLED':
            color = node.inputs.get('Base Color')
            if color is not None:
                walk_input(color, groups, True)
        for input_socket in node.inputs:
            walk_input(input_socket, groups, prefer_base)

    outputs = [node for node in mat.node_tree.nodes if node.type == 'OUTPUT_MATERIAL']
    active_outputs = [node for node in outputs if getattr(node, 'is_active_output', False)]
    for output in active_outputs or outputs[:1]:
        for socket in output.inputs:
            walk_input(socket)

    candidates = base_color or connected
    if candidates:
        # A selected overlay node is a second image on the same material.
        # Tile picking and ordinary paint still address the base atlas.
        if mat.get('sprotile_shared_blend') or mat.get('sprotile_blend_material'):
            for node in candidates:
                if node.get('sprotile_shared_role') == 'original' or node.get('sprotile_blend_role') == 'texture_a':
                    return node
        active = mat.node_tree.nodes.active
        chosen = active if active in candidates else candidates[0]
        return chosen

    # Keep supporting simple atlas materials assembled before their shader is
    # wired up. This fallback is used only when the output has no image at all.
    active = mat.node_tree.nodes.active
    if active is not None and active.type == 'TEX_IMAGE' and loaded_image(active) is not None:
        return active
    return next((node for node in mat.node_tree.nodes
                 if node.type == 'TEX_IMAGE' and loaded_image(node) is not None), None)


def image_from_material(mat):
    node = atlas_node_from_material(mat)
    return node.image if node is not None else None


def find_atlas_image(obj, face=None):
    """Atlas for `obj`, preferring the material of `face` when one is given."""
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None

    if face is not None:
        try:
            slots = obj.material_slots
            if 0 <= face.material_index < len(slots):
                return image_from_material(slots[face.material_index].material)
        except Exception:
            pass
        return None

    materials = []
    if obj.active_material is not None and obj.active_material not in materials:
        materials.append(obj.active_material)
    for mat in obj.data.materials:
        if mat is not None and mat not in materials:
            materials.append(mat)

    for mat in materials:
        image = image_from_material(mat)
        if image is not None:
            return image
    return None


def atlas_for_material_index(obj, material_index):
    """Atlas of this exact slot; another material's atlas is never a substitute."""
    try:
        slots = obj.material_slots
        if 0 <= material_index < len(slots):
            return image_from_material(slots[material_index].material)
    except Exception:
        pass
    return None


def resolve_atlas_image(obj):
    """Atlas the picker should show: the active face's material while editing.

    A mesh that tiles from two atlases used to show whichever image node was
    found first, so the picker could display one atlas while you painted from
    another.
    """
    face = None
    if is_editable_mesh(obj):
        try:
            bm = bmesh.from_edit_mesh(obj.data)
            face = bm.faces.active
            if face is not None and not face.is_valid:
                face = None
        except Exception:
            face = None
    return find_atlas_image(obj, face)


def is_editable_mesh(obj):
    """True only when bmesh.from_edit_mesh() is actually safe to call.

    `obj.mode == 'EDIT'` can be true while the mesh itself has no edit-mode
    data - during a mode transition, or for a linked library mesh - and
    from_edit_mesh() raises in that window.
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return False
    if obj.mode != 'EDIT':
        return False
    if not getattr(obj.data, 'is_editmode', True):
        return False
    # Every Sprotile tool and panel asks this before it reads the edit mesh, so this is where the BMesh is held.
    hold_edit_bmesh(obj.data)
    return True


def edit_mesh_objects(context):
    """Every mesh currently in Edit Mode, not just the active one."""
    objs = getattr(context, 'objects_in_mode', None)
    if objs:
        return [o for o in objs if is_editable_mesh(o)]
    obj = context.active_object
    return [obj] if is_editable_mesh(obj) else []


def mapping_edit_mesh_objects(context):
    """Reject conflicting atlas overrides before deduplicating linked meshes."""
    objects = edit_mesh_objects(context)
    by_mesh = defaultdict(list)
    for obj in objects:
        by_mesh[obj.data.as_pointer()].append(obj)
    # Blender may expose only one representative in objects_in_mode even
    # though two selected objects share its Edit Mode mesh data.
    for obj in getattr(context, 'selected_objects', ()):
        if obj.type == 'MESH' and obj.data.as_pointer() in by_mesh:
            linked = by_mesh[obj.data.as_pointer()]
            if obj not in linked:
                linked.append(obj)
    for linked in by_mesh.values():
        if len(linked) < 2:
            continue
        bm = bmesh.from_edit_mesh(linked[0].data)
        indices = sorted({face.material_index for face in bm.faces if face.select and not face.hide})
        signatures = []
        for obj in linked:
            for face in bm.faces:
                if face.select and not face.hide:
                    plain_paint_binding(obj,bm,face)
            images = [atlas_for_material_index(obj, index) for index in indices]
            signatures.append(tuple(image.as_pointer() if image is not None else None for image in images))
        if any(signature != signatures[0] for signature in signatures[1:]):
            raise ValueError("Selected linked objects use different atlases; match materials or make their meshes single-user")
    return objects


def get_uv_inset(scene):
    return scene.sprotile_uv_inset_px if scene.sprotile_use_uv_inset else 0.0


def validate_tile_dimensions(tile_w_px, tile_h_px, atlas_width, atlas_height):
    """Reject incompatible grids before any UV layer or coordinates are changed."""
    if min(tile_w_px, tile_h_px, atlas_width, atlas_height) <= 0:
        raise ValueError("Atlas and tile dimensions must be positive")
    if tile_w_px > atlas_width or tile_h_px > atlas_height:
        raise ValueError(f"Tile is {tile_w_px}x{tile_h_px}px but atlas is {atlas_width}x{atlas_height}px; reduce Tile Size")


def tile_uv_bounds(col, row, tile_w_px, tile_h_px,
                   atlas_width, atlas_height, inset_px=0.0):
    """Inset origin and span, shared by mapping and pipette reconstruction."""
    validate_tile_dimensions(tile_w_px, tile_h_px, atlas_width, atlas_height)
    # Keep equal pixel margins on all four sides, even for rectangular tiles.
    # Stop just short of half the smaller dimension so oversized inputs cannot
    # collapse or invert the UVs.  The tile centre stays fixed when clamped.
    inset_px = min(max(0.0, inset_px), min(tile_w_px, tile_h_px) * 0.499)
    tile_u = tile_w_px / atlas_width
    tile_v = tile_h_px / atlas_height
    inset_u = inset_px / atlas_width
    inset_v = inset_px / atlas_height
    rows_total = atlas_height / tile_h_px
    return (
        col * tile_u + inset_u,
        (rows_total - 1 - row) * tile_v + inset_v,
        tile_u - 2.0 * inset_u,
        tile_v - 2.0 * inset_v,
    )


# ---------------------------------------------------------------------------
# Recent tiles: session history and one shared layout for drawing/hit testing
# ---------------------------------------------------------------------------

RecentTile = namedtuple("RecentTile", "col row size mode")


class RecentTilesLayout:
    def __init__(self, bounds, cells):
        self.bounds = bounds
        self.cells = cells  # (RecentTile, (left, bottom, right, top)) pairs

    @staticmethod
    def inside(rect, x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def contains(self, x, y):
        return self.inside(self.bounds, x, y)

    def tile_at(self, x, y):
        return next((tile for tile, rect in self.cells if self.inside(rect, x, y)), None)


class RecentTiles:
    """At most 32 unique tiles per scene/atlas, newest first; no mesh state."""

    def __init__(self):
        self._history = {}

    def clear(self):
        self._history.clear()

    def tiles(self, scene, image):
        width, height = image_size(image)
        if width <= 0 or height <= 0:
            return ()
        # Session IDs survive renaming and undo and cannot alias a deleted ID.
        key = (scene.session_uid, image.session_uid)
        tiles = self._history.get(key, ())
        # An image may have been resized/reloaded since these tiles were used.
        return tuple(tile for tile in tiles
                     if (tile.col + 1) * tile_dimensions(tile.size, tile.mode)[0] <= width
                     and (tile.row + 1) * tile_dimensions(tile.size, tile.mode)[1] <= height)

    def remember(self, scene, image, col, row):
        width, height = image_size(image)
        tile_w, tile_h = get_tile_dims(scene)
        if width < tile_w or height < tile_h:
            return
        # Mirror the mapping clamp, including after changing Tile Size.
        col = max(0, min(width // tile_w - 1, col))
        row = max(0, min(height // tile_h - 1, row))
        tile = RecentTile(col, row, scene.sprotile_tile_size, scene.sprotile_tile_mode)
        key = (scene.session_uid, image.session_uid)
        previous = self.tiles(scene, image)
        self._history[key] = [tile] + [item for item in previous if item != tile][:RECENT_TILE_LIMIT - 1]

    def select(self, scene, image, tile):
        if tile not in self.tiles(scene, image):
            return
        scene.sprotile_tile_size = tile.size
        scene.sprotile_tile_mode = tile.mode
        scene.sprotile_active_col = tile.col
        scene.sprotile_active_row = tile.row
        self.remember(scene, image, tile.col, tile.row)

    def layout(self, scene, region, area, image):
        if not scene.sprotile_show_recent_tiles or image_size(image)[0] <= 0:
            return None
        # The N-panel can overlap the WINDOW region. Anchor to its visible edge.
        right = viewport_visible_width(region, area)
        margin, padding, gap, header = 12, 8, 4, 24
        cell = min(40, (right - 2 * margin - 2 * padding - 7 * gap) / RECENT_TILE_COLUMNS,
                   (region.height - 2 * margin - 2 * padding - header - 3 * gap) / 4)
        if cell < 12:
            return None
        tiles = self.tiles(scene, image)
        rows = max(1, (len(tiles) + RECENT_TILE_COLUMNS - 1) // RECENT_TILE_COLUMNS)
        width = 2 * padding + RECENT_TILE_COLUMNS * cell + 7 * gap
        height = 2 * padding + header + rows * cell + (rows - 1) * gap
        left, bottom = right - margin - width, margin
        cells = []
        for index, tile in enumerate(tiles):
            x = left + padding + (index % RECENT_TILE_COLUMNS) * (cell + gap)
            y = bottom + padding + (rows - 1 - index // RECENT_TILE_COLUMNS) * (cell + gap)
            cells.append((tile, (x, y, x + cell, y + cell)))
        return RecentTilesLayout((left, bottom, right - margin, bottom + height), cells)

    def draw(self, scene, region, area, image, mouse_pos):
        layout = self.layout(scene, region, area, image)
        if layout is None:
            return
        left, bottom, right, top = layout.bounds
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        shader.uniform_float("color", (0.055, 0.055, 0.055, 0.98))
        draw_rect_fill(shader, left, bottom, right, top)
        blf_size(0, 11)
        blf.color(0, 0.9, 0.9, 0.9, 1.0)
        blf.position(0, left + 8, top - 20, 0)
        blf.draw(0, f"Recent Tiles  {len(layout.cells)}/{RECENT_TILE_LIMIT}")
        if not layout.cells:
            blf.color(0, 0.6, 0.6, 0.6, 1.0)
            blf.position(0, left + 8, bottom + 20, 0)
            blf.draw(0, "Select a tile to start")
            return
        texture = gpu.texture.from_image(image)
        image_shader = get_image_shader()
        width, height = image_size(image)
        active = RecentTile(scene.sprotile_active_col, scene.sprotile_active_row,
                            scene.sprotile_tile_size, scene.sprotile_tile_mode)
        hovered = layout.tile_at(*mouse_pos)
        for tile, rect in layout.cells:
            x0, y0, x1, y1 = rect
            shader.bind()
            shader.uniform_float("color", (0.13, 0.13, 0.13, 1.0))
            draw_rect_fill(shader, *rect)
            tile_w, tile_h = tile_dimensions(tile.size, tile.mode)
            u, v, span_u, span_v = tile_uv_bounds(tile.col, tile.row, tile_w, tile_h, width, height)
            scale = (x1 - x0 - 4) / max(tile_w, tile_h)
            w, h = tile_w * scale, tile_h * scale
            x, y = (x0 + x1 - w) / 2, (y0 + y1 - h) / 2
            image_shader.bind()
            image_shader.uniform_sampler("image", texture)
            draw_image_quad(image_shader, x, y, x + w, y + h, (u, v, u + span_u, v + span_v))
            shader.bind()
            color = ((1.0, 0.85, 0.15, 1.0) if tile == hovered else
                     (0.0, 0.8, 1.0, 1.0) if tile == active else (0.3, 0.3, 0.3, 1.0))
            shader.uniform_float("color", color)
            draw_rect_outline(shader, *rect)


_recent_tiles = RecentTiles()


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import (
    PANEL_MIN_WIDTH, PANEL_TOP_MARGIN, RECENT_TILE_COLUMNS, RECENT_TILE_LIMIT, ROTATION_ORDER,
    hold_edit_bmesh, material_nodes,
)
from .draw import blf_size, draw_image_quad, draw_rect_fill, draw_rect_outline, get_image_shader
from .mapping import plain_paint_binding
from .picker import get_window_region
