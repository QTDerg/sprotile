"""Overlays: a PNG as B, Soften B (a soft-edged alpha made from a tile) and stored overlay images.
"""

import bpy
import math
import os
import random
import struct
import hashlib
from array import array
import numpy as np
from bpy.props import IntProperty, FloatProperty, StringProperty, EnumProperty


def load_black_key_png(path):
    """Pack a binary mask into a private copy before any shader filtering."""
    with open(path, 'rb') as stream:
        header = stream.read(29)
        if len(header) != 29 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
            raise ValueError('Choose a valid PNG file')
        width, height, depth = struct.unpack('>IIB', header[16:25])
        if not width or not height or width * height > 4 * 1024 * 1024 or depth > 8:
            raise ValueError('Pure black key accepts up to 4 megapixels and 8 bits per channel. Use a smaller cutout or PNG alpha.')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    for existing in bpy.data.images:
        if (existing.get('sprotile_black_key_sha256') == digest and existing.packed_file
                and tuple(existing.size) == (width, height)):
            return existing

    source = bpy.data.images.load(path, check_existing=False)
    keyed = None
    try:
        color_space = source.colorspace_settings.name
        source.alpha_mode = 'CHANNEL_PACKED'  # Read RGB independently of the PNG's alpha.
        if tuple(source.size) != (width, height) or len(source.pixels) != width * height * 4:
            raise ValueError('Blender could not decode the PNG into RGBA pixels')
        rgba = np.empty(len(source.pixels), dtype=np.float32)
        source.pixels.foreach_get(rgba)
        view = rgba.reshape(-1, 4)
        view[:, 3] = (view[:, :3] != 0).any(axis=1)   # pure black is clear, anything else is solid
        keyed = bpy.data.images.new(bpy.path.display_name_from_filepath(path) + ' (black key)',
                                    width, height, alpha=True)
        keyed.colorspace_settings.name = color_space
        keyed.alpha_mode = 'STRAIGHT'
        keyed.pixels.foreach_set(rgba)
        keyed.update()
        keyed.pack()
        keyed['sprotile_black_key_sha256'] = digest
        return keyed
    except Exception:
        if keyed is not None:
            bpy.data.images.remove(keyed)
        raise
    finally:
        bpy.data.images.remove(source)


class MESH_OT_sprotile_load_overlay_image(bpy.types.Operator):
    """Use one whole RGBA PNG as B. For one cell of a sheet, put the sheet in the atlas and Capture B"""
    bl_idname = 'mesh.sprotile_load_overlay_image'
    bl_label = 'Open PNG as B'
    bl_options = {'UNDO'}
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.png', options={'HIDDEN'})
    transparency: EnumProperty(name='Transparency', items=[
        ('ALPHA', 'PNG alpha', 'Use the transparency saved in the PNG, including soft shadows'),
        ('BLACK', 'Pure black key', 'Pack a copy with exact RGB black clear and every other colour fully opaque')],
        default='ALPHA', options={'SKIP_SAVE'})

    def draw(self, context):
        self.layout.prop(self, 'transparency')
        if self.transparency == 'BLACK':
            self.layout.label(text='Only RGB (0, 0, 0) becomes clear')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath.lower().endswith('.png'):
            self.report({'WARNING'}, 'Choose a PNG with an alpha channel')
            return {'CANCELLED'}
        previous = {image.as_pointer() for image in bpy.data.images}
        image = None
        try:
            path = bpy.path.abspath(self.filepath)
            image = load_black_key_png(path) if self.transparency == 'BLACK' else bpy.data.images.load(path, check_existing=True)
            width, height = image_size(image)
            if min(width, height) <= 0:
                raise ValueError('Blender could not read this PNG')
        except (RuntimeError, OSError, ValueError, MemoryError) as error:
            if image and image.as_pointer() not in previous and not image.users:
                bpy.data.images.remove(image)
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        use_whole_image_as_b(context.scene, image)
        self.report({'INFO'}, 'B: '+image.name+'; select the wall faces and Apply')
        return {'FINISHED'}


def use_whole_image_as_b(scene, image):
    """Make a whole RGBA image the B overlay of the next blend."""
    width, height = image_size(image)
    record = scene.sprotile_blend_b
    record.image = image
    record.atlas_width, record.atlas_height = width, height
    record.col = record.row = record.width_px = record.height_px = 0
    record.rotation = 0
    record.flip_u = record.flip_v = False
    record.inset_px = 0
    record.sample_uv = (1,0,0,0,1,0)
    record.is_sampled = True
    scene.sprotile_blend_defaults.edge_style = 'OVERLAY'
    scene.sprotile_blend_settings_source = 'NEW'
    invalidate_clipboard_slot(scene, 'b')
    reset_preview_interaction()


# ---------------------------------------------------------------------------
# Overlay maker: turn an atlas tile into an RGBA overlay with a soft edge
# ---------------------------------------------------------------------------

OVERLAY_SHAPES = [
    ('RAGGED', 'Ragged', 'Uneven natural edge, for rock, dirt or moss patches'),
    ('ROUND', 'Round', 'Soft round splat'),
    ('SQUARE', 'Soft Square', 'The whole tile with a feathered border'),
]
OVERLAY_FOLDER = 'sprotile_overlays'
OVERLAY_MAX_PIXELS = 1024 * 1024
OVERLAY_SIGNATURE = 'sprotile_overlay_signature'


def overlay_value_noise(seed):
    """Smooth 9x9 lattice noise, deterministic per seed. Takes and returns NumPy arrays of u and v."""
    rng = random.Random(seed * 7919 + 17)
    grid = np.array([[rng.random() for _ in range(9)] for _ in range(9)])
    def noise(u, v):
        x, y = np.clip(u, 0., 1.) * 8, np.clip(v, 0., 1.) * 8
        i, j = np.minimum(x.astype(np.int64), 7), np.minimum(y.astype(np.int64), 7)
        fx, fy = x - i, y - j
        fx, fy = fx*fx*(3-2*fx), fy*fy*(3-2*fy)
        low = grid[j, i] + (grid[j, i+1]-grid[j, i])*fx
        high = grid[j+1, i] + (grid[j+1, i+1]-grid[j+1, i])*fx
        return low + (high-low)*fy
    return noise


def overlay_alpha_grid(width, height, shape, softness, roughness, coverage, seed):
    """Alpha mask as a (height, width) array, bottom row first: 1 inside, a smooth fade to 0 at the edge."""
    if shape not in {item[0] for item in OVERLAY_SHAPES}:
        raise ValueError('Unknown overlay shape')
    softness = min(max(float(softness), .02), 1.)
    roughness = min(max(float(roughness), 0.), 1.)
    coverage = min(max(float(coverage), .2), 1.)
    rng = random.Random(seed)
    # A few low harmonics give a lumpy outline; the amplitude falls with frequency.
    harmonics = [(k, 1/k, rng.uniform(0., math.tau)) for k in range(2, 7)]
    norm = sum(amplitude for _, amplitude, _ in harmonics)
    u, v = np.meshgrid((np.arange(width) + .5) / width, (np.arange(height) + .5) / height)
    dx, dy = 2*u - 1, 2*v - 1
    if shape == 'SQUARE':
        s = np.maximum(np.abs(dx), np.abs(dy)) / coverage
    else:
        s = np.hypot(dx, dy) / coverage
        if shape == 'RAGGED' and roughness:
            angle = np.arctan2(dy, dx)
            wobble = sum(a*np.cos(k*angle + phase) for k, a, phase in harmonics) / norm
            s = s/(1 + .35*roughness*wobble) + .3*roughness*(overlay_value_noise(seed)(u, v) - .5)
    t = np.clip((s - (1 - softness)) / softness, 0., 1.)
    return 1 - t*t*(3 - 2*t)


def overlay_alpha(width, height, shape, softness, roughness, coverage, seed):
    """overlay_alpha_grid flattened to floats, bottom row first like Image.pixels."""
    grid = overlay_alpha_grid(width, height, shape, softness, roughness, coverage, seed)
    return array('f', grid.astype(np.float32).tobytes())


def overlay_size(tile, scale):
    """The tile's own texel size, so an overlay keeps the atlas pixel scale."""
    atlas_w, atlas_h = image_size(tile['image'])
    a, b, _c, d, e, _f = tile['sample_uv']
    width = max(1, round(math.hypot(a*atlas_w, d*atlas_h)))
    height = max(1, round(math.hypot(b*atlas_w, e*atlas_h)))
    return width, height, width*scale, height*scale


def overlay_texels(tile, tile_w, tile_h):
    """RGBA at each texel centre of the tile as a (tile_h, tile_w, 4) array, bottom row first.
    Reads only the rows the tile covers, in one block, like image_rgb_sampler."""
    source = tile['image']
    width, height = image_size(source)
    data = source.pixels
    channels = len(data) // (width*height) if width and height else 0
    if channels < 1:
        raise ValueError('Atlas pixels are not available')
    a, b, c, d, e, f = tile['sample_uv']
    tu, tv = np.meshgrid((np.arange(tile_w) + .5) / tile_w, (np.arange(tile_h) + .5) / tile_h)
    u, v = a*tu + b*tv + c, d*tu + e*tv + f
    if not (np.isfinite(u).all() and np.isfinite(v).all()):
        raise ValueError('Preview UV bounds are invalid')
    x = np.clip(np.floor(u*width), 0, width - 1).astype(np.int64)
    y = np.clip(np.floor(v*height), 0, height - 1).astype(np.int64)
    top, bottom = int(y.min()), int(y.max())
    start, end = top*width*channels, (bottom + 1)*width*channels
    if end - start > 8*1024*1024:
        raise ValueError('The preview covers too much of the atlas; use a smaller tile region')
    block = np.frombuffer(array('f', data[start:end]), dtype=np.float32).reshape(bottom - top + 1, width, channels)
    picked = block[y - top, x]
    rgb = picked[..., :3] if channels >= 3 else np.repeat(picked[..., :1], 3, axis=2)
    if channels in (2, 4) and getattr(source, 'alpha_mode', 'STRAIGHT') != 'NONE':
        alpha = np.clip(picked[..., channels - 1], 0., 1.)
    else:
        alpha = np.ones(picked.shape[:2], dtype=np.float32)
    return np.dstack([rgb, alpha]).astype(np.float32)


def original_overlay_atlas(image):
    if image is None:
        return None
    if 'sprotile_overlay_source' in image:
        source = image.get('sprotile_overlay_source')
        return source if isinstance(source, bpy.types.Image) else None
    name = image.get('sprotile_overlay_source_image')  # overlays made before 5.15.2
    return bpy.data.images.get(name) if name else None


def make_overlay_image(tile, shape='RAGGED', softness=.35, roughness=.5, coverage=.9, seed=1, scale=2):
    """Write tile colours with a shaped alpha into a new image, reusing an identical earlier one."""
    tile = tile_as_sample(tile)
    source = tile['image']
    scale = int(scale)
    if scale not in (1, 2, 4):
        raise ValueError('Edge detail must be 1, 2 or 4')
    tile_w, tile_h, width, height = overlay_size(tile, scale)
    if width * height > OVERLAY_MAX_PIXELS:
        raise ValueError('That tile is too large for an overlay; pick a single tile or a smaller region')
    # Colour comes from the tile's own texel centres; only the alpha uses the finer grid. The texels are part of the
    # signature, so an atlas whose pixels changed (a newer version, an edit) makes a new overlay instead of reusing
    # the old colours, and renaming the atlas doesn't (5.15.2).
    texels = overlay_texels(tile, tile_w, tile_h)
    signature = hashlib.sha256(repr((image_size(source), tuple(round(v, 7) for v in tile['sample_uv']),
                                     shape, round(softness, 4), round(roughness, 4), round(coverage, 4),
                                     int(seed), scale)).encode() + texels.tobytes()).hexdigest()
    for existing in bpy.data.images:
        if (existing.get(OVERLAY_SIGNATURE) == signature and tuple(existing.size) == (width, height)
                and original_overlay_atlas(existing) == source
                and existing.colorspace_settings.name == source.colorspace_settings.name):
            existing['sprotile_overlay_source'] = source  # upgrade a matching older name-only record
            if existing.packed_file is not None and not existing.filepath_raw and bpy.data.filepath:
                # Made before the .blend was first saved: write its PNG now, as the guide promises.
                store_overlay_image(existing, existing.name)
            return existing
    colour = texels.repeat(scale, axis=0).repeat(scale, axis=1)
    colour[..., 3] *= overlay_alpha_grid(width, height, shape, softness, roughness, coverage, seed).astype(np.float32)
    pixels = colour.ravel()
    label = {'RAGGED': 'Ragged', 'ROUND': 'Round', 'SQUARE': 'Soft Square'}[shape]
    name = f'{bpy.path.display_name_from_filepath(source.name) or source.name} overlay {label} {signature[:6]}'
    image = bpy.data.images.new(name, width, height, alpha=True)
    try:
        image.colorspace_settings.name = source.colorspace_settings.name
        image.alpha_mode = 'STRAIGHT'
        image.pixels.foreach_set(pixels)
        image[OVERLAY_SIGNATURE] = signature
        image['sprotile_overlay_source_image'] = source.name
        image['sprotile_overlay_source'] = source   # follows a rename, unlike the name
        image['sprotile_overlay_source_uv'] = list(tile['sample_uv'])
        store_overlay_image(image, name)
    except Exception:
        bpy.data.images.remove(image)
        raise
    return image


def store_overlay_image(image, name):
    """Save beside the .blend so Unreal can import it; pack it while the .blend is unsaved."""
    if not bpy.data.filepath:
        image.pack()
        return None
    folder = bpy.path.abspath('//' + OVERLAY_FOLDER)
    os.makedirs(folder, exist_ok=True)
    stem = bpy.path.clean_name(name)
    number = 0
    while True:
        basename = f'{stem}-{number}.png' if number else f'{stem}.png'
        path = os.path.join(folder, basename)
        try:
            with open(path, 'xb'):
                pass
            break
        except FileExistsError:
            number += 1
    packed = image.packed_file is not None
    previous_path = image.filepath_raw
    try:
        image.filepath_raw = path
        image.file_format = 'PNG'
        image.save()
    except Exception:
        image.filepath_raw = previous_path
        if os.path.exists(path):
            os.remove(path)
        raise
    if packed:
        image.unpack(method='USE_ORIGINAL')   # read from the PNG just written from now on
    image.filepath_raw = '//' + OVERLAY_FOLDER + '/' + os.path.basename(path)
    return path


def overlay_source_tile(record):
    """The atlas tile a made overlay came from, so softening again starts from the original tile."""
    image = record.image
    name = image.get('sprotile_overlay_source_image') if image is not None else None
    uv = image.get('sprotile_overlay_source_uv') if image is not None else None
    source = original_overlay_atlas(image)
    whole = record.is_sampled and tuple(record.sample_uv) == (1,0,0,0,1,0)
    if image is not None and image.get(OVERLAY_SIGNATURE) and whole and (
            source is None or source.get(OVERLAY_SIGNATURE) or uv is None or len(uv) != 6):
        # Softening the overlay itself would soften its edge twice and double its size.
        raise ValueError(f"The atlas {name or 'this overlay'} came from can't be found (renamed or removed); "
                         'Capture B from the atlas again, then Soften B')
    if source is None or uv is None or len(uv) != 6 or not whole:
        return validated_blend_tile(record)
    width, height = image_size(source)
    coefficients = tuple(float(v) for v in uv)
    sample_uv_bounds(coefficients)
    return dict(image=source, col=0, row=0, width_px=0, height_px=0, atlas_width=width, atlas_height=height,
                rotation=0, flip_u=False, flip_v=False, inset_px=0., is_sampled=True, sample_uv=coefficients)


class MESH_OT_sprotile_make_overlay(bpy.types.Operator):
    """Turn tile B into an overlay with a soft, uneven edge, so it can lie over tile A (rock over grass, moss over stone)"""
    bl_idname = 'mesh.sprotile_make_overlay'
    bl_label = 'Soften B'
    bl_options = {'REGISTER', 'UNDO'}
    shape: EnumProperty(name='Shape', items=OVERLAY_SHAPES, default='RAGGED')
    softness: FloatProperty(name='Soft Edge', description='How wide the fade at the edge is',
                            subtype='FACTOR', min=.02, max=1., default=.35)
    roughness: FloatProperty(name='Roughness', description='How uneven a Ragged edge is',
                             subtype='FACTOR', min=0., max=1., default=.5)
    coverage: FloatProperty(name='Size', description='How much of the tile the solid part fills',
                            subtype='FACTOR', min=.2, max=1., default=.9)
    seed: IntProperty(name='Variation', description='Try other numbers for a different outline',
                      min=0, max=9999, default=1)
    scale: EnumProperty(name='Edge Detail', items=[
        ('1', 'Tile pixels', 'Alpha at the tile resolution, the most PS1-like'),
        ('2', '2x', 'Alpha at twice the tile resolution; colours keep the tile pixels'),
        ('4', '4x', 'Alpha at four times the tile resolution; colours keep the tile pixels')], default='2')

    @classmethod
    def poll(cls, context):
        record = getattr(context.scene, 'sprotile_blend_b', None)
        return record is not None and record.image is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, 'shape')
        layout.prop(self, 'softness', slider=True)
        if self.shape == 'RAGGED':
            layout.prop(self, 'roughness', slider=True)
        layout.prop(self, 'coverage', slider=True)
        if self.shape == 'RAGGED':
            layout.prop(self, 'seed')
        layout.prop(self, 'scale')

    def execute(self, context):
        scene = context.scene
        try:
            tile = overlay_source_tile(scene.sprotile_blend_b)
            image = make_overlay_image(tile, self.shape, self.softness, self.roughness, self.coverage,
                                       self.seed, int(self.scale))
        except (RuntimeError, OSError, ValueError, MemoryError) as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        use_whole_image_as_b(scene, image)
        where = 'saved in ' + OVERLAY_FOLDER if image.packed_file is None else 'packed; save the .blend to write a PNG'
        self.report({'INFO'}, f'B: {image.name} ({where}). Select faces and Apply')
        return {'FINISHED'}


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .tiles import image_size
from .blend_plan import validated_blend_tile
from .blend_recipes import sample_uv_bounds
from .presets import tile_as_sample
from .blend_ops import invalidate_clipboard_slot, reset_preview_interaction
