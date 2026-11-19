"""GPU drawing helpers: text size, the image shader, rectangles, lines, discs and redraw tagging.
"""

import bpy
import gpu
import blf
import math
from gpu_extras.batch import batch_for_shader
from . import state


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


def get_image_shader():
    """Cached nearest-pixel shader for the atlas preview (Blender 3.6+)."""
    if state._image_shader is None:
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
        state._image_shader = gpu.shader.create_from_info(info)
    return state._image_shader


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


def draw_image_quad(tex_shader, x0, y0, x1, y1, uv_rect=(0.0, 0.0, 1.0, 1.0)):
    tex_shader.uniform_float(
        "ModelViewProjectionMatrix",
        gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix(),
    )
    u0, v0, u1, v1 = uv_rect
    batch = batch_for_shader(
        tex_shader, 'TRIS',
        {
            "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
            "texCoord": ((u0, v0), (u1, v0), (u1, v1), (u0, v1)),
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
