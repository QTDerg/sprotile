"""Sprotile 5.13 storage: tile A in the render UV map, the Mask and each face's previous tile in corner attributes,
Edge only with grass or snow, Prepare for Unreal, and the upgrade of 5.12 files."""
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


def uv_map(mesh, name):
    data = array('f', [0.0]) * (2 * len(mesh.loops))
    mesh.uv_layers[name].data.foreach_get('uv', data)
    return [(data[2*i], data[2*i+1]) for i in range(len(mesh.loops))]


def vectors(mesh, name):
    data = array('f', [0.0]) * (3 * len(mesh.loops))
    mesh.attributes[name].data.foreach_get('vector', data)
    return [(data[3*i], data[3*i+1]) for i in range(len(mesh.loops))]


def close(a, b, tolerance=1e-6):
    return all(abs(x - y) <= tolerance for p, q in zip(a, b) for x, y in zip(p, q)) and len(a) == len(b)


def blended_faces(mesh):
    patch = mesh.attributes.get(addon.SHARED_PATCH_ID)
    previous = mesh.attributes.get(addon.BLEND_ORIGINAL_MATERIAL)
    return {p.index for p in mesh.polygons if (patch and patch.data[p.index].value > 0)
            or (previous and previous.data[p.index].value > 0)}


def setup():
    """t.setup with this suite's own defaults (scene settings persist between tests)."""
    obj, bm, atlas, mat = t.setup()
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = 'SHARED'
    defaults = scene.sprotile_blend_defaults
    defaults.edge_style = 'PLAIN'; defaults.path = False; defaults.tiles = 'PATCH'
    scene.sprotile_blend_settings_source = 'NEW'
    return obj, bm, atlas, mat


def test_layout_after_apply():
    obj, bm, atlas, mat = setup()
    t.leave()
    authored = uv_map(obj.data, 'Original UVs')
    t.select_faces(obj, [0, 1])
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    mesh = obj.data
    assert [layer.name for layer in mesh.uv_layers] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV]
    for name in (addon.BLEND_MASK_ATTRIBUTE, addon.BLEND_BASE_ATTRIBUTE):
        attribute = mesh.attributes[name]
        assert attribute.domain == 'CORNER' and attribute.data_type == 'FLOAT_VECTOR'
    assert addon.render_uv_name(mesh) == 'Original UVs' and mesh.uv_layers.active.name == 'Original UVs'
    shown, base, mask = uv_map(mesh, 'Original UVs'), vectors(mesh, addon.BLEND_BASE_ATTRIBUTE), vectors(mesh, addon.BLEND_MASK_ATTRIBUTE)
    blended = blended_faces(mesh)
    assert blended == {0, 1}
    loops_of = {p.index: list(p.loop_indices) for p in mesh.polygons}
    for face, loops in loops_of.items():
        if face in blended:
            assert close([base[i] for i in loops], [authored[i] for i in loops]), 'the base keeps the authored tile'
            assert not close([shown[i] for i in loops], [authored[i] for i in loops]), 'the render map shows A'
            assert all(-1e-6 <= v <= 1 + 1e-6 for i in loops for v in mask[i]), 'the Mask spans the patch 0..1'
        else:
            assert close([shown[i] for i in loops], [authored[i] for i in loops]), 'unblended faces are untouched'
    # The shared graph samples A from the render map ('' = render) and B from its own map.
    roles = addon.shared_nodes(mat)
    assert roles['uv_0'].uv_map == '' and roles['uv_1'].uv_map == addon.BLEND_B_UV
    return 'Apply adds B and Factor only; A replaces the authored tile in the render map, which the base attribute keeps.'


def test_remove_restores_and_reapply_keeps_base():
    obj, bm, atlas, mat = setup()
    t.leave()
    authored = uv_map(obj.data, 'Original UVs')
    bm = t.select_faces(obj, [0, 1])
    assert addon.apply_texture_blend(bpy.context)
    first_a = t.uv_values(bm, 'A')
    t.capture(atlas, 2, 3)                      # a second Apply over the same faces
    bpy.context.scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    assert t.uv_values(bm, 'A') != first_a
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    t.leave()
    assert close(uv_map(obj.data, 'Original UVs'), authored), 'Remove Blend puts back the tile from before the first blend'
    # Separate Material too.
    obj, bm, atlas, mat = setup()
    t.leave()
    authored = uv_map(obj.data, 'Original UVs')
    t.select_faces(obj, [0, 1])
    bpy.context.scene.sprotile_blend_material_mode = 'SEPARATE'
    assert addon.apply_texture_blend(bpy.context)
    nodes = addon.blend_nodes(addon.active_blend_material(bpy.context))
    assert nodes['texture_a_uv'].uv_map == '' and addon.is_mask_node(nodes['mask_uv'])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    t.leave()
    assert close(uv_map(obj.data, 'Original UVs'), authored)
    return 'Reapplying keeps the first base tile; Remove Blend restores it for shared and separate blends.'


def test_existing_uvs_reads_base():
    """Existing UVs over an already blended face reads the authored layout, as in 5.12, not tile A."""
    obj, bm, atlas, mat = setup()      # setup uses Existing UVs
    assert addon.apply_texture_blend(bpy.context)
    first_mask = t.uv_values(bm, 'MASK')
    bpy.context.scene.sprotile_blend_settings_source = 'NEW'
    assert addon.apply_texture_blend(bpy.context)
    assert close([uv for f in t.uv_values(bm, 'MASK') for uv in f], [uv for f in first_mask for uv in f])
    return 'Reapplying with Existing UVs gives the same Mask: the authored layout is read from the base attribute.'


def test_edge_only_with_grass_and_prepare():
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    mesh = obj.data
    assert addon.SHARED_EDGE_UV not in mesh.uv_layers
    bpy.ops.object.select_all(action='DESELECT'); obj.select_set(True); bpy.context.view_layer.objects.active = obj
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert [l.name for l in mesh.uv_layers] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV]
    # Grass adds Edge; turning the patch back to Straight leaves an all-zero Edge that Prepare drops.
    t.select_faces(obj, [2, 3]); t.capture(atlas, 2, 3)
    scene.sprotile_blend_settings_source = 'NEW'; scene.sprotile_blend_defaults.edge_style = 'GRASS'
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    assert addon.SHARED_EDGE_UV in mesh.uv_layers and addon.unreal_report(mesh)['shaped']
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert [l.name for l in mesh.uv_layers][3] == addon.SHARED_EDGE_UV
    bpy.ops.object.mode_set(mode='EDIT'); t.select_faces(obj, [2, 3])
    addon.active_shared_patch(bpy.context).edge_style = 'PLAIN'
    t.leave()
    assert not addon.edge_in_use(mesh)
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert addon.SHARED_EDGE_UV not in mesh.uv_layers
    # With every blend removed, Prepare strips the leftover maps so the mesh exports as a plain mesh.
    bpy.ops.object.mode_set(mode='EDIT')
    t.select_faces(obj, [0, 1, 2, 3])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    t.leave()
    assert addon.unreal_report(mesh)['leftover']
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert [l.name for l in mesh.uv_layers] == ['Original UVs'] and addon.unreal_report(mesh) is None
    return 'Edge only exists while grass or snow uses it; a mesh without blends left exports with its own maps only.'


def test_unreal_report_in_edit_mode():
    """The Unreal Export panel reads the edit BMesh in Edit Mode (Mesh arrays are empty there; 5.12 raised)."""
    obj, bm, atlas, mat = setup()
    scene = bpy.context.scene
    assert addon.apply_texture_blend(bpy.context)
    t.select_faces(obj, [2, 3]); t.capture(atlas, 2, 3)
    scene.sprotile_blend_settings_source = 'NEW'; scene.sprotile_blend_defaults.edge_style = 'GRASS'
    assert addon.apply_texture_blend(bpy.context)
    assert obj.data.is_editmode
    editing = addon.unreal_report(obj.data)
    t.leave()
    assert editing == addon.unreal_report(obj.data), (editing, addon.unreal_report(obj.data))
    assert editing['shaped'] and addon.blended_face_count(obj.data) == 4
    bpy.ops.object.mode_set(mode='EDIT'); t.select_faces(obj, [0, 1, 2, 3])
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    assert addon.unreal_report(obj.data)['leftover'], 'Edit Mode sees the removal at once'
    t.leave()
    return 'The Unreal report gives the same answer in Edit and Object Mode, including right after Remove Blend.'


def test_fbx_leaves_out_tool_data():
    obj, bm, atlas, mat = setup()
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    path = os.path.join(tempfile.mkdtemp(), 'compact.fbx')
    bpy.ops.object.select_all(action='DESELECT'); obj.select_set(True); bpy.context.view_layer.objects.active = obj
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    expected = uv_map(obj.data, 'Original UVs')
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, add_leaf_bones=False, bake_anim=False)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path)
    imported = next(o for o in bpy.data.objects if o not in before and o.type == 'MESH')
    names = [l.name for l in imported.data.uv_layers]
    assert names == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV], names
    assert not any(n in imported.data.attributes for n in (addon.BLEND_MASK_ATTRIBUTE, addon.BLEND_BASE_ATTRIBUTE))
    # UV0 carries tile A on blended faces: compare as sets of corner values (FBX may reorder corners).
    got = sorted((round(u, 5), round(v, 5)) for u, v in uv_map(imported.data, 'Original UVs'))
    assert got == sorted((round(u, 5), round(v, 5)) for u, v in expected)
    bpy.data.objects.remove(imported)
    return 'The FBX carries UV0 (with A), B and Factor; the Mask and base attributes stay in Blender.'


def downgrade_to_512(obj, mat):
    """Rewrite a 5.13 blend into the 5.12 layout: A and the Mask back in UV maps, the authored tile back in UV0."""
    mesh = obj.data
    blended = blended_faces(mesh)
    shown, base, mask = uv_map(mesh, 'Original UVs'), vectors(mesh, addon.BLEND_BASE_ATTRIBUTE), vectors(mesh, addon.BLEND_MASK_ATTRIBUTE)
    authored = list(shown)
    for p in mesh.polygons:
        if p.index in blended:
            for i in p.loop_indices:
                authored[i] = base[i]
    for name, values in (('SprotileBlend_A', shown), ('SprotileBlend_Mask', mask)):
        mesh.uv_layers.new(name=name, do_init=False)
        mesh.uv_layers[name].data.foreach_set('uv', [v for uv in values for v in uv])
    mesh.uv_layers['Original UVs'].data.foreach_set('uv', [v for uv in authored for v in uv])
    for name in (addon.BLEND_MASK_ATTRIBUTE, addon.BLEND_BASE_ATTRIBUTE):
        mesh.attributes.remove(mesh.attributes[name])
    mesh.uv_layers['Original UVs'].active_render = True
    if mat is not None:
        addon.shared_nodes(mat)['uv_0'].uv_map = 'SprotileBlend_A'
    mesh.update()
    return shown, mask, authored


def test_upgrade_512_shared():
    obj, bm, atlas, mat = setup()
    assert addon.apply_texture_blend(bpy.context)
    recipe = addon.read_blend_recipe(bpy.context)
    t.leave()
    shown, mask, authored = downgrade_to_512(obj, mat)
    assert addon.mesh_is_legacy(obj.data)
    del obj.data[addon.BLEND_MESH_TAG]  # A joined 5.12 mesh can retain the original shader and layers alone.
    report = addon.unreal_report(obj.data)
    assert report['legacy'] and not report['ordered']
    assert addon.upgrade_file_blends() == 1
    mesh = obj.data
    assert [l.name for l in mesh.uv_layers] == ['Original UVs', addon.BLEND_B_UV, addon.SHARED_FACTOR_UV]
    assert close(uv_map(mesh, 'Original UVs'), shown) and close(vectors(mesh, addon.BLEND_MASK_ATTRIBUTE), mask)
    assert close(vectors(mesh, addon.BLEND_BASE_ATTRIBUTE), authored)
    assert addon.shared_nodes(mat)['uv_0'].uv_map == ''
    assert addon.upgrade_file_blends() == 0, 'the upgrade runs once'
    # The upgraded blend reads back and edits live.
    bpy.ops.object.mode_set(mode='EDIT'); bm = t.select_faces(obj, [0, 1])
    assert addon.recipes_match(recipe, addon.read_blend_recipe(bpy.context))
    before = t.uv_values(bm, addon.SHARED_FACTOR_UV)
    addon.active_shared_patch(bpy.context).position = .3
    assert t.uv_values(bm, addon.SHARED_FACTOR_UV) != before
    assert bpy.ops.mesh.sprotile_remove_blend() == {'FINISHED'}
    t.leave()
    assert close(uv_map(obj.data, 'Original UVs'), authored)
    return 'A 5.12 shared blend upgrades in place: same pixels, same recipe, live controls and Remove Blend.'


def test_upgrade_512_separate_and_edit_mode():
    obj, bm, atlas, mat = setup()
    bpy.context.scene.sprotile_blend_material_mode = 'SEPARATE'
    assert addon.apply_texture_blend(bpy.context)
    material = addon.active_blend_material(bpy.context)
    recipe = addon.read_blend_recipe(bpy.context)
    t.leave()
    downgrade_to_512(obj, None)
    nodes = addon.blend_nodes(material)
    nodes['texture_a_uv'].uv_map = 'SprotileBlend_A'
    old_mask = nodes['mask_uv']
    tree = material.node_tree
    legacy = tree.nodes.new('ShaderNodeUVMap'); legacy.uv_map = 'SprotileBlend_Mask'; legacy['sprotile_blend_role'] = 'mask_uv'
    for link in list(old_mask.outputs['Vector'].links):
        tree.links.new(legacy.outputs[0], link.to_socket)
    tree.nodes.remove(old_mask)
    bpy.ops.object.mode_set(mode='EDIT')                 # upgrade while the mesh is in Edit Mode
    assert addon.upgrade_file_blends() == 1
    nodes = addon.blend_nodes(material)
    assert nodes['texture_a_uv'].uv_map == '' and addon.is_mask_node(nodes['mask_uv'])
    assert nodes['mask_axis'].inputs[0].links[0].from_socket == nodes['mask_uv'].outputs['Vector']
    t.select_faces(obj, [0, 1])
    assert addon.recipes_match(recipe, addon.read_blend_recipe(bpy.context))
    t.leave()
    assert [l.name for l in obj.data.uv_layers] == ['Original UVs', addon.BLEND_B_UV]
    return 'A 5.12 Separate Material blend upgrades in Edit Mode, including its mask node, and still copies exactly.'


def test_user_map_named_like_512_is_left_alone():
    obj, bm, atlas, mat = setup()
    t.leave()
    obj.data.uv_layers.new(name='SprotileBlend_A')
    assert not addon.mesh_is_legacy(obj.data) and addon.upgrade_file_blends() == 0
    assert 'SprotileBlend_A' in obj.data.uv_layers
    return 'A user map that only shares a 5.12 name is not treated as a blend.'


def test_compact_mesh_with_user_map_named_like_512():
    obj, bm, atlas, mat = setup()
    assert addon.apply_texture_blend(bpy.context)
    t.leave()
    mesh = obj.data
    base = vectors(mesh, addon.BLEND_BASE_ATTRIBUTE)
    mesh.uv_layers.new(name=addon.LEGACY_A_UV).data[0].uv = (.125, .875)
    assert not addon.mesh_is_legacy(mesh) and addon.upgrade_file_blends() == 0
    assert addon.LEGACY_A_UV in addon.unreal_report(mesh)['order']
    assert bpy.ops.mesh.sprotile_prepare_unreal() == {'FINISHED'}
    assert tuple(mesh.uv_layers[addon.LEGACY_A_UV].data[0].uv) == (.125, .875)
    assert vectors(mesh, addon.BLEND_BASE_ATTRIBUTE) == base


def test_atlas_must_read_render_map():
    obj, bm, atlas, mat = setup()
    other = obj.data.uv_layers.new(name='Other map')
    obj.data.uv_layers['Original UVs'].active_render = True
    texture = next(n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE')
    uv = mat.node_tree.nodes.new('ShaderNodeUVMap'); uv.uv_map = 'Other map'
    mat.node_tree.links.new(uv.outputs[0], texture.inputs['Vector'])
    before = t.graph_state(mat)
    try:
        addon.apply_texture_blend(bpy.context)
    except ValueError as error:
        assert 'render UV map' in str(error), error
    else:
        raise AssertionError('An atlas that reads another map must be refused')
    assert t.graph_state(mat) == before
    uv.uv_map = ''
    assert addon.apply_texture_blend(bpy.context)
    return 'Shared blends refuse an atlas that samples a non-render map, so A always lands where it is drawn.'


results = []
for test in (test_layout_after_apply, test_remove_restores_and_reapply_keeps_base, test_existing_uvs_reads_base,
             test_edge_only_with_grass_and_prepare, test_unreal_report_in_edit_mode, test_fbx_leaves_out_tool_data, test_upgrade_512_shared,
             test_upgrade_512_separate_and_edit_mode, test_user_map_named_like_512_is_left_alone,
             test_atlas_must_read_render_map, test_compact_mesh_with_user_map_named_like_512):
    try:
        detail = test()
    except Exception:
        traceback.print_exc()
        results.append(dict(name=test.__name__, passed=False, traceback=traceback.format_exc()))
    else:
        results.append(dict(name=test.__name__, passed=True, detail=detail))
        print('PASS', test.__name__, detail)
report = dict(results=results, passed=sum(r['passed'] for r in results), total=len(results))
(t.HERE/'compact_storage_results.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
t.leave(); addon.unregister()
if report['passed'] != report['total']:
    raise SystemExit(1)
