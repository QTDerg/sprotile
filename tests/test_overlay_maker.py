"""Soften B into an RGBA overlay, and prepare shared blends for Unreal's UV order."""
import json
import os
import sys
import tempfile
import traceback
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shared_blend as t

addon, bpy, bmesh = t.addon, t.bpy, t.bmesh


def setup():
    obj, bm, atlas, mat = t.setup()
    scene = bpy.context.scene
    scene.sprotile_rectangle_corner_mode = 'AUTO'
    scene.sprotile_blend_defaults.opacity = 100
    scene.sprotile_blend_defaults.edge_style = 'OVERLAY'
    return obj, bm, atlas, mat


def pixels(image):
    data = array('f', [0.0]) * (len(image.pixels))
    image.pixels.foreach_get(data)
    return data


def rgba(image, x, y):
    data = pixels(image)
    index = 4 * (y * image.size[0] + x)
    return tuple(data[index:index+4])


def test_alpha_shapes():
    for shape in ('RAGGED', 'ROUND', 'SQUARE'):
        alpha = addon.overlay_alpha(32, 32, shape, .3, .6, .9, 7)
        centre, corner = alpha[16*32+16], alpha[0]
        assert centre > .99 and corner < .01, (shape, centre, corner)
        assert all(0. <= value <= 1. for value in alpha)
        assert any(.05 < value < .95 for value in alpha), shape + ' has no soft edge'
    a = addon.overlay_alpha(24, 24, 'RAGGED', .3, .8, .9, 1)
    b = addon.overlay_alpha(24, 24, 'RAGGED', .3, .8, .9, 2)
    assert a == addon.overlay_alpha(24, 24, 'RAGGED', .3, .8, .9, 1), 'the same variation must repeat'
    assert a != b, 'another variation must change the outline'
    smooth = addon.overlay_alpha(24, 24, 'RAGGED', .3, 0., .9, 3)
    assert smooth == addon.overlay_alpha(24, 24, 'ROUND', .3, 0., .9, 3), 'zero roughness is round'
    try:
        addon.overlay_alpha(8, 8, 'STAR', .3, .5, .9, 1)
    except ValueError:
        pass
    else:
        raise AssertionError('unknown shapes must be refused')
    return 'Ragged, Round and Soft Square: opaque centre, clear corner, soft band; variations repeat and differ.'


def test_overlay_speed_and_texels():
    """Soften B stays quick at every Edge Detail and keeps each tile texel's colour exactly."""
    import time
    start = time.perf_counter()
    big = addon.overlay_alpha(1024, 1024, 'RAGGED', .35, .6, .9, 3)
    seconds = time.perf_counter() - start
    assert len(big) == 1024*1024 and seconds < 1.5, seconds
    obj, bm, atlas, mat = setup()
    t.capture(atlas, 0, 1)
    tile = addon.overlay_source_tile(bpy.context.scene.sprotile_blend_b)
    timings = {}
    for scale in (1, 2, 4):
        start = time.perf_counter()
        image = addon.make_overlay_image(tile, 'RAGGED', .35, .6, .9, 40 + scale, scale)
        timings[scale] = (time.perf_counter() - start) * 1000
        width = image.size[0]
        data = pixels(image)
        # Every pixel of one texel block carries that texel's colour; only alpha varies.
        for x0, y0 in ((0, 0), (width // 2, width // 2)):
            block = [data[4*((y0 + dy)*width + x0 + dx):4*((y0 + dy)*width + x0 + dx) + 3]
                     for dy in range(scale) for dx in range(scale)]
            assert all(colour == block[0] for colour in block), (scale, block)
        bpy.data.images.remove(image)
    assert max(timings.values()) < 400, timings
    return f'1024 px alpha in {seconds*1000:.0f} ms; one tile at 1x/2x/4x in ' + \
        ' / '.join(f'{timings[s]:.0f}' for s in (1, 2, 4)) + ' ms.'


def test_make_overlay_packed_then_reused():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    t.capture(atlas, 0, 1)  # B is the green tile.
    assert bpy.ops.mesh.sprotile_make_overlay(shape='ROUND', softness=.4, coverage=.9, scale='2') == {'FINISHED'}
    image = scene.sprotile_blend_b.image
    assert image is not atlas and tuple(image.size) == (128, 128), tuple(image.size)
    assert image.packed_file is not None, 'an unsaved .blend packs the overlay'
    assert scene.sprotile_blend_defaults.edge_style == 'OVERLAY'
    assert tuple(scene.sprotile_blend_b.sample_uv) == (1, 0, 0, 0, 1, 0)
    centre, corner = rgba(image, 64, 64), rgba(image, 0, 0)
    assert centre[1] > .7 and centre[0] < .1 and centre[3] > .99, centre
    assert corner[3] < .01 and corner[1] > .7, 'clear pixels keep the tile colour (no dark fringe)'
    count = len(bpy.data.images)
    t.capture(atlas, 0, 1)
    assert bpy.ops.mesh.sprotile_make_overlay(shape='ROUND', softness=.4, coverage=.9, scale='2') == {'FINISHED'}
    assert scene.sprotile_blend_b.image == image and len(bpy.data.images) == count, 'identical settings reuse'
    # Softening the made overlay starts again from its source tile, not from the softened copy.
    assert bpy.ops.mesh.sprotile_make_overlay(shape='SQUARE', softness=.2, coverage=1., scale='1') == {'FINISHED'}
    square = scene.sprotile_blend_b.image
    assert square != image and tuple(square.size) == (64, 64)
    assert rgba(square, 2, 32)[3] > .2, 'the square edge comes from the opaque source tile'
    return 'Overlay is the tile colour with shaped alpha, packed while unsaved, reused, and re-softened from its source.'


def test_saved_blend_writes_png_and_applies():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    folder = Path(tempfile.mkdtemp(prefix='sprotile-overlay-'))
    t.leave()
    bpy.ops.wm.save_as_mainfile(filepath=str(folder/'lab.blend'))
    t.capture(atlas, 0, 2)  # B is blue.
    assert bpy.ops.mesh.sprotile_make_overlay(shape='RAGGED', seed=4, scale='1') == {'FINISHED'}
    image = scene.sprotile_blend_b.image
    assert image.packed_file is None and image.filepath_raw.startswith('//' + addon.OVERLAY_FOLDER + '/')
    path = Path(bpy.path.abspath(image.filepath_raw))
    assert path.exists() and path.stat().st_size > 100, path
    t.capture(atlas, 3, 2)
    use = scene.sprotile_blend_a
    assert use.image == atlas
    use_b = scene.sprotile_blend_b
    addon.use_whole_image_as_b(scene, image)
    t.select_faces(obj, [0, 1])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    recipe = addon.read_blend_recipe(bpy.context)
    assert recipe['b']['image'] == image and recipe['settings']['edge_style'] == 'OVERLAY'
    bank = addon.shared_overlay_bank(mat)
    assert [sampler.image for sampler in bank.values()] == [image]
    assert use_b.image == image
    return 'A saved .blend writes the overlay PNG beside it, and the overlay applies as a shared overlay.'


def test_unreal_report_and_reorder():
    obj, bm, atlas, mat = setup()
    t.leave()
    mesh = obj.data
    assert addon.unreal_report(mesh) is None, 'no shared blends, nothing to report'
    extra = mesh.uv_layers.new(name='Lightmap')
    for index, entry in enumerate(extra.data):
        entry.uv = (index * .01, .5)
    mesh.uv_layers['Original UVs'].active_render = True
    mesh.uv_layers.active = mesh.uv_layers['Original UVs']
    scene = bpy.context.scene
    scene.sprotile_blend_defaults.edge_style = 'PLAIN'
    t.capture(atlas, 0, 1)
    t.select_faces(obj, [0, 1])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    before_recipe = addon.read_blend_recipe(bpy.context)
    t.capture(atlas, 2, 3)
    scene.sprotile_blend_defaults.edge_style = 'OVERLAY'
    t.select_faces(obj, [2, 3])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    overlay_recipe = addon.read_blend_recipe(bpy.context)
    t.leave()
    report = addon.unreal_report(mesh)
    assert report['names'][:2] == ['Original UVs', 'Lightmap'] and not report['ordered']
    unreal = ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV, 'Lightmap']
    assert report['order'] == unreal, report['order']
    assert not report['shaped'] and report['overlays'] == [(mat.name, 1, atlas.name, True)], report['overlays']
    values = {}
    for layer in mesh.uv_layers:
        data = array('f', [0.0]) * (2*len(mesh.loops)); layer.data.foreach_get('uv', data); values[layer.name] = data
    bpy.ops.object.select_all(action='DESELECT'); obj.select_set(True); bpy.context.view_layer.objects.active = obj
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    names = [layer.name for layer in mesh.uv_layers]
    assert names == unreal, names
    for name, data in values.items():
        after = array('f', [0.0]) * len(data); mesh.uv_layers[name].data.foreach_get('uv', after)
        assert after == data, name + ' values moved with the map'
    assert addon.SHARED_EDGE_UV not in mesh.uv_layers, 'no grass or snow, so no Edge map'
    assert mesh.uv_layers['Original UVs'].active_render and mesh.uv_layers.active.name == 'Original UVs'
    assert addon.unreal_report(mesh)['ordered']
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}, 'already ordered is a no-op, not an error'
    t.select_faces(obj, [0, 1])
    assert addon.recipes_match(before_recipe, addon.read_blend_recipe(bpy.context))
    t.select_faces(obj, [2, 3])
    assert addon.recipes_match(overlay_recipe, addon.read_blend_recipe(bpy.context))
    # Edit Mode is left and restored around the rewrite.
    assert obj.mode == 'EDIT' and bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'} and obj.mode == 'EDIT'
    # A second overlay source that a later blend replaces is listed as unused.
    t.capture(atlas, 0, 1)
    assert bpy.ops.mesh.sprotile_make_overlay(shape='ROUND', scale='1') == {'FINISHED'}
    made = scene.sprotile_blend_b.image
    t.select_faces(obj, [0, 1])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    assert (mat.name, 2, made.name, True) in addon.unreal_report(mesh)['overlays']
    # Shaped edges are reported so the Unreal switch can be set.
    scene.sprotile_blend_defaults.edge_style = 'GRASS'
    t.capture(atlas, 1, 0)
    t.select_faces(obj, [0, 1])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    report = addon.unreal_report(mesh)
    assert report['shaped'] and not report['ordered'], report      # Apply added Edge after the user's maps
    assert report['order'] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV, addon.SHARED_EDGE_UV, 'Lightmap']
    assert (mat.name, 2, made.name, False) in report['overlays'], report['overlays']
    assert (mat.name, 1, atlas.name, True) in report['overlays']
    bpy.ops.object.select_all(action='DESELECT'); obj.select_set(True); bpy.context.view_layer.objects.active = obj
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'} and addon.unreal_report(mesh)['ordered']
    return 'Unreal order is the render map (A), B, Factor, Edge only with grass or snow, then extras; values, render/active maps and recipes survive.'


def test_unreal_limits():
    """5.13 adds only B and Factor (and Edge for grass or snow), so Prepare never needs a map Apply did not add.
    A mesh full of user maps is refused at Apply, before anything changes."""
    obj, bm, atlas, mat = setup()
    t.leave()
    mesh = obj.data
    for index in range(5):
        mesh.uv_layers.new(name=f'Spare {index}')
    scene = bpy.context.scene
    scene.sprotile_blend_defaults.edge_style = 'PLAIN'
    t.capture(atlas, 0, 1)
    t.select_faces(obj, [0, 1])
    scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    report = addon.unreal_report(mesh)
    assert not report['too_many'] and len(report['order']) == 8, report['order']
    bpy.ops.object.select_all(action='DESELECT'); obj.select_set(True); bpy.context.view_layer.objects.active = obj
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'} and addon.unreal_report(mesh)['ordered']
    names = [layer.name for layer in mesh.uv_layers]
    assert names[:3] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV], names
    scene.sprotile_blend_defaults.edge_style = 'GRASS'
    t.select_faces(obj, [2, 3])
    scene.sprotile_blend_settings_source = 'NEW'
    try:
        addon.apply_texture_blend(bpy.context)
    except ValueError as error:
        assert 'eight-map limit' in str(error), error
    else:
        raise AssertionError('a ninth UV map (Edge) must be refused')
    t.leave()
    assert [layer.name for layer in mesh.uv_layers] == names, 'a refused Apply changes nothing'
    return 'Six user maps leave room for B and Factor; a grass edge (a ninth map) is refused at Apply with nothing changed.'


def test_soften_keeps_each_source():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    other = t.image()  # identical tiles in an independent image
    def soften(source):
        t.capture(source, 0, 1)
        assert bpy.ops.mesh.sprotile_make_overlay(shape='ROUND', scale='1') == {'FINISHED'}
        return scene.sprotile_blend_b.image
    first, second = soften(atlas), soften(other)
    assert first != second
    assert first.get('sprotile_overlay_source') == atlas
    assert second.get('sprotile_overlay_source') == other
    other.name = 'Renamed independent atlas'
    assert soften(other) == second
    assert soften(atlas) == first


def test_overlay_png_collision():
    with tempfile.TemporaryDirectory() as folder:
        t.leave()
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(folder) / 'overlay.blend'))
        directory = Path(folder) / addon.OVERLAY_FOLDER
        directory.mkdir()
        existing = directory / 'Moss.png'
        existing.write_bytes(b'artist file')
        image = bpy.data.images.new('Overlay PNG collision', 4, 4, alpha=True)
        path = Path(addon.store_overlay_image(image, 'Moss'))
        assert existing.read_bytes() == b'artist file'
        assert path != existing and path.read_bytes().startswith(b'\x89PNG')
        assert Path(bpy.path.abspath(image.filepath_raw)) == path


def test_prepare_skips_user_maps():
    obj, bm, atlas, mat = setup()
    t.leave()
    mesh = obj.data
    for name in (addon.BLEND_B_UV, addon.SHARED_FACTOR_UV, addon.SHARED_EDGE_UV):
        mesh.uv_layers.new(name=name)
    mesh.uv_layers[addon.SHARED_FACTOR_UV].data[0].uv = (.25, .75)
    names = list(mesh.uv_layers.keys())
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert list(mesh.uv_layers.keys()) == names
    assert tuple(mesh.uv_layers[addon.SHARED_FACTOR_UV].data[0].uv) == (.25, .75)
    assert addon.unreal_report(mesh) is None


def test_prepare_keeps_uv_editor_settings():
    obj, bm, atlas, mat = setup()
    t.leave()
    mesh = obj.data
    mesh.uv_layers.new(name='Lightmap')
    mesh.uv_layers['Original UVs'].active_render = True
    t.select_faces(obj, [0, 1])
    t.capture(atlas, 0, 1)
    assert addon.apply_texture_blend(bpy.context)
    # Set different flags on two UV maps using the UV editor's BMesh API.
    bm = bmesh.from_edit_mesh(mesh)
    bm.faces.ensure_lookup_table()
    for index, name in enumerate(('Original UVs', 'Lightmap')):
        layer = bm.loops.layers.uv[name]
        for face in bm.faces:
            for corner, loop in enumerate(face.loops):
                loop[layer].pin_uv = corner == index
                if hasattr(bpy.types.MeshUVLoop, 'select'):
                    loop[layer].select = corner != index
                    loop[layer].select_edge = corner == index + 1
    bmesh.update_edit_mesh(mesh)
    t.leave()
    def flags(name):
        return [tuple(getattr(loop, key) for key in ('pin_uv', 'select', 'select_edge') if hasattr(loop, key))
                for loop in mesh.uv_layers[name].data]
    before = {name: flags(name) for name in ('Original UVs', 'Lightmap')}
    # Blender 5.x stores UV selection on the mesh instead of each UV map.
    shared_selection = {}
    for name in ('.uv_select_vert', '.uv_select_edge', '.uv_select_face'):
        attr = mesh.attributes.get(name)
        if attr is not None:
            for index, item in enumerate(attr.data):
                item.value = index % 3 == 1
            shared_selection[name] = [item.value for item in attr.data]
    mesh.uv_layers.active = mesh.uv_layers['Lightmap']
    mesh.uv_layer_clone_index = mesh.uv_layers.find(addon.SHARED_FACTOR_UV)
    mesh.uv_layer_stencil_index = mesh.uv_layers.find('Lightmap')
    assert not addon.unreal_report(mesh)['ordered']
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert addon.unreal_report(mesh)['ordered']
    assert all(flags(name) == old for name, old in before.items())
    assert all([item.value for item in mesh.attributes[name].data] == old
               for name, old in shared_selection.items())
    assert mesh.uv_layers.active.name == 'Lightmap'
    assert mesh.uv_layers[mesh.uv_layer_clone_index].name == addon.SHARED_FACTOR_UV
    assert mesh.uv_layers[mesh.uv_layer_stencil_index].name == 'Lightmap'


def test_prepare_recovers_joined_mesh_and_protects_user_edge():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    scene.sprotile_blend_defaults.edge_style = 'PLAIN'
    assert addon.apply_texture_blend(bpy.context)
    mesh = obj.data
    t.leave()
    assert mesh[addon.BLEND_MESH_TAG] == 2
    del mesh[addon.BLEND_MESH_TAG]  # Join can drop the version, while retaining the shader and face data
    user_edge = mesh.uv_layers.new(name=addon.SHARED_EDGE_UV)
    user_edge.data[0].uv = (.25, 0.)
    assert addon.unreal_report(mesh) is not None
    t.select_faces(obj, [0, 1])
    assert addon.unreal_report(mesh) is not None
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    t.leave()
    assert tuple(mesh.uv_layers[addon.SHARED_EDGE_UV].data[0].uv) == (.25, 0.)
    mesh.uv_layers[addon.BLEND_B_UV].active_render = True
    before = list(mesh.uv_layers.keys())
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'CANCELLED'}
    assert list(mesh.uv_layers.keys()) == before


results = []
for test in (test_alpha_shapes, test_overlay_speed_and_texels, test_make_overlay_packed_then_reused,
             test_saved_blend_writes_png_and_applies,
             test_unreal_report_and_reorder, test_unreal_limits, test_soften_keeps_each_source,
             test_prepare_skips_user_maps, test_prepare_keeps_uv_editor_settings,
             test_prepare_recovers_joined_mesh_and_protects_user_edge, test_overlay_png_collision):
    try:
        detail = test()
    except Exception:
        traceback.print_exc()
        results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
    else:
        results.append(dict(name=test.__name__, passed=True, detail=detail))
        print('PASS', test.__name__)
report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
(t.HERE/'overlay_maker_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
t.leave(); addon.unregister()
if report['passed'] != report['total']:
    raise SystemExit(1)
