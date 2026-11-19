"""Render overlays selected from an external sheet through the public workflow."""

import json
from pathlib import Path
import struct
import sys
import tempfile
import traceback
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shared_blend as fixture

addon, bpy = fixture.addon, fixture.bpy
CELL = 16
BASE_CELL = 64
WIDTH, HEIGHT = 300, 100
RED, BLUE, CYAN = (255, 0, 0), (0, 0, 255), (0, 255, 255)
GREEN, YELLOW = (0, 255, 0), (255, 255, 0)
EXPECTED = dict(left_clear=RED, left_solid=GREEN, right_clear=BLUE, right_solid=YELLOW,
                separate_clear=CYAN, separate_solid=YELLOW)


def png_image(path, width, height, rgba):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload)))

    rows = b''.join(b'\0' + bytes(channel for x in range(width) for channel in rgba(x, y))
                    for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 6, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    image = bpy.data.images.load(str(path), check_existing=False)
    image.colorspace_settings.name = 'Non-Color'
    image.pack()
    return image


def sheet_pixel(x, y):
    cell = x // CELL, y // CELL
    if cell in ((1, 0), (2, 1)):
        if x % CELL < CELL // 2:
            return (255, 0, 255, 0)
        return (*{(1, 0): GREEN, (2, 1): YELLOW}[cell], 255)
    return (255, 255, 255, 255)


def set_up(folder):
    obj, _, _, _ = fixture.setup()
    fixture.leave()
    scene = bpy.context.scene
    atlas_path, sheet_path = folder/'Mesh atlas.png', folder/'External sheet.png'
    atlas = png_image(atlas_path, BASE_CELL * 4, BASE_CELL,
                      lambda x, y: (*(RED, BLUE, CYAN, (0, 0, 0))[x // BASE_CELL], 255))
    sheet = png_image(sheet_path, CELL * 3, CELL * 2, sheet_pixel)
    obj.data.materials[0] = fixture.material(atlas)
    separate = fixture.object_mesh(fixture.material(atlas), name='Separate overlay')
    separate.location.x = 2
    for mesh in (obj.data, separate.data):
        for corner in mesh.color_attributes['Tint'].data:
            corner.color = (.5, .5, .5, 1)
    scene.sprotile_rectangle_corner_mode = 'AUTO'
    scene.sprotile_overlay_source = 'SHEET'
    scene.sprotile_overlay_sheet = sheet
    scene.sprotile_overlay_sheet_size = CELL
    scene.sprotile_overlay_kind = 'ALL'
    return obj, separate, atlas, sheet, (atlas_path, sheet_path)


def apply_patch(obj, faces, atlas, a_column, sheet, b_cell, mode):
    fixture.select_faces(obj, faces)
    fixture.capture(atlas, a=a_column)
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = mode
    scene.sprotile_blend_settings_source = 'NEW'
    scene.sprotile_blend_defaults.opacity = 100
    matches = [index for index, entry in enumerate(scene.sprotile_overlay_cells)
               if (entry.col, entry.row) == b_cell]
    assert len(matches) == 1, (b_cell, matches)
    scene.sprotile_overlay_index = matches[0]
    assert bpy.ops.mesh.sprotile_use_overlay_cell() == {'FINISHED'}
    assert scene.sprotile_blend_b.image == sheet
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}


def assert_shared_bank(obj, sheet):
    material = obj.data.materials[0]
    bank = addon.shared_overlay_bank(material)
    assert len(bank) == 1 and next(iter(bank.values())).image == sheet, bank
    report = addon.unreal_report(obj.data)
    assert report['overlays'] == [(material.name, 1, sheet.name, True)], report
    return report['overlays']


def configure_render():
    scene = bpy.context.scene
    data = bpy.data.cameras.new('Overlay sheet camera')
    camera = bpy.data.objects.new('Overlay sheet camera', data)
    scene.collection.objects.link(camera)
    data.type = 'ORTHO'
    data.ortho_scale = 3
    camera.location = (1.5, .5, 5)
    scene.camera = camera
    engines = bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items
    scene.render.engine = next(engine.identifier for engine in engines if engine.identifier.startswith('BLENDER_EEVEE'))
    scene.render.resolution_x, scene.render.resolution_y = WIDTH, HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'


def render(folder, name):
    path = folder/f'{name}.exr'
    bpy.context.scene.render.filepath = str(path)
    assert bpy.ops.render.render(write_still=True) == {'FINISHED'}
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        pixels = list(image.pixels)
        samples = {}
        for label, x in zip(EXPECTED, (25, 75, 125, 175, 225, 275)):
            offset = (WIDTH * 45 + x) * 4
            actual = tuple(pixels[offset:offset + 3])
            expected = tuple(channel / 255 for channel in EXPECTED[label])
            assert max(abs(a - b) for a, b in zip(actual, expected)) < .03, (label, actual, expected)
            samples[label] = actual
        return samples
    finally:
        bpy.data.images.remove(image)


def test_sheet_renders_and_survives_reopen():
    with tempfile.TemporaryDirectory(prefix='overlay-sheet-', dir=fixture.HERE) as directory:
        folder = Path(directory)
        obj, separate, atlas, sheet, sources = set_up(folder)
        assert bpy.ops.mesh.sprotile_scan_overlay_cells() == {'FINISHED'}
        assert {(entry.col, entry.row) for entry in bpy.context.scene.sprotile_overlay_cells} == {(1, 0), (2, 1)}
        apply_patch(obj, [0, 1], atlas, 0, sheet, (1, 0), 'SHARED')
        apply_patch(obj, [2, 3], atlas, 1, sheet, (2, 1), 'SHARED')
        apply_patch(separate, [0, 1], atlas, 2, sheet, (2, 1), 'SEPARATE')
        fixture.leave()
        export = assert_shared_bank(obj, sheet)
        configure_render()
        before = render(folder, 'before')
        name, sheet_name = obj.name, sheet.name
        saved = folder/'external-overlay-sheet.blend'
        assert bpy.ops.wm.save_as_mainfile(filepath=str(saved)) == {'FINISHED'}
        for source in sources:
            source.unlink()
        assert bpy.ops.wm.open_mainfile(filepath=str(saved)) == {'FINISHED'}
        reopened_sheet = bpy.data.images[sheet_name]
        assert reopened_sheet.packed_file
        assert assert_shared_bank(bpy.data.objects[name], reopened_sheet) == export
        after = render(folder, 'after')
        return dict(before=before, reopened=after, export=export)


if __name__ == '__main__':
    try:
        detail = test_sheet_renders_and_survives_reopen()
    except Exception:
        results = [dict(name='external sheet render and reopen', passed=False, traceback=traceback.format_exc())]
    else:
        results = [dict(name='external sheet render and reopen', passed=True, detail=detail)]
    report = dict(results=results, passed=sum(result['passed'] for result in results), total=len(results))
    (fixture.HERE/'overlay_sheet_render_results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    fixture.leave()
    addon.unregister()
    if report['passed'] != report['total']:
        raise SystemExit(1)
