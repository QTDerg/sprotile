"""Read complete atlas cells in picker order and classify those that mix clear and solid texels."""

from collections import OrderedDict

import numpy as np

ALPHA_CUTOFF = .5
_scan_cache = OrderedDict()


def classify_colour(rgb):
    """grass, snow or extra from a cell's mean opaque colour. Tuned on the 549 Giga V2.8 overlay cells (9 Nov 2026):
    grass covers green through olive and yellow-green, snow covers pale greys and pale blue or violet, and everything
    else (roots, dirt, leaves, sand, flames) is extra."""
    r, g, b = (float(v) for v in rgb)
    high, low = max(r, g, b), min(r, g, b)
    saturation = (high - low) / high if high > 1e-6 else 0.
    delta = high - low
    if delta < 1e-6:
        hue = 0.
    elif high == r:
        hue = (60. * ((g - b) / delta)) % 360.
    elif high == g:
        hue = 60. * ((b - r) / delta) + 120.
    else:
        hue = 60. * ((r - g) / delta) + 240.
    if high >= .55 and (saturation < .3 or 180. <= hue <= 300.):
        return 'snow'
    if 50. <= hue <= 185. and g >= r * .95 and g >= b:
        return 'grass'
    return 'extra'


def scan_key(image, cell):
    return image.session_uid, tuple(image.size), image.filepath, image.alpha_mode, image.colorspace_settings.name, cell


def scan_overlay_cells(image, cell=64, force=False):
    """Every overlay cell of `image` as (col, row, kind, (r, g, b)); row 0 is the top row, like the picker."""
    width, height = (int(v) for v in image.size)
    cell = max(1, int(cell))
    if width < cell or height < cell:
        return []
    if not image.has_data:
        image.reload()
        width, height = image.size
        if not image.has_data or min(width, height) <= 0:
            raise ValueError('Atlas pixels are unavailable')
    key = scan_key(image, cell)
    if not force and not image.is_dirty and key in _scan_cache:
        _scan_cache.move_to_end(key)
        return list(_scan_cache[key])
    if image.alpha_mode == 'NONE':
        return []
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)   # Blender stores rows from the bottom
    cols, rows = width // cell, height // cell
    found = []
    for row in range(rows):   # the picker starts at the top, even with an incomplete bottom row
        low = height - (row + 1) * cell
        block = pixels[low:low + cell, :cols * cell].reshape(cell, cols, cell, 4)
        solid = block[..., 3] >= ALPHA_CUTOFF
        solid_count = solid.sum(axis=(0, 2))
        total = cell * cell
        for col in np.nonzero((solid_count > 0) & (solid_count < total))[0]:
            texels = block[:, col][solid[:, col]][:, :3]
            rgb = tuple(float(v) for v in texels.mean(axis=0))
            found.append((int(col), row, classify_colour(rgb), rgb))
    _scan_cache[key] = found
    _scan_cache.move_to_end(key)
    if len(_scan_cache) > 8:
        _scan_cache.popitem(last=False)
    return list(found)


def overlay_cells(image, kind=None, cell=64):
    """The cached cells of `image` (scanning it when it has not been scanned), optionally of one kind."""
    cells = scan_overlay_cells(image, cell)
    if kind and kind != 'ALL':
        kind = kind.lower()
        return [item for item in cells if item[2] == kind]
    return cells
