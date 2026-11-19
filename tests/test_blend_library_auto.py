"""Applied blends join the preset library; Use One Atlas repairs a shared material that samples two atlases."""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shared_blend import addon, bpy, setup, leave, capture, select_faces


def apply_blend(atlas, faces, b):
    scene = bpy.context.scene
    obj = bpy.context.active_object
    capture(atlas, 0, b)
    scene.sprotile_blend_settings_source = 'NEW'
    select_faces(obj, faces)
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}


def test_auto_presets():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    assert scene.sprotile_auto_presets is True
    assert len(scene.sprotile_blend_presets) == 0
    apply_blend(atlas, [0, 1], 1)
    assert len(scene.sprotile_blend_presets) == 1, len(scene.sprotile_blend_presets)
    apply_blend(atlas, [2, 3], 1)
    assert len(scene.sprotile_blend_presets) == 1, 'The same recipe must not be added twice'
    apply_blend(atlas, [2, 3], 2)
    assert len(scene.sprotile_blend_presets) == 2, 'A different B tile is a new recipe'
    assert len({p.name for p in scene.sprotile_blend_presets}) == 2


def test_auto_presets_off():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    scene.sprotile_blend_presets.clear()
    scene.sprotile_auto_presets = False
    apply_blend(atlas, [0, 1], 1)
    assert len(scene.sprotile_blend_presets) == 0
    scene.sprotile_auto_presets = True


def test_whole_image_and_single_cell_have_distinct_editable_presets():
    obj, _bm, atlas, _material = setup()
    scene = bpy.context.scene
    scene.sprotile_blend_presets.clear()
    scene.sprotile_blend_task = 'OVERLAY'
    scene.sprotile_use_uv_inset = False
    image = bpy.data.images.new('One decal', 16, 16, alpha=True)
    select_faces(obj, [0, 1])
    addon.use_whole_image_as_b(scene, image)
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    assert len(scene.sprotile_blend_presets) == 1
    addon.use_cell_as_b(scene, image, 0, 0, cell_size=16)
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    assert len(scene.sprotile_blend_presets) == 2
    presets = [addon.preset_recipe(p) for p in scene.sprotile_blend_presets]
    assert addon.recipes_match(*presets) and [p['b']['width_px'] for p in presets] == [0, 16]
    assert addon.ensure_preset_for_recipe(scene, presets[1], 'same cell again') is None


def shared_material_after_apply():
    obj, bm, atlas, mat = setup()
    apply_blend(atlas, [0, 1], 1)
    leave()
    material = next(slot.material for slot in obj.material_slots if slot.material.get(addon.SHARED_MATERIAL_TAG))
    return obj, atlas, material


def second_atlas(name, width=256, height=64):
    image = bpy.data.images.new(name, width=width, height=height)
    image.colorspace_settings.name = 'Non-Color'
    return image


def test_use_one_atlas():
    obj, atlas, material = shared_material_after_apply()
    scene = bpy.context.scene
    assert addon.shared_atlas_mismatch(material) == set()
    other = second_atlas('Four tiles copy')
    roles = addon.shared_nodes(material)
    roles['texture_a'].image = other
    assert addon.shared_atlas_mismatch(material) == {atlas.name, other.name}
    try:
        addon.shared_atlas_node(material, atlas)
    except ValueError as error:
        assert atlas.name in str(error) and other.name in str(error) and 'Use One Atlas' in str(error), str(error)
    else:
        raise AssertionError('A mixed atlas must be refused')
    scene.sprotile_blend_b.image = other
    assert bpy.ops.mesh.sprotile_use_one_atlas() == {'FINISHED'}
    assert addon.shared_atlas_mismatch(material) == set()
    assert scene.sprotile_blend_b.image == atlas
    assert addon.shared_atlas_node(material, atlas).as_pointer() == roles['original'].as_pointer()


def test_use_one_atlas_refuses_other_size():
    obj, atlas, material = shared_material_after_apply()
    other = second_atlas('Small atlas', 128, 64)
    roles = addon.shared_nodes(material)
    roles['texture_b'].image = other
    assert bpy.ops.mesh.sprotile_use_one_atlas() == {'CANCELLED'}
    assert roles['texture_b'].image == other, 'Nothing may change when sizes differ'
    assert addon.shared_atlas_mismatch(material) == {atlas.name, other.name}


results = []
for test in (test_auto_presets, test_auto_presets_off, test_use_one_atlas, test_use_one_atlas_refuses_other_size,
             test_whole_image_and_single_cell_have_distinct_editable_presets):
    try:
        test()
    except Exception:
        traceback.print_exc()
        results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
    else:
        results.append(dict(name=test.__name__, passed=True))
report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
Path(__file__).with_name('blend_library_auto_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
leave()
addon.unregister()
if report['passed'] != report['total']:
    raise SystemExit(1)
