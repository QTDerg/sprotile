"""Overlay browser: scanning an atlas for overlay cells, filtering them, and Use as B."""
import json
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sprotile_loader
addon = sprotile_loader.load('sprotile_overlay_browser_tests')
addon.register()
results = []
CELL, SIZE = 64, 256


def run(name, function):
    try:
        function()
    except Exception:
        results.append(dict(name=name, passed=False, traceback=traceback.format_exc()))
        traceback.print_exc()
    else:
        results.append(dict(name=name, passed=True))


def atlas():
    """4 x 4 cells: opaque grey everywhere, except a green fringe at (col 1, row 0 from the top) and a white
    snow cell at (col 2, row 3), both half transparent."""
    import numpy as np
    pixels = np.ones((SIZE, SIZE, 4), dtype=np.float32)
    pixels[..., :3] = .4
    def cell(col, row, rgb):   # row 0 is the top row; Blender's pixel rows start at the bottom
        y = SIZE - (row + 1) * CELL
        block = pixels[y:y + CELL, col * CELL:(col + 1) * CELL]
        block[..., :3] = rgb
        block[:, :CELL // 2, 3] = 0.
    cell(1, 0, (.1, .7, .1))
    cell(2, 3, (1., 1., 1.))
    image = bpy.data.images.new('Overlay browser atlas', SIZE, SIZE, alpha=True)
    image.pixels.foreach_set(pixels.ravel())
    return image


def setup():
    scene = bpy.context.scene
    scene.sprotile_tile_size, scene.sprotile_tile_mode = CELL, 'FULL'
    image = atlas()
    scene.sprotile_blend_a.image = image
    scene.sprotile_overlay_kind = 'ALL'
    return scene, image


def test_scan_finds_two():
    scene, image = setup()
    cells = addon.scan_overlay_cells(image, CELL, force=True)
    assert [(c, r, k) for c, r, k, _ in cells] == [(1, 0, 'grass'), (2, 3, 'snow')], cells
    assert len(addon.overlay_cells(image)) == 2
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    assert len(scene.sprotile_overlay_cells) == 2


def test_filter_by_kind():
    scene, image = setup()
    assert [k for _, _, k, _ in addon.overlay_cells(image, 'grass')] == ['grass']
    assert len(addon.overlay_cells(image, 'snow')) == 1
    assert addon.overlay_cells(image, 'extra') == []
    assert len(addon.overlay_cells(image, 'ALL')) == 2


def test_use_as_b():
    scene, image = setup()
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    scene.sprotile_overlay_index = 1
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    b = scene.sprotile_blend_b
    assert b.image == image and (b.col, b.row) == (2, 3)
    assert (b.width_px, b.height_px) == (CELL, CELL) and (b.atlas_width, b.atlas_height) == (SIZE, SIZE)
    assert not b.is_sampled and tuple(b.sample_uv) == (1, 0, 0, 0, 1, 0)
    assert (b.rotation, b.flip_u, b.flip_v) == (0, False, False)
    assert scene.sprotile_blend_defaults.edge_style == 'OVERLAY'
    assert scene.sprotile_blend_defaults.tiles == 'FACE'
    assert addon.validated_blend_tile(b)['col'] == 2


def test_empty_selection_refused():
    scene, image = setup()
    scene.sprotile_overlay_cells.clear()
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'CANCELLED'}


def test_partial_cells_match_picker():
    import numpy as np
    scene, _ = setup()
    width, height = 2 * CELL + 5, CELL + 9
    pixels = np.ones((height, width, 4), dtype=np.float32)
    pixels[-1, :CELL, 3] = 0.  # top of the complete first cell
    pixels[:9, CELL:2 * CELL, 3] = 0.  # below the complete second cell
    pixels[:, -1, 3] = 0.  # incomplete column on the right
    image = bpy.data.images.new('Padded atlas', width, height, alpha=True)
    image.pixels.foreach_set(pixels.ravel())
    scene.sprotile_blend_a.image = image
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    assert [(c.col, c.row) for c in scene.sprotile_overlay_cells] == [(0, 0)]
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    assert addon.validated_blend_tile(scene.sprotile_blend_b)['image'] == image


def test_use_only_with_scanned_grid():
    scene, original = setup()
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    replacement = atlas()
    scene.sprotile_blend_a.image = replacement
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'CANCELLED'}
    scene.sprotile_blend_a.image = original
    scene.sprotile_tile_size = CELL // 2
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'CANCELLED'}
    assert scene.sprotile_blend_b.image == original and scene.sprotile_blend_b.width_px == CELL
    scene.sprotile_tile_size = CELL
    original.scale(SIZE * 2, SIZE)
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'CANCELLED'}
    scene.sprotile_blend_a.image = replacement
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    assert scene.sprotile_blend_b.image == replacement


def test_filter_selection():
    scene, image = setup()
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    scene.sprotile_overlay_kind = 'snow'
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    assert (scene.sprotile_blend_b.col, scene.sprotile_blend_b.row) == (2, 3)
    scene.sprotile_overlay_kind = 'extra'
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'CANCELLED'}
    scene.sprotile_overlay_kind = 'snow'
    assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
    assert scene.sprotile_overlay_cells[scene.sprotile_overlay_index].kind == 'snow'


def test_explicit_rescan_after_reload():
    import numpy as np
    scene, image = setup()
    with tempfile.TemporaryDirectory() as folder:
        image.file_format = 'PNG'
        image.filepath_raw = str(Path(folder) / 'atlas.png')
        image.save()
        loaded = bpy.data.images.load(image.filepath_raw, check_existing=False)
        scene.sprotile_blend_a.image = loaded
        assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
        assert len(scene.sprotile_overlay_cells) == 2
        image.pixels.foreach_set(np.ones(SIZE * SIZE * 4, dtype=np.float32))
        image.save()
        loaded.reload()
        assert not loaded.is_dirty
        assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
        assert len(scene.sprotile_overlay_cells) == 0


def test_dirty_image_helper():
    import numpy as np
    scene, image = setup()
    assert len(addon.overlay_cells(image)) == 2
    image.pixels.foreach_set(np.ones(SIZE * SIZE * 4, dtype=np.float32))
    image.update()
    assert addon.overlay_cells(image) == []


run('scan finds exactly the two overlay cells', test_scan_finds_two)
run('kind filter lists one', test_filter_by_kind)
run('Use as B sets B, Overlay style and Per Face', test_use_as_b)
run('Use as B refuses without a scanned cell', test_empty_selection_refused)
run('partial rows and columns match picker bounds', test_partial_cells_match_picker)
run('Use as B requires the scanned image, dimensions and tile size', test_use_only_with_scanned_grid)
run('Use as B follows the visible kind after filtering or rescanning', test_filter_selection)
run('Scan rereads a reloaded file of the same size', test_explicit_rescan_after_reload)
run('helper refreshes edited image pixels', test_dirty_image_helper)
report = dict(blender=bpy.app.version_string, passed=sum(r['passed'] for r in results),
              total=len(results), results=results)
(HERE / 'overlay_browser_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
if report['passed'] != report['total']:
    raise SystemExit(1)
