"""First tile from an empty mesh, material reuse, existing faces and failed setup."""
from pathlib import Path
import json
import sys
import tempfile
import traceback
from unittest.mock import patch

import bmesh
import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sprotile_loader

addon = sprotile_loader.load('sprotile_atlas_setup_test')
addon.register()


def strip():
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    mesh = bpy.data.meshes.new('Four panels')
    mesh.from_pydata([(x, y, 0) for y in (0, 1) for x in range(5)], [],
                     [(x, x+1, x+6, x+5) for x in range(4)])
    obj = bpy.data.objects.new('Four panels', mesh)
    bpy.context.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    return obj


def choose(obj, *indices):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for face in bm.faces:
        face.select_set(face.index in indices)
    bm.faces.active = bm.faces[indices[0]] if indices else None
    bmesh.update_edit_mesh(obj.data)
    return bm


def test_first_tile(path):
    obj = strip()
    bm = choose(obj, 1)
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'FINISHED'}
    image = addon.resolve_atlas_image(obj)
    assert image and tuple(image.size) == (128, 64)
    slot = bm.faces[1].material_index
    assert slot != 0 and obj.data.materials[0] is None
    assert [f.material_index for f in bm.faces] == [0, slot, 0, 0]
    node = addon.atlas_node_from_material(obj.data.materials[slot])
    assert node.image == image and node.interpolation == 'Closest'
    assert node.outputs['Color'].links[0].to_socket.name == 'Base Color'
    scene = bpy.context.scene
    scene.sprotile_tile_size = 64
    scene.sprotile_use_uv_inset = False
    scene.sprotile_active_col, scene.sprotile_active_row = 1, 0
    assert bpy.ops.mesh.sprotile_map_selected(mapping_mode='PER_FACE') == {'FINISHED'}
    layer = bm.loops.layers.uv.active
    assert sorted(tuple(loop[layer].uv) for loop in bm.faces[1].loops) == [
        (.5, 0.), (.5, 1.), (1., 0.), (1., 1.)]
    assert bpy.ops.mesh.sprotile_capture_blend_tile(slot='B') == {'FINISHED'}
    scene.sprotile_active_col = 0
    assert bpy.ops.mesh.sprotile_capture_blend_tile(slot='A') == {'FINISHED'}
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    assert addon.read_blend_recipe(bpy.context)['b']['image'] == image


def test_repeat_mixed_selection(path):
    obj = strip()
    plain = bpy.data.materials.new('Artist material')
    obj.data.materials.append(plain)
    bm = choose(obj, 0)
    bm.loops.layers.uv.new('Artist UV')
    uv = bm.loops.layers.uv.active
    for f in bm.faces:
        for loop in f.loops:
            loop[uv].uv = (.1, .7)
    before = [tuple(loop[uv].uv) for f in bm.faces for loop in f.loops]
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'FINISHED'}
    material = obj.data.materials[bm.faces[0].material_index]
    choose(obj, 0, 2)
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'FINISHED'}
    assert len(obj.data.materials) == 2 and obj.data.materials[0] == plain
    assert [f.material_index for f in bm.faces] == [1, 0, 1, 0]
    assert obj.data.materials[1] == material and bm.faces.active.index == 2
    assert before == [tuple(loop[uv].uv) for f in bm.faces for loop in f.loops]
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'CANCELLED'}


def test_failed_setup(path):
    obj = strip()
    bm = choose(obj, 1)
    images, materials = len(bpy.data.images), len(bpy.data.materials)
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path.with_suffix('.missing'))) == {'CANCELLED'}
    assert not len(obj.data.materials) and not len(bm.loops.layers.uv)
    original_update = bmesh.update_edit_mesh
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('test update failure')
        return original_update(*args, **kwargs)
    with patch.object(bmesh, 'update_edit_mesh', fail_once):
        assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'CANCELLED'}
    assert len(calls) == 2 and not len(obj.data.materials) and not len(bm.loops.layers.uv)
    assert bm.faces.active.index == 1 and all(f.material_index == 0 for f in bm.faces)
    assert images == len(bpy.data.images) and materials == len(bpy.data.materials)
    choose(obj)
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'CANCELLED'}


def test_setup_undo_redo(path):
    obj = strip()
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.ed.undo_push(message='New mesh')
    bpy.ops.object.mode_set('EXEC_DEFAULT', True, mode='EDIT')
    choose(obj, 2)
    bpy.ops.ed.undo_push(message='Select one face')
    assert bpy.ops.mesh.sprotile_setup_atlas('EXEC_DEFAULT', True, filepath=str(path)) == {'FINISHED'}
    assert bpy.ops.ed.undo() == {'FINISHED'}
    mesh = bpy.context.active_object.data
    assert not len(mesh.materials) and not len(mesh.uv_layers)
    assert bpy.ops.ed.redo() == {'FINISHED'}
    obj = bpy.context.active_object
    bm = bmesh.from_edit_mesh(obj.data)
    assert [face.material_index for face in bm.faces] == [0, 0, 1, 0]
    assert len(bm.loops.layers.uv) == 1 and tuple(addon.resolve_atlas_image(obj).size) == (128, 64)


def test_linked_instances_keep_overrides(path):
    obj = strip()
    bpy.ops.object.mode_set(mode='OBJECT')
    plain = bpy.data.materials.new('Original material')
    obj.data.materials.append(plain)
    instance = obj.copy()
    bpy.context.collection.objects.link(instance)
    instance.material_slots[0].link = 'OBJECT'
    override = bpy.data.materials.new('Other instance material')
    instance.material_slots[0].material = override
    instance.select_set(False)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = choose(obj, 0)
    assert bpy.ops.mesh.sprotile_setup_atlas(filepath=str(path)) == {'CANCELLED'}
    assert instance.material_slots[0].material == override and obj.data.materials[0] == plain
    assert len(obj.data.materials) == 1 and not len(bm.loops.layers.uv)


if __name__ == '__main__':
    results = []
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'Two tiles.png'
        sample = bpy.data.images.new('Two tiles', 128, 64)
        sample.pixels.foreach_set([v for y in range(64) for x in range(128)
                                   for v in ((.2, .7, .3, 1.) if x < 64 else (.6, .3, .1, 1.))])
        sample.filepath_raw = str(path)
        sample.file_format = 'PNG'
        sample.save()
        bpy.data.images.remove(sample)
        for test in (test_failed_setup, test_first_tile, test_repeat_mixed_selection, test_setup_undo_redo,
                     test_linked_instances_keep_overrides):
            try:
                test(path)
            except Exception:
                traceback.print_exc()
                results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
            else:
                results.append(dict(name=test.__name__, passed=True))
    report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
    (HERE / 'atlas_setup_results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    addon.unregister()
    if report['passed'] != report['total']:
        raise SystemExit(1)
