"""External Overlay cells through Apply, Edit, Copy, presets, and Prepare UVs."""
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_shared_blend as t

addon, bpy = t.addon, t.bpy
CELL = 64


def sheet(width=192, height=128):
    image = bpy.data.images.new('External RGBA overlays', width, height, alpha=True)
    image.pixels.foreach_set([value for _y in range(height) for x in range(width)
                              for value in (.2, .7, .2, float(x % CELL < CELL // 2))])
    return image


def current_recipe():
    return addon.read_blend_recipe(bpy.context)


def b_bounds():
    return addon.sample_uv_bounds(current_recipe()['b']['sample_uv'])


def close(actual, expected):
    assert len(actual) == len(expected)
    assert max(abs(a - b) for a, b in zip(actual, expected)) < 2e-6, (actual, expected)


def cell_bounds(size=CELL):
    return ((size + .25) / 192, (128 - 2 * size + .25) / 128, (size - .5) / 192, (size - .5) / 128)


def setup(*, material_mode='SHARED', stretch=False, whole_image=False, cell_size=CELL):
    obj, _bm, atlas, material = t.setup()
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = material_mode
    scene.sprotile_patch_projection = 'SURFACE'
    scene.sprotile_blend_settings_source = 'NEW'
    external = sheet(CELL, CELL) if whole_image else sheet()
    if whole_image:
        addon.use_whole_image_as_b(scene, external)
    else:
        addon.use_cell_as_b(scene, external, 1, 1, cell_size=cell_size)
    if stretch:
        t.leave()
        for vertex in obj.data.vertices:
            vertex.co.x *= 2
        t.select_faces(obj, [0, 1, 2, 3])
        scene.sprotile_blend_defaults.tiles = 'PATCH'
        scene.sprotile_patch_fit = 'KEEP_SHAPE'
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    return obj, atlas, material, external


def test_shared_and_separate_roundtrip():
    for mode in ('SHARED', 'SEPARATE'):
        obj, atlas, material, external = setup(material_mode=mode)
        scene = bpy.context.scene
        initial = current_recipe()
        assert initial['a']['image'] == atlas and initial['b']['image'] == external
        close(b_bounds(), cell_bounds())
        assert bpy.ops.mesh.sprotile_pick_blend() == {'FINISHED'}
        assert addon.copied_tile_in_grid(scene, scene.sprotile_blend_b)[:3] == ('FULL', 1, 1)
        addon.add_preset(scene, initial, 'External sheet ' + mode)
        preset = addon.selected_preset(scene)
        assert preset.preview_data and addon.recipes_match(initial, addon.preset_recipe(preset))
        assert bpy.ops.mesh.sprotile_load_preset() == {'FINISHED'}
        assert scene.sprotile_blend_b.image == external
        assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
        assert addon.recipes_match(initial, current_recipe())
        assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CW') == {'FINISHED'}
        turned = current_recipe()
        assert initial['b']['sample_uv'] != turned['b']['sample_uv']
        close(turned['a']['sample_uv'], initial['a']['sample_uv'])
        close(b_bounds(), cell_bounds())
        if mode == 'SHARED':
            addon.active_shared_patch(bpy.context).opacity = 37
            assert abs(current_recipe()['settings']['opacity'] - 37) < 1e-5
            assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
            report = addon.unreal_report(obj.data)
            assert report['overlays'] == [(material.name, 1, external.name, True)]
            assert report['order'][:3] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV]
            close(b_bounds(), cell_bounds())
        else:
            assert addon.unreal_report(obj.data) is None


def test_stretched_cell_can_refit_and_retile():
    obj, _atlas, _material, _external = setup(stretch=True)
    before = b_bounds()
    assert before[2] * 192 > 60 and before[3] * 128 < 20, before
    assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CW') == {'FINISHED'}
    turned = b_bounds()
    assert turned[2] * 192 < 20 and turned[3] * 128 > 60, turned
    assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CCW') == {'FINISHED'}
    close(b_bounds(), before)
    addon.active_shared_patch(bpy.context).tiles = 'FACE'
    close(b_bounds(), cell_bounds())

def test_independent_cell_sizes_survive_grid_source_and_file_changes():
    saved_path = HERE / 'external_cell_grid_saved_state.blend'
    for size in (16, 32):
        for mode in ('SHARED', 'SEPARATE'):
            obj, atlas, _material, external = setup(material_mode=mode, stretch=True, cell_size=size)
            scene = bpy.context.scene
            assert scene.sprotile_tile_size == 64
            scene.sprotile_overlay_sheet = sheet()
            scene.sprotile_overlay_sheet_size = 64
            scene.sprotile_overlay_source = 'SHEET'
            original = current_recipe()
            assert original['b']['width_px'] == size
            assert bpy.ops.mesh.sprotile_pick_blend() == {'FINISHED'}
            addon.add_preset(scene, original, f'Placed {size} {mode}')
            assert addon.preset_recipe(addon.selected_preset(scene))['b']['width_px'] == size
            assert bpy.ops.mesh.sprotile_load_preset() == {'FINISHED'}
            assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
            assert current_recipe()['b']['width_px'] == size
            if size == 32 and mode == 'SHARED':
                name = obj.name
                t.leave()
                atlas.pack()
                external.pack()
                try:
                    bpy.ops.wm.save_as_mainfile(filepath=str(saved_path))
                    bpy.ops.wm.open_mainfile(filepath=str(saved_path))
                finally:
                    saved_path.unlink(missing_ok=True)
                obj = bpy.data.objects[name]
                t.select_faces(obj, [0, 1, 2, 3])
            before = b_bounds()
            assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CW') == {'FINISHED'}
            turned = b_bounds()
            close((turned[2] * 192, turned[3] * 128), (before[3] * 128, before[2] * 192))
            assert abs(turned[3] * 128 - (size - .5)) < 2e-6
            if mode == 'SHARED':
                if size == 32:
                    t.leave()
                    obj.data.sprotile_blend_patches[0].tiles = 'FACE'
                    t.select_faces(obj, [0, 1, 2, 3])
                else:
                    addon.active_shared_patch(bpy.context).tiles = 'FACE'
                close(b_bounds(), cell_bounds(size))


def test_unclassified_old_cell_keeps_its_existing_region():
    obj, _atlas, _material, _external = setup(stretch=True, cell_size=32)
    before = b_bounds()
    bm = t.bmesh.from_edit_mesh(obj.data)
    flags = bm.faces.layers.int[addon.SHARED_PATCH_FLAGS]
    for face in bm.faces:
        face[flags] &= ~addon.OVERLAY_CELL_SIZE_FLAG
    t.bmesh.update_edit_mesh(obj.data)
    assert current_recipe()['b']['width_px'] == 0
    assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CW') == {'FINISHED'}
    close(b_bounds(), before)
    addon.active_shared_patch(bpy.context).tiles = 'FACE'
    close(b_bounds(), before)


def test_failed_apply_restores_overlay_cell_metadata():
    obj, _atlas, material, external = setup(stretch=True, cell_size=16)
    before = current_recipe()
    scene = bpy.context.scene
    addon.use_cell_as_b(scene, external, 1, 1, cell_size=32)
    real_update = t.bmesh.update_edit_mesh
    updates = 0

    def fail_first_update(*args, **kwargs):
        nonlocal updates
        updates += 1
        if updates == 1:
            raise RuntimeError('Injected Apply failure')
        return real_update(*args, **kwargs)

    with patch.object(t.bmesh, 'update_edit_mesh', side_effect=fail_first_update):
        try:
            addon.apply_texture_blend(bpy.context)
        except RuntimeError as error:
            assert str(error) == 'Injected Apply failure'
        else:
            raise AssertionError('Expected the injected failure')
    assert updates == 2
    assert current_recipe()['b']['width_px'] == 16
    assert addon.recipes_match(before, current_recipe())
    assert addon.unreal_report(obj.data)['overlays'] == [(material.name, 1, external.name, True)]


def make_artist_cell_attribute(obj):
    attr = obj.data.attributes.new(addon.OVERLAY_CELL_SIZE_ATTRIBUTE, 'INT', 'FACE')
    assert attr.name == addon.OVERLAY_CELL_SIZE_ATTRIBUTE
    for index, entry in enumerate(attr.data):
        entry.value = 101 + index


def artist_cell_values(obj):
    return tuple(entry.value for entry in obj.data.attributes[addon.OVERLAY_CELL_SIZE_ATTRIBUTE].data)


def test_artist_cell_size_attribute_is_left_alone():
    for preexisting_blend in (False, True):
        obj, _bm, atlas, _material = t.setup()
        scene = bpy.context.scene
        scene.sprotile_blend_defaults.edge_style = 'PLAIN'
        scene.sprotile_blend_settings_source = 'NEW'
        if preexisting_blend:
            assert addon.apply_texture_blend(bpy.context)
        t.leave()
        make_artist_cell_attribute(obj)
        t.select_faces(obj, [0, 1])
        external = sheet()
        addon.use_cell_as_b(scene, external, 1, 1, cell_size=32)
        assert addon.apply_texture_blend(bpy.context)
        assert current_recipe()['b']['width_px'] == 32
        scene.sprotile_blend_defaults.edge_style = 'PLAIN'
        scene.sprotile_blend_settings_source = 'NEW'
        t.capture(atlas, 0, 1)
        assert addon.apply_texture_blend(bpy.context)
        addon.use_whole_image_as_b(scene, external)
        assert addon.apply_texture_blend(bpy.context)
        assert current_recipe()['b']['width_px'] == 0
        t.leave()
        assert artist_cell_values(obj) == (101, 102, 103, 104)


def test_join_recovers_owned_cell_metadata():
    for mixed in (False, True):
        obj, _atlas, material, external = setup(cell_size=16)
        t.leave()
        receiver = t.object_mesh(material, 'Join receiver')
        if mixed:
            make_artist_cell_attribute(receiver)
        obj.select_set(True)
        receiver.select_set(True)
        bpy.context.view_layer.objects.active = receiver
        assert bpy.ops.object.join() == {'FINISHED'}
        name = addon.OVERLAY_CELL_SIZE_ATTRIBUTE
        if mixed:
            artist_before = artist_cell_values(receiver)
            assert artist_before[:4] == (101, 102, 103, 104)
        else:
            assert receiver.data.attributes.get(name) is None
        t.select_faces(receiver, [4, 5])
        assert current_recipe()['b']['width_px'] == 16
        addon.use_cell_as_b(bpy.context.scene, external, 1, 1, cell_size=32)
        assert addon.apply_texture_blend(bpy.context)
        assert current_recipe()['b']['width_px'] == 32
        assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
        assert addon.apply_texture_blend(bpy.context)
        assert current_recipe()['b']['width_px'] == 32
        if mixed:
            t.select_faces(receiver, [0, 1])
            assert addon.apply_texture_blend(bpy.context)
            assert current_recipe()['b']['width_px'] == 32
        t.leave()
        if mixed:
            assert artist_cell_values(receiver) == artist_before
        else:
            assert receiver.data.attributes.get(name) is None


def test_failed_first_apply_removes_cell_metadata():
    obj, _bm, _atlas, _material = t.setup()
    addon.use_cell_as_b(bpy.context.scene, sheet(), 1, 1, cell_size=32)
    real_update = t.bmesh.update_edit_mesh
    updates = 0

    def fail_first_update(*args, **kwargs):
        nonlocal updates
        updates += 1
        if updates == 1:
            raise RuntimeError('Injected Apply failure')
        return real_update(*args, **kwargs)

    with patch.object(t.bmesh, 'update_edit_mesh', side_effect=fail_first_update):
        try:
            addon.apply_texture_blend(bpy.context)
        except RuntimeError as error:
            assert str(error) == 'Injected Apply failure'
        else:
            raise AssertionError('Expected the injected failure')
    assert updates == 2
    bm = t.bmesh.from_edit_mesh(obj.data)
    stored = addon.edit_blend_layers(obj.data, bm)
    assert stored.base is None and stored.mask is None
    assert bm.faces.layers.int.get(addon.SHARED_PATCH_FLAGS) is None


def test_cell_size_uses_both_vector_halves():
    # Exercise the entire signed Blender INT range without allocating a gigantic image.
    largest_size = 0x7fffffff
    other_size = 16
    with patch.object(addon, 'overlay_capture_cell_size', return_value=largest_size):
        obj, _atlas, _material, _external = setup(cell_size=other_size)
    assert current_recipe()['b']['width_px'] == largest_size
    bm = t.bmesh.from_edit_mesh(obj.data)
    stored = addon.edit_blend_layers(obj.data, bm)
    assert {(loop[stored.base].z, loop[stored.mask].z) for loop in bm.faces.active.loops} == {
        (0xffff, 0x7fff)}


def test_whole_png_keeps_its_region():
    _obj, _atlas, _material, external = setup(stretch=True, whole_image=True)
    assert tuple(external.size) == (CELL, CELL)
    before = b_bounds()
    close(before, (0, 0, 1, 1))
    assert bpy.ops.mesh.sprotile_turn_blend_texture(slot='B', action='CW') == {'FINISHED'}
    close(b_bounds(), before)
    addon.active_shared_patch(bpy.context).tiles = 'FACE'
    close(b_bounds(), before)


if __name__ == '__main__':
    results = []
    for test in (test_shared_and_separate_roundtrip, test_stretched_cell_can_refit_and_retile,
                 test_independent_cell_sizes_survive_grid_source_and_file_changes,
                 test_unclassified_old_cell_keeps_its_existing_region, test_failed_apply_restores_overlay_cell_metadata,
                 test_artist_cell_size_attribute_is_left_alone, test_join_recovers_owned_cell_metadata,
                 test_failed_first_apply_removes_cell_metadata, test_cell_size_uses_both_vector_halves,
                 test_whole_png_keeps_its_region):
        try:
            test()
        except Exception:
            traceback.print_exc()
            results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
        else:
            results.append(dict(name=test.__name__, passed=True))
    report = dict(results=results, passed=sum(result['passed'] for result in results), total=len(results))
    (HERE / 'external_overlay_workflows_results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    t.leave()
    addon.unregister()
    if report['passed'] != report['total']:
        raise SystemExit(1)
