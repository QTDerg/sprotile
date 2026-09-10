bl_info = {
    "name": "Sprotile",
    "version": (5, 1),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Sprotile | View3D > Toolbar (Edit Mode)",
    "description": "Paint texture atlas tiles onto faces with a continuous visual picker",
    "category": "UV",
}

import bpy
import bmesh
import gpu
import blf
import math
import time
import traceback
import mathutils
from collections import deque, defaultdict
from mathutils.bvhtree import BVHTree
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent
from bpy.props import IntProperty, FloatProperty, EnumProperty, BoolProperty

# ---------------------------------------------------------------------------
# Global state
#
# The draw handles and the live modal operators are mirrored into module level
# globals so they can always be shut down, even if the operator instance that
# created them is gone.  Blender kills modal operators without warning on file
# load, area close, script reload, etc.  A leaked draw handler is what makes
# the overlay "stick" on screen forever, and it hard-crashes Blender once the
# addon is unregistered underneath it.
# ---------------------------------------------------------------------------

_preview_operator = None
_preview_draw_handle = None
_brush_draw_handles = []
_active_brush_operators = []
addon_keymaps = []
_tool_registered = False

PANEL_TOP_MARGIN = 35
DOUBLE_CLICK_SECONDS = 0.3
ROTATION_ORDER = ('0', '90', '180', '270')

# Runaway guard for the brush flood fill.  The real bound is "faces touching
# the brush circle"; this only exists so a pathological mesh cannot lock the UI.
BRUSH_MAX_FACES = 20000


# ---------------------------------------------------------------------------
# Version compatibility shims (Blender 3.6 -> 5.x)
# ---------------------------------------------------------------------------

_blf_needs_dpi = None


def blf_size(font_id, size):
    """blf.size() lost its `dpi` argument in 4.0; 3.6 still expects it."""
    global _blf_needs_dpi
    if _blf_needs_dpi is None:
        try:
            blf.size(font_id, size)
            _blf_needs_dpi = False
            return
        except TypeError:
            _blf_needs_dpi = True
    if _blf_needs_dpi:
        blf.size(font_id, size, 72)
    else:
        blf.size(font_id, size)


_image_shader = None


def get_image_shader():
    """Cached nearest-pixel shader for the atlas preview (Blender 3.6+)."""
    global _image_shader
    if _image_shader is None:
        interface = gpu.types.GPUStageInterfaceInfo("sprotile_image_interface")
        interface.smooth('VEC2', "uv")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "ModelViewProjectionMatrix")
        # Blender updates this built-in uniform for the current framebuffer.
        # An sRGB framebuffer encodes linear output itself; converting in the
        # shader as well would apply gamma twice and wash out the preview.
        info.push_constant('BOOL', "srgbTarget")
        info.vertex_in(0, 'VEC2', "pos")
        info.vertex_in(1, 'VEC2', "texCoord")
        info.vertex_out(interface)
        info.sampler(0, 'FLOAT_2D', "image")
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source('''
            void main()
            {
                uv = texCoord;
                gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
            }
        ''')
        # texelFetch reads one pixel at mip level 0, bypassing interpolation
        # without changing the sampler of the image shared with Blender.
        # GPUTexture filtering controls are unavailable in older versions.
        info.fragment_source('''
            float to_srgb(float value)
            {
                return value < 0.0031308
                    ? 12.92 * max(value, 0.0)
                    : 1.055 * pow(value, 1.0 / 2.4) - 0.055;
            }

            void main()
            {
                ivec2 size = textureSize(image, 0);
                ivec2 pixel = clamp(ivec2(floor(uv * vec2(size))),
                                    ivec2(0), size - ivec2(1));
                vec4 color = texelFetch(image, pixel, 0);
                fragColor = srgbTarget ? color
                    : vec4(to_srgb(color.r), to_srgb(color.g),
                           to_srgb(color.b), color.a);
            }
        ''')
        _image_shader = gpu.shader.create_from_info(info)
    return _image_shader


def invert_matrix(matrix):
    """Matrix.inverted(), tolerating a degenerate (zero-scale) object."""
    try:
        return matrix.inverted()
    except ValueError:
        return matrix.inverted_safe()


# ---------------------------------------------------------------------------
# Primitive helpers
#
# TRI_FAN and LINE_LOOP are legacy primitives: they still exist in the Python
# API but are unsupported on the Metal and Vulkan backends, so they render as
# garbage (or nothing) outside OpenGL.  Every fill/outline goes through these
# helpers instead, which draw identically on 3.6 and on 4.x / 5.x, on any
# backend.
# ---------------------------------------------------------------------------

def draw_rect_fill(shader, x0, y0, x1, y1):
    batch = batch_for_shader(
        shader, 'TRIS',
        {"pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1))},
        indices=((0, 1, 2), (0, 2, 3)),
    )
    batch.draw(shader)


def draw_rect_outline(shader, x0, y0, x1, y1):
    batch = batch_for_shader(shader, 'LINES', {"pos": (
        (x0, y0), (x1, y0),
        (x1, y0), (x1, y1),
        (x1, y1), (x0, y1),
        (x0, y1), (x0, y0),
    )})
    batch.draw(shader)


def draw_line(shader, x0, y0, x1, y1):
    batch = batch_for_shader(shader, 'LINES', {"pos": ((x0, y0), (x1, y1))})
    batch.draw(shader)


def draw_image_quad(tex_shader, x0, y0, x1, y1):
    tex_shader.uniform_float(
        "ModelViewProjectionMatrix",
        gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix(),
    )
    batch = batch_for_shader(
        tex_shader, 'TRIS',
        {
            "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
            "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
        },
        indices=((0, 1, 2), (0, 2, 3)),
    )
    batch.draw(tex_shader)


def circle_points(cx, cy, radius, segments=32):
    return [
        (cx + math.cos((i / segments) * math.tau) * radius,
         cy + math.sin((i / segments) * math.tau) * radius)
        for i in range(segments + 1)
    ]


def draw_disc(shader, cx, cy, radius, segments=32):
    ring = circle_points(cx, cy, radius, segments)
    pos = [(cx, cy)] + ring
    indices = [(0, i, i + 1) for i in range(1, len(ring))]
    if not indices:
        return
    batch = batch_for_shader(shader, 'TRIS', {"pos": pos}, indices=indices)
    batch.draw(shader)


def tag_redraw_view3d():
    try:
        wm = bpy.context.window_manager
        if wm is None:
            return
        for window in wm.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


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


def detect_tile_transform(local_uvs):
    """Best-matching (rotation, flip_u, flip_v) for a face's local tile UVs.

    Every candidate is generated with the exact same code that paints them, so
    a tile painted by Sprotile reads back as the transform it was painted with.
    Ties resolve toward 0 degrees / no flips because that is the first
    candidate tried and later ones must beat it outright.
    """
    n = len(local_uvs)
    best = (0, False, False)
    best_err = None
    for flip_v in (False, True):
        for flip_u in (False, True):
            for rotation in (0, 90, 180, 270):
                err = 0.0
                for i, (au, av) in enumerate(local_uvs):
                    bu, bv = base_local_uv(i, n)
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
    s = max(1, scene.sprotile_tile_size)
    mode = scene.sprotile_tile_mode
    if mode == 'HALF_WIDE':
        return s, max(1, s // 2)
    if mode == 'HALF_TALL':
        return max(1, s // 2), s
    return s, s


def preview_panel_rect(scene, region):
    """Clamped picker rectangle in region pixels: (left, width, top).

    One definition, used by the overlay, the hit test and the brush dead zone -
    if these ever drift apart the panel and the area it blocks stop matching.
    """
    left = max(0, min(int(scene.sprotile_preview_left), max(0, region.width - 60)))
    width = max(60, min(int(scene.sprotile_preview_width), max(60, region.width - left)))
    top = max(1, region.height - PANEL_TOP_MARGIN)
    return left, width, top


def image_size(image):
    """Safe size read; a freed or unloaded image raises instead of crashing."""
    try:
        return int(image.size[0]), int(image.size[1])
    except (ReferenceError, AttributeError, IndexError):
        return 0, 0


def image_from_material(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            w, h = image_size(node.image)
            if w > 0 and h > 0:
                return node.image
    return None


def find_atlas_image(obj, face=None):
    """Atlas for `obj`, preferring the material of `face` when one is given."""
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None

    materials = []
    if face is not None:
        try:
            slots = obj.material_slots
            if 0 <= face.material_index < len(slots):
                mat = slots[face.material_index].material
                if mat is not None:
                    materials.append(mat)
        except Exception:
            pass

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
    """Atlas belonging to one material slot, falling back to the object's."""
    try:
        slots = obj.material_slots
        if 0 <= material_index < len(slots):
            image = image_from_material(slots[material_index].material)
            if image is not None:
                return image
    except Exception:
        pass
    return find_atlas_image(obj)


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
    return bool(getattr(obj.data, 'is_editmode', True))


def edit_mesh_objects(context):
    """Every mesh currently in Edit Mode, not just the active one."""
    objs = getattr(context, 'objects_in_mode', None)
    if objs:
        return [o for o in objs if is_editable_mesh(o)]
    obj = context.active_object
    return [obj] if is_editable_mesh(obj) else []


def get_uv_inset(scene):
    return scene.sprotile_uv_inset_px if scene.sprotile_use_uv_inset else 0.0


def tile_uv_bounds(col, row, tile_w_px, tile_h_px,
                   atlas_width, atlas_height, inset_px=0.0):
    """Inset origin and span, shared by mapping and pipette reconstruction."""
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


def map_face_to_tile(face, uv_layer, col, row, tile_w_px, tile_h_px,
                     atlas_width, atlas_height,
                     rotation=0, flip_u=False, flip_v=False, inset_px=0.0):
    """Map a face's loops to the calculated UV coordinates of the target tile."""
    if atlas_width <= 0 or atlas_height <= 0 or tile_w_px <= 0 or tile_h_px <= 0:
        return

    # The active tile is stored independently of the grid, so raising Tile Size
    # can leave it pointing past the last column or row.  Clamp here - the one
    # place every mapping path goes through - so UVs can never land outside the
    # atlas.
    col = max(0, min(max(1, int(atlas_width / tile_w_px)) - 1, col))
    row = max(0, min(max(1, int(atlas_height / tile_h_px)) - 1, row))

    u_start, v_start, span_u, span_v = tile_uv_bounds(
        col, row, tile_w_px, tile_h_px, atlas_width, atlas_height, inset_px,
    )

    num_verts = len(face.loops)
    if num_verts == 0:
        return

    for i, loop in enumerate(face.loops):
        u_local, v_local = base_local_uv(i, num_verts)
        u_local, v_local = apply_tile_transform(u_local, v_local, rotation, flip_u, flip_v)
        loop[uv_layer].uv = (
            u_start + u_local * span_u,
            v_start + v_local * span_v,
        )


def map_selected_faces_to_active(context):
    """Map the selected faces of every mesh in Edit Mode to the active tile.

    Faces are grouped by material slot so a selection spanning two atlases is
    mapped against the correct one for each face.
    """
    scene = context.scene
    col = scene.sprotile_active_col
    row = scene.sprotile_active_row
    tile_w_px, tile_h_px = get_tile_dims(scene)
    rotation, flip_u, flip_v = get_tile_transform(scene)
    inset_px = get_uv_inset(scene)

    mapped_any = False

    for obj in edit_mesh_objects(context):
        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        uv_layer = bm.loops.layers.uv.verify()

        selected = [f for f in bm.faces if f.select and not f.hide]
        if not selected:
            continue

        by_material = defaultdict(list)
        for face in selected:
            by_material[face.material_index].append(face)

        mapped_here = False
        for material_index, faces in by_material.items():
            image = atlas_for_material_index(obj, material_index)
            if image is None:
                continue
            atlas_width, atlas_height = image_size(image)
            if atlas_width <= 0 or atlas_height <= 0:
                continue
            for face in faces:
                map_face_to_tile(
                    face, uv_layer, col, row,
                    tile_w_px, tile_h_px,
                    atlas_width, atlas_height,
                    rotation, flip_u, flip_v,
                    inset_px=inset_px,
                )
            mapped_here = True

        if mapped_here:
            # UVs never change topology.  Skipping the destructive rebuild keeps
            # live BMesh references (held by the brush operator) valid and is
            # far faster.
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
            mapped_any = True

    if mapped_any:
        tag_redraw_view3d()
    return mapped_any


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
# Sprotile Map Selected Operator (Bound to Keymap)
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_map_selected(bpy.types.Operator):
    """Map currently selected faces in the 3D Viewport to the active texture tile"""
    bl_idname = "mesh.sprotile_map_selected"
    bl_label = "Sprotile Map Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        if not map_selected_faces_to_active(context):
            self.report({'WARNING'}, "Ensure a mesh is in Edit Mode, faces are selected, and material has a texture.")
            return {'CANCELLED'}
        self.report({'INFO'}, "Mapped selected faces to active tile")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Tile transform operators (keymap-able; nothing is bound by default so that
# R, X and Y keep their normal meaning in the viewport)
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_rotate(bpy.types.Operator):
    """Rotate the tile that gets painted, in 90 degree steps"""
    bl_idname = "mesh.sprotile_rotate"
    bl_label = "Sprotile Rotate Tile"
    bl_options = {'REGISTER'}

    steps: IntProperty(
        name="Steps",
        description="Number of 90 degree steps; negative rotates the other way",
        default=1,
    )

    def execute(self, context):
        cycle_rotation(context.scene, self.steps)
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_flip(bpy.types.Operator):
    """Mirror the tile that gets painted"""
    bl_idname = "mesh.sprotile_flip"
    bl_label = "Sprotile Flip Tile"
    bl_options = {'REGISTER'}

    axis: EnumProperty(
        name="Axis",
        items=[('U', "U", "Mirror horizontally"), ('V', "V", "Mirror vertically")],
        default='U',
    )

    def execute(self, context):
        scene = context.scene
        if self.axis == 'U':
            scene.sprotile_flip_u = not scene.sprotile_flip_u
        else:
            scene.sprotile_flip_v = not scene.sprotile_flip_v
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_reset_transform(bpy.types.Operator):
    """Clear rotation and both flips"""
    bl_idname = "mesh.sprotile_reset_transform"
    bl_label = "Sprotile Reset Tile Orientation"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        scene.sprotile_rotation = '0'
        scene.sprotile_flip_u = False
        scene.sprotile_flip_v = False
        tag_redraw_view3d()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Persistent Preview Background Helper Operator
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_preview_helper(bpy.types.Operator):
    """Manages the persistent texture atlas sidebar and its zoom/pan/click interactions"""
    bl_idname = "mesh.sprotile_preview_helper"
    bl_label = "Sprotile Preview Helper"
    bl_options = {'INTERNAL'}

    # -- lifecycle ----------------------------------------------------------

    def invoke(self, context, event):
        global _preview_operator, _preview_draw_handle

        if _preview_operator is not None:
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Start the Sprotile preview from a 3D Viewport")
            return {'CANCELLED'}

        self.area = context.area
        self.stop_requested = False
        self.is_panning = False
        self.pan_start_x = 0
        self.pan_start_y = 0
        self.hover_col = -1
        self.hover_row = -1
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

        self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, (), 'WINDOW', 'POST_PIXEL'
        )
        _preview_draw_handle = self.draw_handle
        _preview_operator = self

        context.window_manager.modal_handler_add(self)
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def finish(self, context=None):
        global _preview_operator, _preview_draw_handle
        if getattr(self, 'draw_handle', None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            except Exception:
                pass
            if _preview_draw_handle is self.draw_handle:
                _preview_draw_handle = None
            self.draw_handle = None
        if _preview_operator is self:
            _preview_operator = None
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
        """True if the mouse sits over a non-WINDOW region (toolbar, N-panel,
        the floating 'Adjust Last Operation' popup...).  Those overlap the
        preview rectangle visually but belong to Blender's own UI handling."""
        if self.area is None:
            return False
        try:
            regions = self.area.regions
        except Exception:
            return False

        mx, my = event.mouse_x, event.mouse_y  # window space, like region.x/y
        for region in regions:
            if region.type == 'WINDOW' or region.width <= 0 or region.height <= 0:
                continue
            if (region.x <= mx <= region.x + region.width and
                    region.y <= my <= region.y + region.height):
                return True
        return False

    def map_selected(self, context):
        """Run the mapping as a real operator so it lands on the undo stack.

        Calling the worker function directly (as the original did) changed UVs
        with no undo step behind them - Ctrl+Z could not take the mapping back.
        """
        try:
            if bpy.ops.mesh.sprotile_map_selected.poll():
                bpy.ops.mesh.sprotile_map_selected()
        except Exception:
            traceback.print_exc()

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

        self.area.tag_redraw()

        # Only act on events that belong to our own viewport.
        if context.area is None or context.area != self.area:
            if not self.is_panning:
                self.hover_col = -1
                self.hover_row = -1
                return {'PASS_THROUGH'}

        scene = context.scene

        # event.mouse_region_* is relative to whichever region produced the
        # event (sidebar, toolbar, another area...).  Derive our own region
        # coordinates from window space so the picker never jumps.
        mouse_x = event.mouse_x - region.x
        mouse_y = event.mouse_y - region.y

        self.image = resolve_atlas_image(context.active_object)
        if self.image is None:
            self.atlas_width = 0
            self.atlas_height = 0
            self.hover_col = -1
            self.hover_row = -1
            if self.is_panning and event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'} and event.value == 'RELEASE':
                self.is_panning = False
            return {'PASS_THROUGH'}

        self.atlas_width, self.atlas_height = image_size(self.image)
        if self.atlas_width <= 0 or self.atlas_height <= 0:
            return {'PASS_THROUGH'}

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
            return {'PASS_THROUGH'}

        left, width, top = preview_panel_rect(scene, region)
        in_region = 0 <= mouse_x <= region.width and 0 <= mouse_y <= region.height
        in_preview = in_region and (left <= mouse_x <= left + width) and (0 <= mouse_y <= top)

        if not in_preview:
            self.hover_col = -1
            self.hover_row = -1
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self.update_hover(context, mouse_x, mouse_y)
            return {'RUNNING_MODAL'}

        if event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.is_panning = True
            self.pan_start_x = event.mouse_x
            self.pan_start_y = event.mouse_y
            return {'RUNNING_MODAL'}

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            self.zoom_at(scene, region, mouse_x, mouse_y,
                         1.15 if event.type == 'WHEELUPMOUSE' else 0.85)
            self.update_hover(context, mouse_x, mouse_y)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self.on_click(context, event, mouse_x, mouse_y)
            # Swallow the release too, otherwise the viewport (or the Sprotile
            # brush tool) reacts to a click that was meant for the picker.
            return {'RUNNING_MODAL'}

        # Tile orientation, only while the cursor is over the picker - R, X and
        # Y keep their normal viewport meaning everywhere else.
        if event.value == 'PRESS':
            if event.type == 'R':
                cycle_rotation(scene, -1 if event.shift else 1)
                return {'RUNNING_MODAL'}
            if event.type == 'X':
                scene.sprotile_flip_u = not scene.sprotile_flip_u
                return {'RUNNING_MODAL'}
            if event.type == 'Y':
                scene.sprotile_flip_v = not scene.sprotile_flip_v
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

    def zoom_at(self, scene, region, mouse_x, mouse_y, factor):
        rect = self.atlas_rect(scene, region)
        if rect is None:
            self.zoom = max(0.1, min(100.0, self.zoom * factor))
            return

        start_x, start_y, old_w, old_h = rect
        rel_x = (mouse_x - start_x) / old_w
        rel_y = (mouse_y - start_y) / old_h

        self.zoom = max(0.1, min(100.0, self.zoom * factor))

        new_rect = self.atlas_rect(scene, region)
        if new_rect is None:
            return
        new_x, new_y, new_w, new_h = new_rect

        self.offset_x -= (new_x + rel_x * new_w) - mouse_x
        self.offset_y -= (new_y + rel_y * new_h) - mouse_y

    # -- drawing ------------------------------------------------------------

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

            self.image = resolve_atlas_image(context.active_object)
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

            # Clip to the panel.  A scissor box must stay inside the region or
            # the driver rejects it and everything drawn afterwards in the
            # viewport vanishes.
            sc_x = max(0, min(int(left), region.width))
            sc_y = 0
            sc_w = max(0, min(int(width), region.width - sc_x))
            sc_h = max(0, min(int(top), region.height))
            if sc_w > 0 and sc_h > 0:
                gpu.state.scissor_test_set(True)
                gpu.state.scissor_set(sc_x, sc_y, sc_w, sc_h)
                scissor_on = True

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
            cols, rows, rows_total, tile_w_disp, tile_h_disp = self.tile_metrics(scene, rect)

            shader.bind()

            # 3. Grid, drawn at true tile size and anchored top-left.  Any
            # remainder is left ungridded rather than stretched over.
            if cols > 0 and rows > 0 and tile_w_disp > 0.0 and tile_h_disp > 0.0:
                grid_right = start_x + cols * tile_w_disp
                grid_top = start_y + rows_total * tile_h_disp
                grid_bottom = start_y + (rows_total - rows) * tile_h_disp

                shader.uniform_float("color", (1.0, 1.0, 1.0, 0.2))
                grid_vertices = []
                for i in range(cols + 1):
                    x = start_x + i * tile_w_disp
                    grid_vertices.extend([(x, grid_bottom), (x, grid_top)])
                for i in range(rows + 1):
                    y = grid_top - i * tile_h_disp
                    grid_vertices.extend([(start_x, y), (grid_right, y)])
                batch_grid = batch_for_shader(shader, 'LINES', {"pos": grid_vertices})
                batch_grid.draw(shader)

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
                    if not transform_is_default(rotation, flip_u, flip_v):
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

            if scissor_on:
                gpu.state.scissor_test_set(False)
                scissor_on = False

            # 6. Header text
            font_id = 0
            blf_size(font_id, 11)

            title = f"Tile: {scene.sprotile_active_col}, {scene.sprotile_active_row} ({tile_w_px}x{tile_h_px})"
            if not transform_is_default(rotation, flip_u, flip_v):
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

class MESH_OT_sprotile_pipette(bpy.types.Operator):
    """Sample tile coordinates and orientation from the active or selected face"""
    bl_idname = "mesh.sprotile_pipette"
    bl_label = "Pipette Active Tile"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        obj = context.active_object
        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        uv_layer = bm.loops.layers.uv.verify()

        # Prefer the active face - that is the one you clicked last.  Otherwise
        # fall back to the original rule of exactly one selected face.
        face = bm.faces.active
        if face is None or not face.is_valid or not face.select or face.hide:
            selected_faces = [f for f in bm.faces if f.select and not f.hide]
            if len(selected_faces) != 1:
                self.report({'WARNING'}, "Select one face (or make the face you want active) to pipette from.")
                return {'CANCELLED'}
            face = selected_faces[0]

        # Sample against the atlas this face actually uses, not just the first
        # image node on the object.
        image = atlas_for_material_index(obj, face.material_index)
        if image is None:
            self.report({'ERROR'}, "No active image found inside materials.")
            return {'CANCELLED'}

        atlas_width, atlas_height = image_size(image)
        if atlas_width <= 0 or atlas_height <= 0:
            self.report({'ERROR'}, "Atlas image has no pixel data.")
            return {'CANCELLED'}

        scene = context.scene
        tile_w_px, tile_h_px = get_tile_dims(scene)

        uvs = [loop[uv_layer].uv.copy() for loop in face.loops]
        if not uvs:
            self.report({'WARNING'}, "Selected face has no valid UV coordinates.")
            return {'CANCELLED'}

        u_center = sum(uv.x for uv in uvs) / len(uvs)
        v_center = sum(uv.y for uv in uvs) / len(uvs)

        tile_u = tile_w_px / atlas_width
        tile_v = tile_h_px / atlas_height
        rows_total = atlas_height / tile_h_px
        cols_total = atlas_width / tile_w_px

        col = int(u_center / tile_u)
        row = int(rows_total - (v_center / tile_v))

        # Clamp calculations within valid grid limits
        col = max(0, min(int(cols_total) - 1, col))
        row = max(0, min(int(rows_total) - 1, row))

        # Read the orientation back out of the face's own UVs.  Every rotation
        # and flip keeps the tile centre fixed, so col/row above are unaffected.
        # Undo the same inset used for painting before comparing orientations.
        u_start, v_start, span_u, span_v = tile_uv_bounds(
            col, row, tile_w_px, tile_h_px, atlas_width, atlas_height,
            get_uv_inset(scene),
        )
        local_uvs = [((uv.x - u_start) / span_u, (uv.y - v_start) / span_v) for uv in uvs]
        rotation, flip_u, flip_v = detect_tile_transform(local_uvs)

        scene.sprotile_active_col = col
        scene.sprotile_active_row = row
        scene.sprotile_rotation = str(rotation)
        scene.sprotile_flip_u = flip_u
        scene.sprotile_flip_v = flip_v

        self.report({'INFO'}, f"Pipetted Tile: ({col}, {row}) {transform_label(rotation, flip_u, flip_v)}")
        tag_redraw_view3d()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Start / Stop Operators
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_preview_start(bpy.types.Operator):
    """Open and run the persistent Sprotile texture picker panel"""
    bl_idname = "mesh.sprotile_preview_start"
    bl_label = "Show Sprotile Preview"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        global _preview_operator
        if _preview_operator is not None:
            # Stale global from a run that died without cleanup - reset it so
            # the picker is not locked out for the rest of the session.
            if area_is_alive(getattr(_preview_operator, 'area', None)):
                self.report({'INFO'}, "The Sprotile preview is already open in another viewport")
                return {'CANCELLED'}
            force_cleanup()
        bpy.ops.mesh.sprotile_preview_helper('INVOKE_DEFAULT')
        return {'FINISHED'}


class MESH_OT_sprotile_preview_stop(bpy.types.Operator):
    """Close the persistent Sprotile texture picker panel"""
    bl_idname = "mesh.sprotile_preview_stop"
    bl_label = "Hide Sprotile Preview"

    def execute(self, context):
        global _preview_operator
        if _preview_operator is not None:
            _preview_operator.stop_requested = True
            # If the modal no longer receives events (its area went away) it
            # will never see the flag - tear it down directly.
            if not area_is_alive(getattr(_preview_operator, 'area', None)):
                force_cleanup()
        else:
            force_cleanup()
        tag_redraw_view3d()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Brush paint targets
#
# One per mesh in Edit Mode.  The BVH is built from the BMesh itself, so hits
# map straight back to editable faces - unlike scene.ray_cast, whose polygon
# index refers to the evaluated (post-modifier) mesh and silently points at the
# wrong face as soon as a Mirror/Array/Subsurf modifier is in the stack.
# ---------------------------------------------------------------------------

def hit_within_face_bounds(point, face):
    """Cheap sanity check that a BVH hit belongs to the face it reported.

    Bounding box only - a plane test would reject legitimate hits on the
    slightly non-planar quads that tiling meshes are full of.
    """
    try:
        verts = [v.co for v in face.verts]
        if not verts:
            return False
        size = 0.0
        for axis in range(3):
            size = max(size, max(c[axis] for c in verts) - min(c[axis] for c in verts))
        tol = max(1e-4, size * 1e-3)
        for axis in range(3):
            lo = min(c[axis] for c in verts) - tol
            hi = max(c[axis] for c in verts) + tol
            if not (lo <= point[axis] <= hi):
                return False
        return True
    except Exception:
        return True


class PaintTarget:
    """One editable mesh: its BMesh, BVH, matrices and per-slot atlas sizes."""

    def __init__(self, obj):
        self.obj = obj
        self.me = obj.data
        self.bm = bmesh.from_edit_mesh(self.me)
        self.bm.faces.ensure_lookup_table()
        if not self.bm.faces:
            raise ValueError("mesh has no faces")
        self.uv_layer = self.bm.loops.layers.uv.verify()
        self.bvh = BVHTree.FromBMesh(self.bm)
        self.matrix = obj.matrix_world.copy()
        self.matrix_inv = invert_matrix(self.matrix)
        self._atlas_cache = {}

    @property
    def is_valid(self):
        try:
            return self.bm.is_valid
        except Exception:
            return False

    def atlas_dims(self, material_index):
        dims = self._atlas_cache.get(material_index)
        if dims is None:
            image = atlas_for_material_index(self.obj, material_index)
            dims = image_size(image) if image is not None else (0, 0)
            self._atlas_cache[material_index] = dims
        return dims

    def has_atlas(self):
        return any(self.atlas_dims(i)[0] > 0
                   for i in range(max(1, len(self.obj.material_slots))))

    def raycast(self, origin_world, direction_world):
        """Nearest non-hidden face hit, as (face, world_distance) or None."""
        origin = self.matrix_inv @ origin_world
        direction = (self.matrix_inv.to_3x3() @ direction_world)
        if direction.length_squared == 0.0:
            return None
        direction = direction.normalized()

        # Hidden geometry is not pickable in Blender, so step past it rather
        # than repainting faces the user has deliberately hidden.
        for _ in range(8):
            location, _normal, index, _dist = self.bvh.ray_cast(origin, direction)
            if location is None or index is None:
                return None
            if index >= len(self.bm.faces):
                return None
            face = self.bm.faces[index]
            if not face.is_valid:
                return None
            if face.hide:
                origin = location + direction * 1e-4
                continue
            if not hit_within_face_bounds(location, face):
                return None
            world_hit = self.matrix @ location
            return face, (world_hit - origin_world).length
        return None


# ---------------------------------------------------------------------------
# Sprotile Paint Brush Operator
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_brush_paint(bpy.types.Operator):
    """Paint selected locked-in texture tile onto faces with a viewport brush"""
    bl_idname = "mesh.sprotile_brush_paint"
    bl_label = "Sprotile Brush Paint"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (is_editable_mesh(context.active_object)
                and context.area is not None and context.area.type == 'VIEW_3D')

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        objects = edit_mesh_objects(context)
        if not objects:
            self.report({'ERROR'}, "Active object must be a Mesh in Edit Mode")
            return {'CANCELLED'}

        self.area = context.area
        if is_quad_view(self.area):
            self.report({'ERROR'}, "Sprotile Paint does not support Quad View")
            return {'CANCELLED'}
        if get_region_3d(self.area) is None:
            return {'CANCELLED'}

        self.stop_requested = False
        self.draw_handle = None
        self.painted = False
        self.targets = []

        for obj in objects:
            try:
                target = PaintTarget(obj)
            except ValueError:
                continue  # empty mesh, nothing to paint on
            except Exception as exc:
                print(f"Sprotile: skipping {obj.name}: {exc}")
                continue
            if target.has_atlas():
                self.targets.append(target)

        if not self.targets:
            self.report({'ERROR'}, "No active image found inside materials")
            return {'CANCELLED'}

        region = get_window_region(self.area)
        if region is None:
            return {'CANCELLED'}

        self.mouse_pos = (event.mouse_x - region.x, event.mouse_y - region.y)

        self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, (), 'WINDOW', 'POST_PIXEL'
        )
        _brush_draw_handles.append(self.draw_handle)
        _active_brush_operators.append(self)

        self.paint_at_mouse(context)

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            traceback.print_exc()
            self.cleanup(context)
            return {'FINISHED'}

    def _modal(self, context, event):
        # force_cleanup() sets this when the addon is being torn down under us.
        if self.stop_requested:
            self.cleanup(context)
            return {'FINISHED'}

        # Anything that invalidates the live BMesh (leaving Edit Mode, an undo
        # step, switching object, closing the viewport) must stop the stroke -
        # using the stale BMesh afterwards crashes Blender outright.
        if not self.state_is_valid(context):
            self.cleanup(context)
            return {'FINISHED'}

        self.area.tag_redraw()

        if event.type == 'MOUSEMOVE':
            region = get_window_region(self.area)
            if region is not None:
                self.mouse_pos = (event.mouse_x - region.x, event.mouse_y - region.y)
                self.paint_at_mouse(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.cleanup(context)
            return {'FINISHED'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.cleanup(context)
            return {'FINISHED'}

        if event.type == 'WINDOW_DEACTIVATE':
            self.cleanup(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def state_is_valid(self, context):
        if context.mode != 'EDIT_MESH':
            return False
        if not area_is_alive(self.area):
            return False
        if not self.targets:
            return False
        for target in self.targets:
            if not target.is_valid:
                return False
        return True

    def cleanup(self, context=None):
        if getattr(self, 'draw_handle', None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            except Exception:
                pass
            if self.draw_handle in _brush_draw_handles:
                _brush_draw_handles.remove(self.draw_handle)
            self.draw_handle = None

        if self in _active_brush_operators:
            _active_brush_operators.remove(self)

        if self.painted:
            for target in self.targets:
                try:
                    if target.is_valid:
                        bmesh.update_edit_mesh(target.me, loop_triangles=False, destructive=False)
                except Exception:
                    pass
        self.targets = []
        tag_redraw_view3d()

    def cancel(self, context):
        self.cleanup(context)

    def blocked_by_preview(self, scene, region, mouse_x, mouse_y):
        """Don't paint through the picker panel - but only while it is open.

        The old build applied this cutoff unconditionally, which made the whole
        left edge of the viewport unpaintable even with the panel closed.
        """
        if _preview_operator is None:
            return False
        if getattr(_preview_operator, 'area', None) != self.area:
            return False
        left, width, top = preview_panel_rect(scene, region)
        return left <= mouse_x <= left + width and 0 <= mouse_y <= top

    def paint_at_mouse(self, context):
        from bpy_extras.view3d_utils import (
            region_2d_to_vector_3d, region_2d_to_origin_3d, location_3d_to_region_2d,
        )

        scene = context.scene
        region = get_window_region(self.area)
        rv3d = get_region_3d(self.area)
        if region is None or rv3d is None:
            return

        mouse_x, mouse_y = self.mouse_pos
        if not (0 <= mouse_x <= region.width and 0 <= mouse_y <= region.height):
            return
        if self.blocked_by_preview(scene, region, mouse_x, mouse_y):
            return

        mouse_coord = (mouse_x, mouse_y)
        mouse_vec = mathutils.Vector(mouse_coord)

        # 1. Project screen space ray
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_direction = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        if ray_origin is None or ray_direction is None:
            return

        # 2. Nearest hit across every mesh being edited
        best = None
        for target in self.targets:
            if not target.is_valid:
                continue
            hit = target.raycast(ray_origin, ray_direction)
            if hit is None:
                continue
            face, distance = hit
            if best is None or distance < best[2]:
                best = (target, face, distance)

        if best is None:
            return
        target, hit_face, _distance = best

        col = scene.sprotile_active_col
        row = scene.sprotile_active_row
        tile_w_px, tile_h_px = get_tile_dims(scene)
        rotation, flip_u, flip_v = get_tile_transform(scene)
        inset_px = get_uv_inset(scene)

        radius_pixels = scene.sprotile_brush_radius
        matrix_world = target.matrix

        def is_face_in_brush(face):
            """Brush overlaps the face centre, any vertex, or any edge midpoint."""
            p2d = location_3d_to_region_2d(region, rv3d, matrix_world @ face.calc_center_median())
            if p2d is not None and (p2d - mouse_vec).length <= radius_pixels:
                return True
            for v in face.verts:
                p2d = location_3d_to_region_2d(region, rv3d, matrix_world @ v.co)
                if p2d is not None and (p2d - mouse_vec).length <= radius_pixels:
                    return True
            for e in face.edges:
                mid = (e.verts[0].co + e.verts[1].co) * 0.5
                p2d = location_3d_to_region_2d(region, rv3d, matrix_world @ mid)
                if p2d is not None and (p2d - mouse_vec).length <= radius_pixels:
                    return True
            return False

        # BFS across edge-connected, visible faces that fall inside the brush
        queue = deque((hit_face,))
        visited = {hit_face}
        faces_to_paint = [hit_face]  # the face under the cursor always paints

        iterations = 0
        while queue and iterations < BRUSH_MAX_FACES:
            iterations += 1
            face = queue.popleft()
            for edge in face.edges:
                for nf in edge.link_faces:
                    if nf in visited:
                        continue
                    visited.add(nf)
                    if nf.hide:
                        continue
                    if is_face_in_brush(nf):
                        faces_to_paint.append(nf)
                        queue.append(nf)

        painted_here = False
        for f in faces_to_paint:
            atlas_width, atlas_height = target.atlas_dims(f.material_index)
            if atlas_width <= 0 or atlas_height <= 0:
                continue
            map_face_to_tile(
                f, target.uv_layer, col, row,
                tile_w_px, tile_h_px,
                atlas_width, atlas_height,
                rotation, flip_u, flip_v,
                inset_px=inset_px,
            )
            painted_here = True

        if painted_here:
            self.painted = True
            bmesh.update_edit_mesh(target.me, loop_triangles=False, destructive=False)

    def draw_callback(self):
        blend_on = False
        try:
            context = bpy.context
            if context.area is None or context.area != self.area:
                return
            region = context.region
            if region is None or region.type != 'WINDOW':
                return

            radius = context.scene.sprotile_brush_radius
            x, y = self.mouse_pos

            gpu.state.blend_set('ALPHA')
            blend_on = True

            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.bind()

            # Inner brush fill
            shader.uniform_float("color", (1.0, 0.9, 0.0, 0.04))
            draw_disc(shader, x, y, radius)

            # Brush outline
            shader.uniform_float("color", (1.0, 0.85, 0.0, 0.7))
            batch_line = batch_for_shader(shader, 'LINE_STRIP', {"pos": circle_points(x, y, radius)})
            batch_line.draw(shader)
        except Exception:
            traceback.print_exc()
        finally:
            try:
                if blend_on:
                    gpu.state.blend_set('NONE')
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sprotile Paint Workspace Toolbar Definition
# ---------------------------------------------------------------------------

class MESH_WT_sprotile_paint_tool(bpy.types.WorkSpaceTool):
    """Integrate Sprotile Painting directly as a brush tool in the main Edit Toolbar"""
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'EDIT_MESH'
    bl_idname = "mesh.sprotile_paint_tool"
    bl_label = "Sprotile Paint"
    bl_description = "Paint locked texture atlas tiles onto faces with a brush"
    bl_icon = "ops.paint.texture_paint"
    bl_widget = None
    bl_keymap = (
        ("mesh.sprotile_brush_paint", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
    )


# ---------------------------------------------------------------------------
# Viewport Panel (Sidebar)
# ---------------------------------------------------------------------------

class VIEW3D_PT_atlas_mapper_panel(bpy.types.Panel):
    """Sidebar HUD panel for adjusting settings and toggling atlas previews"""
    bl_label = "Sprotile Paint HUD"
    bl_idname = "VIEW3D_PT_atlas_mapper"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = context.active_object

        # The toggle is always available.  Previously it only appeared in Edit
        # Mode, so leaving Edit Mode with the preview open stranded the overlay
        # on screen with no way to switch it back off.
        col = layout.column(align=True)
        if _preview_operator is None:
            col.operator("mesh.sprotile_preview_start", text="Open Atlas Preview", icon='IMAGE_DATA')
        else:
            col.operator("mesh.sprotile_preview_stop", text="Close Atlas Preview", icon='PANEL_CLOSE')

        in_edit = is_editable_mesh(obj)
        if not in_edit:
            layout.separator()
            layout.label(text="Select mesh in Edit Mode", icon='INFO')
            return

        col = layout.column(align=True)
        col.separator()
        col.prop(scene, "sprotile_tile_size")
        col.prop(scene, "sprotile_tile_mode")
        col.prop(scene, "sprotile_brush_radius")

        layout.separator()
        box = layout.box()
        box.prop(scene, "sprotile_use_uv_inset")
        inset_row = box.row()
        inset_row.enabled = scene.sprotile_use_uv_inset
        inset_row.prop(scene, "sprotile_uv_inset_px")
        box.label(text="Prevents texture seams", icon='INFO')

        layout.separator()
        box = layout.box()
        header = box.row(align=True)
        header.label(text="Tile Orientation:", icon='ORIENTATION_GIMBAL')
        if not transform_is_default(*get_tile_transform(scene)):
            header.operator("mesh.sprotile_reset_transform", text="", icon='LOOP_BACK')
        box.row(align=True).prop(scene, "sprotile_rotation", expand=True)
        flip_row = box.row(align=True)
        flip_row.prop(scene, "sprotile_flip_u", toggle=True)
        flip_row.prop(scene, "sprotile_flip_v", toggle=True)
        box.label(text="Applies when painting or mapping", icon='INFO')

        layout.separator()
        box = layout.box()
        box.label(text="Atlas UI Layout:", icon='PREFERENCES')
        box.prop(scene, "sprotile_preview_width")
        box.prop(scene, "sprotile_preview_left")

        layout.separator()
        box2 = layout.box()
        box2.label(text=f"Active Tile: ({scene.sprotile_active_col}, {scene.sprotile_active_row})", icon='PINNED')
        box2.operator("mesh.sprotile_pipette", text="Pipette from Selected Face", icon='EYEDROPPER')

        layout.separator()
        layout.label(text="Workflow Guide:", icon='INFO')
        layout.label(text="1. Toggle on 'Open Atlas Preview'")
        layout.label(text="2. Select faces in the 3D Viewport")
        layout.label(text="3. Ctrl+LMB or Double-Click tile to map instantly")
        layout.label(text="4. Or press '4' to map selected faces to active tile")
        layout.label(text="5. Or select 'Sprotile Paint' brush tool to paint")
        layout.label(text="6. Over the picker: R rotates, X / Y flip")


# ---------------------------------------------------------------------------
# Cleanup / handlers
# ---------------------------------------------------------------------------

def force_cleanup():
    """Shut down every modal operator we own and remove its draw handler.

    Modal operators are killed without calling cancel() in several situations
    (loading a file, reloading scripts, closing the window).  Without this the
    overlay keeps drawing forever and the picker can never be reopened.
    """
    global _preview_operator, _preview_draw_handle, _image_shader

    if _preview_operator is not None:
        try:
            _preview_operator.stop_requested = True
            _preview_operator.draw_handle = None
        except Exception:
            pass
        _preview_operator = None

    if _preview_draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_preview_draw_handle, 'WINDOW')
        except Exception:
            pass
        _preview_draw_handle = None

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

    _image_shader = None
    tag_redraw_view3d()


@persistent
def _sprotile_load_pre(_dummy):
    force_cleanup()


@persistent
def _sprotile_load_post(_dummy):
    force_cleanup()


# ---------------------------------------------------------------------------
# Registration / Unregistration
# ---------------------------------------------------------------------------

classes = (
    MESH_OT_sprotile_preview_helper,
    MESH_OT_sprotile_pipette,
    MESH_OT_sprotile_map_selected,
    MESH_OT_sprotile_rotate,
    MESH_OT_sprotile_flip,
    MESH_OT_sprotile_reset_transform,
    MESH_OT_sprotile_preview_start,
    MESH_OT_sprotile_preview_stop,
    MESH_OT_sprotile_brush_paint,
    VIEW3D_PT_atlas_mapper_panel,
)

_PROPERTY_NAMES = (
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
    "sprotile_active_col",
    "sprotile_active_row",
)


def _register_properties():
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
        name="Rotation",
        description="Rotate the tile as it is painted onto the face. "
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
        min=100,
        max=1200,
    )
    bpy.types.Scene.sprotile_preview_left = IntProperty(
        name="Preview Left Offset",
        description="Horizontal spacing from the left screen edge in pixels",
        default=50,
        min=0,
        max=400,
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

    _register_properties()

    for cls in classes:
        bpy.utils.register_class(cls)

    try:
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
    # Only the map-selected shortcut is bound by default - rotate and flip are
    # registered as operators so they can be bound without stealing R/X/Y.
    addon_keymaps.clear()
    wm = bpy.context.window_manager
    if wm is not None and wm.keyconfigs is not None:
        kc = wm.keyconfigs.addon
        if kc is not None:
            km = kc.keymaps.new(name='Mesh', space_type='EMPTY')
            kmi = km.keymap_items.new(
                MESH_OT_sprotile_map_selected.bl_idname,
                type='FOUR',
                value='PRESS',
            )
            addon_keymaps.append((km, kmi))

    if _sprotile_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_sprotile_load_pre)
    if _sprotile_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sprotile_load_post)


def unregister():
    global _tool_registered

    # Kill running modal operators and their draw handlers *before* the classes
    # they belong to go away, otherwise Blender dereferences freed types.
    force_cleanup()

    if _sprotile_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_sprotile_load_pre)
    if _sprotile_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sprotile_load_post)

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


if __name__ == "__main__":
    register()
