"""Shared shader/patch integration tests using real Blender data and failure injection."""
from pathlib import Path
import importlib.util
import json
import sys
import traceback
from unittest.mock import patch
import bpy
import bmesh

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprotile_loader
addon = sprotile_loader.load('sprotile_shared_tests')
addon.register()
RESULTS = []


def leave():
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def image():
    result = bpy.data.images.new('Four tiles', width=256, height=64)
    result.colorspace_settings.name = 'Non-Color'
    colors = [(0.8, .02, .02, 1), (.02, .8, .02, 1), (.02, .02, .8, 1), (.8, .8, .02, 1)]
    result.pixels.foreach_set([v for y in range(64) for x in range(256) for v in colors[x // 64]])
    return result


def material(atlas):
    mat = bpy.data.materials.new('Original PS1 atlas')
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    tex = nodes.new('ShaderNodeTexImage'); tex.image = atlas; tex.interpolation = 'Closest'
    tint = nodes.new('ShaderNodeVertexColor'); tint.layer_name = 'Tint'
    mul = nodes.new('ShaderNodeMixRGB'); mul.blend_type = 'MULTIPLY'; mul.inputs[0].default_value = 1
    double = nodes.new('ShaderNodeMixRGB'); double.blend_type = 'ADD'; double.inputs[0].default_value = 1
    emit = nodes.new('ShaderNodeEmission'); out = nodes.new('ShaderNodeOutputMaterial')
    links.new(tex.outputs['Color'], mul.inputs[1]); links.new(tint.outputs['Color'], mul.inputs[2])
    links.new(mul.outputs[0], double.inputs[1]); links.new(mul.outputs[0], double.inputs[2])
    links.new(double.outputs[0], emit.inputs[0]); links.new(emit.outputs[0], out.inputs[0])
    nodes.active = tex
    mat['ps1_spyro_atlas'] = True
    return mat


def object_mesh(mat, name='Two patches'):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0,0,0),(1,0,0),(2,0,0),(0,1,0),(1,1,0),(2,1,0)], [],
                     [(0,1,4),(0,4,3),(1,2,5),(1,5,4)])
    obj = bpy.data.objects.new(name, mesh); bpy.context.collection.objects.link(obj)
    mesh.materials.append(mat)
    uv = mesh.uv_layers.new(name='Original UVs')
    for poly in mesh.polygons:
        for index in poly.loop_indices:
            co = mesh.vertices[mesh.loops[index].vertex_index].co
            uv.data[index].uv = (.30 + .02 * co.x, .1 + .8 * co.y)
    tint = mesh.color_attributes.new(name='Tint', type='FLOAT_COLOR', domain='CORNER')
    for entry in tint.data:
        entry.color = (.25, .45, .6, .37)
    return obj


def capture(atlas, a=0, b=1):
    scene = bpy.context.scene
    scene.sprotile_tile_size = 64; scene.sprotile_tile_mode = 'FULL'
    scene.sprotile_active_row = 0; scene.sprotile_rotation = '0'
    scene.sprotile_flip_u = scene.sprotile_flip_v = False
    scene.sprotile_use_uv_inset = True; scene.sprotile_uv_inset_px = .25
    scene.sprotile_active_col = a; addon.capture_blend_tile(scene, 'A', atlas)
    scene.sprotile_active_col = b; addon.capture_blend_tile(scene, 'B', atlas)


def select_faces(obj, indices):
    if obj.mode != 'EDIT':
        leave(); bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True); bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data); bm.faces.ensure_lookup_table()
    for face in bm.faces: face.select_set(face.index in indices)
    bm.faces.active = bm.faces[indices[0]] if indices else None
    return bm


def setup():
    leave(); bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
    atlas = image(); mat = material(atlas); obj = object_mesh(mat)
    bm = select_faces(obj, [0, 1]); capture(atlas)
    scene = bpy.context.scene
    scene.sprotile_blend_material_mode = 'SHARED'; scene.sprotile_patch_projection = 'EXISTING'
    scene.sprotile_patch_fit = 'FILL'
    controls = scene.sprotile_blend_defaults
    controls.direction = 'LEFT_RIGHT'; controls.position = .5; controls.width = .6; controls.smooth = True
    return obj, bm, atlas, mat


# Where a placed blend's layouts live (5.13): 'A' the render UV map, 'B' its UV map, 'MASK' and 'BASE' corner
# attributes. uv_values accepts these role names as well as UV map names.
BLEND_ROLES = ('A', 'B', 'MASK')


def blend_layer(bm, name):
    if name in ('A', 'B', 'MASK', 'BASE'):
        mesh = next((m for m in bpy.data.meshes if m.is_editmode and bmesh.from_edit_mesh(m) is bm), None)
        stored = addon.edit_blend_layers(mesh, bm)
        return {'A': stored.a, 'B': stored.b, 'MASK': stored.mask, 'BASE': stored.base}[name]
    return bm.loops.layers.uv.get(name)


def uv_values(bm, name):
    layer = blend_layer(bm, name)
    def read(loop):   # a UV map, or a 5.13 vector attribute (also while a test swaps in an old add-on module)
        item = loop[layer]
        return item.uv if hasattr(item, 'uv') else item.xy
    return None if layer is None else [[tuple(read(loop)) for loop in face.loops] for face in bm.faces]


def authored_values(bm, name):
    """A user UV map as authored. Since 5.13 a blended face's render map shows tile A and the base attribute keeps
    the coordinates it replaced, so for those faces read the base attribute."""
    values = uv_values(bm, name)
    mesh = next((m for m in bpy.data.meshes if m.is_editmode and bmesh.from_edit_mesh(m) is bm), None)
    base = blend_layer(bm, 'BASE')
    if values is None or base is None or name != addon.render_uv_name(mesh):
        return values
    patch = bm.faces.layers.int.get(addon.SHARED_PATCH_ID)
    previous = bm.faces.layers.int.get(addon.BLEND_ORIGINAL_MATERIAL)
    return [[tuple(loop[base].xy) for loop in face.loops] if addon.face_is_blended(face, patch, previous) else row
            for face, row in zip(bm.faces, values)]


def mesh_state(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    tint = bm.loops.layers.float_color.get('Tint') or bm.loops.layers.color.get('Tint')
    return dict(uv=authored_values(bm, 'Original UVs'), tint=[[tuple(loop[tint]) for loop in f.loops] for f in bm.faces],
                slots=tuple(obj.data.materials), geometry=[tuple(v.co) for v in bm.verts],
                selection=[(f.select, f.hide) for f in bm.faces], active=bm.faces.active.index)


def graph_state(mat):
    return (tuple(n.name for n in mat.node_tree.nodes),
            tuple(sorted((l.from_node.name,l.from_socket.name,l.to_node.name,l.to_socket.name) for l in mat.node_tree.links)),
            dict(mat.items()))


def test_preservation():
    obj,bm,atlas,mat = setup(); before=mesh_state(obj); count=len(bpy.data.materials)
    nodes={n.name for n in mat.node_tree.nodes}
    assert addon.apply_texture_blend(bpy.context)
    assert mesh_state(obj)==before
    assert len(bpy.data.materials)==count and len(obj.data.materials)==1 and obj.data.materials[0] is mat
    assert all(f.material_index==0 for f in bm.faces)
    assert nodes.issubset({n.name for n in mat.node_tree.nodes})
    assert addon.image_from_material(mat) is atlas
    assert all(uv==(0,0) for face in uv_values(bm,addon.SHARED_FACTOR_UV)[2:] for uv in face)
    assert addon.active_shared_patch(bpy.context) is not None
    return 'Original material identity/slot, nodes, Tint RGBA, geometry, selection and authored UVs retained.'


def two_patches():
    obj,bm,atlas,mat=setup(); assert addon.apply_texture_blend(bpy.context)
    first=addon.active_shared_patch(bpy.context); first.position=.25
    capture(atlas,2,3); select_faces(obj,[2,3])
    bpy.context.scene.sprotile_blend_defaults.position=.75
    assert addon.apply_texture_blend(bpy.context)
    return obj,bm,atlas,mat,first,addon.active_shared_patch(bpy.context)


def test_independent():
    obj,bm,atlas,mat,first,second=two_patches()
    assert first.patch_id!=second.patch_id and len(obj.data.materials)==1
    assert abs(first.position-.25)<1e-6 and abs(second.position-.75)<1e-6
    before=uv_values(bm,addon.SHARED_FACTOR_UV)
    first.direction='RIGHT_LEFT';first.width=.15;first.smooth=False
    after=uv_values(bm,addon.SHARED_FACTOR_UV)
    assert before[:2]!=after[:2] and before[2:]==after[2:]
    a=uv_values(bm,BLEND_ROLES[0]);b=uv_values(bm,BLEND_ROLES[1])
    assert max(uv[0] for f in a[:2] for uv in f)<.25
    assert min(uv[0] for f in a[2:] for uv in f)>.5
    assert min(uv[0] for f in b[2:] for uv in f)>.75
    return 'Different pairs, directions, widths, smoothing and positions coexist in one material.'


def test_reapply_subset_remove():
    obj,bm,atlas,mat,first,second=two_patches(); node_count=len(mat.node_tree.nodes)
    select_faces(obj,[0]); assert addon.apply_texture_blend(bpy.context)
    assert len(obj.data.materials)==1 and len(mat.node_tree.nodes)==node_count
    patch=bm.faces.layers.int[addon.SHARED_PATCH_ID]
    assert bm.faces[0][patch]!=bm.faces[1][patch]
    other=uv_values(bm,addon.SHARED_FACTOR_UV)[1:]
    assert bpy.ops.mesh.sprotile_remove_blend()=={'FINISHED'}
    assert bm.faces[0][patch]==0 and uv_values(bm,addon.SHARED_FACTOR_UV)[1:]==other
    first.position=.8
    assert uv_values(bm,addon.SHARED_FACTOR_UV)[0]==[(0,0)]*3
    return 'Reapply does not add slots/nodes. Removing part of a patch stays removed when old controls change.'


def test_bad_targets():
    for kind in ('different_images','reserved','missing_material','object_override','bad_patch_type'):
        obj,bm,atlas,mat=setup(); before=mesh_state(obj); graph=graph_state(mat)
        if kind=='different_images':addon.capture_blend_tile(bpy.context.scene,'B',image())
        elif kind=='reserved':bm.loops.layers.uv.new(addon.SHARED_FACTOR_UV)
        elif kind=='missing_material':obj.data.materials[0]=None
        elif kind=='object_override':obj.material_slots[0].link='OBJECT'
        else:bm.faces.layers.float.new(addon.SHARED_PATCH_ID)
        layers=list(bm.loops.layers.uv.keys())
        assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'},kind
        assert list(bm.loops.layers.uv.keys())==layers and graph_state(mat)==graph,kind
    return 'Different images, reserved names/types, missing source and object overrides cancel before shader changes.'


def multiple_objects():
    obj,bm,atlas,mat=setup();leave()
    second=object_mesh(mat,'Other edited mesh')
    obj.select_set(True);second.select_set(True);bpy.context.view_layer.objects.active=obj
    bpy.ops.object.mode_set(mode='EDIT')
    for item in (obj,second):
        mb=bmesh.from_edit_mesh(item.data);mb.faces.ensure_lookup_table()
        for f in mb.faces:f.select_set(f.index<2)
        mb.faces.active=mb.faces[0]
    return obj,second,atlas,mat


def test_atomic_write():
    first,second,atlas,mat=multiple_objects()
    states=[mesh_state(o) for o in (first,second)];graph=graph_state(mat)
    real=bmesh.update_edit_mesh;calls=[0]
    def fail_second(*args,**kwargs):
        calls[0]+=1
        if calls[0]==2:raise RuntimeError('Injected second write')
        return real(*args,**kwargs)
    with patch.object(bmesh,'update_edit_mesh',side_effect=fail_second):
        assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert [mesh_state(o) for o in (first,second)]==states
    assert graph_state(mat)==graph
    for obj in (first,second):
        mb=bmesh.from_edit_mesh(obj.data)
        assert list(mb.loops.layers.uv.keys())==['Original UVs']
        assert not mb.faces.layers.int.get(addon.SHARED_PATCH_ID)
        assert len(obj.data.sprotile_blend_patches)==0
    return 'Injected second-mesh failure rolls back all meshes, new records, layers and the original material graph.'


def test_atomic_graph():
    first,second,atlas,mat=multiple_objects();leave()
    second_mat=mat.copy();second.data.materials[0]=second_mat
    bpy.ops.object.mode_set(mode='EDIT')
    states=[mesh_state(o) for o in (first,second)];graphs=[graph_state(m) for m in (mat,second_mat)]
    real=addon.upgrade_shared_material;calls=[0]
    def fail_second(*args):
        calls[0]+=1
        if calls[0]==2:raise MemoryError('Injected second shader')
        return real(*args)
    with patch.object(addon,'upgrade_shared_material',side_effect=fail_second):
        assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert [mesh_state(o) for o in (first,second)]==states
    assert [graph_state(m) for m in (mat,second_mat)]==graphs
    return 'Failure upgrading the second source material restores the first source graph before allocating UVs.'


def test_migration():
    obj,bm,atlas,mat=setup();scene=bpy.context.scene
    scene.sprotile_blend_material_mode='SEPARATE';assert addon.apply_texture_blend(bpy.context)
    old=obj.data.materials[bm.faces[0].material_index]
    assert old.get('sprotile_blend_material')
    scene.sprotile_blend_material_mode='SHARED';assert addon.apply_texture_blend(bpy.context)
    assert bm.faces[0].material_index==0 and obj.data.materials[0] is mat
    assert len(obj.data.materials)==2 and obj.data.materials[1] is old
    assert bpy.ops.mesh.sprotile_remove_blend()=={'FINISHED'}
    assert bm.faces[0].material_index==0
    return '5.4 separate blends migrate selected faces onto their recorded original atlas; other material slots stay intact.'


def test_methods_and_record_updates():
    for method in ('SURFACE','UNFOLD','EXISTING'):
        for fit in ('FILL','KEEP_SHAPE'):
            obj,bm,atlas,mat=setup();scene=bpy.context.scene
            scene.sprotile_patch_projection=method;scene.sprotile_patch_fit=fit
            assert addon.apply_texture_blend(bpy.context)
            controls=addon.active_shared_patch(bpy.context)
            original=uv_values(bm,'Original UVs')
            for direction in ('LEFT_RIGHT','RIGHT_LEFT','BOTTOM_TOP','TOP_BOTTOM'):
                controls.direction=direction;controls.width=0
                assert all(abs(uv[0])<2e6 for f in uv_values(bm,addon.SHARED_FACTOR_UV)[:2] for uv in f)
            assert uv_values(bm,'Original UVs')==original
    return 'All chart/fit combinations and four directions, including zero width, update without remapping source UVs.'


def test_shared_to_separate():
    obj,bm,atlas,mat=setup();assert t_apply()
    controls=addon.active_shared_patch(bpy.context)
    bpy.context.scene.sprotile_blend_material_mode='SEPARATE'
    assert t_apply()
    assert bpy.ops.mesh.sprotile_remove_blend()=={'FINISHED'}
    assert bm.faces[0].material_index==0
    controls.position=.1
    assert uv_values(bm,addon.SHARED_FACTOR_UV)[:2]==[[(0,0)]*3]*2
    assert addon.active_shared_patch(bpy.context) is None
    return 'Converting a shared patch to Separate clears old membership; removing it returns the unblended original.'


def t_apply():return addon.apply_texture_blend(bpy.context)


def test_multiple_materials_and_links():
    first,second,atlas,mat=multiple_objects()
    assert addon.apply_texture_blend(bpy.context)
    assert len(mat.node_tree.nodes)==len({n.as_pointer() for n in mat.node_tree.nodes})
    assert len(first.data.materials)==len(second.data.materials)==1
    first_controls=addon.active_shared_patch(bpy.context)
    second_uvs=uv_values(bmesh.from_edit_mesh(second.data),addon.SHARED_FACTOR_UV)
    first_controls.position=.2
    assert uv_values(bmesh.from_edit_mesh(second.data),addon.SHARED_FACTOR_UV)==second_uvs
    leave()
    instance=bpy.data.objects.new('Linked override',first.data);bpy.context.collection.objects.link(instance)
    bpy.ops.object.select_all(action='DESELECT')
    first.select_set(True);instance.select_set(True);bpy.context.view_layer.objects.active=first
    instance.material_slots[0].link='OBJECT'
    bpy.ops.object.mode_set(mode='EDIT')
    before=graph_state(mat)
    assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert graph_state(mat)==before
    return 'Shared source upgrades once across meshes; patch controls stay mesh-local; linked overrides reject before writes.'


def test_existing_graph_failure():
    obj,bm,atlas,mat,first,second=two_patches()
    before=[uv_values(bm,name) for name in BLEND_ROLES+(addon.SHARED_FACTOR_UV,)]
    graph=graph_state(mat);count=len(obj.data.sprotile_blend_patches)
    with patch.object(bmesh,'update_edit_mesh',side_effect=[RuntimeError('Injected reapply'),None]):
        assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert [uv_values(bm,name) for name in BLEND_ROLES+(addon.SHARED_FACTOR_UV,)]==before
    assert graph_state(mat)==graph and len(obj.data.sprotile_blend_patches)==count
    assert abs(first.position-.25)<1e-6 and abs(second.position-.75)<1e-6
    return 'Reapply failure preserves an already-upgraded graph, both earlier patches and their controls.'


def test_vertex_selection_controls():
    obj,bm,atlas,mat=setup();assert addon.apply_texture_blend(bpy.context)
    active=addon.active_shared_patch(bpy.context)
    bm.faces.active=None
    assert addon.active_shared_patch(bpy.context).patch_id == active.patch_id
    select_faces(obj,[0,1,2,3]);bm.faces.active=None
    assert addon.active_shared_patch(bpy.context) is None
    return 'Vertex/edge selections expose a single selected patch even without an active face; mixed selections stay unambiguous.'


def test_uv_limit_and_graph_edit():
    obj,bm,atlas,mat=setup()
    for n in range(6):bm.loops.layers.uv.new('Extra '+str(n))   # 7 maps + B + Factor > 8
    before=graph_state(mat)
    assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert graph_state(mat)==before and len(bm.loops.layers.uv)==7
    obj,bm,atlas,mat=setup();assert addon.apply_texture_blend(bpy.context)
    mat.node_tree.nodes.remove(addon.shared_nodes(mat)['gradient_linear'])
    before=graph_state(mat);uvs=uv_values(bm,addon.SHARED_FACTOR_UV)
    assert bpy.ops.mesh.sprotile_apply_blend()=={'CANCELLED'}
    assert graph_state(mat)==before and uv_values(bm,addon.SHARED_FACTOR_UV)==uvs
    return 'UV-capacity and manually deleted shader support nodes are rejected before any further edits.'


def test_object_mode_and_save():
    obj,bm,atlas,mat,first,second=two_patches();name=obj.name
    leave();second.width=.22;second.direction='BOTTOM_TOP';atlas.pack()
    expected=[tuple(x.uv) for x in obj.data.uv_layers[addon.SHARED_FACTOR_UV].data]
    path=HERE/'shared_blend_saved_state.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path));bpy.ops.wm.open_mainfile(filepath=str(path))
    obj=bpy.data.objects[name];mesh=obj.data
    assert len(mesh.materials)==1 and len(mesh.sprotile_blend_patches)==2
    assert [tuple(x.uv) for x in mesh.uv_layers[addon.SHARED_FACTOR_UV].data]==expected
    mesh.sprotile_blend_patches[1].position=.1
    assert [tuple(x.uv) for x in mesh.uv_layers[addon.SHARED_FACTOR_UV].data]!=expected
    return 'Patch controls/UVs persist on disk and update in Object Mode after reload.'


def test_undo():
    obj,bm,atlas,mat=setup();name=obj.name;matname=mat.name
    bpy.ops.ed.undo_push(message='Before shared blend')
    assert bpy.ops.mesh.sprotile_apply_blend()=={'FINISHED'}
    bpy.ops.ed.undo_push(message='After shared blend')
    assert bpy.ops.ed.undo()=={'FINISHED'}
    obj=bpy.data.objects[name];mat=bpy.data.materials[matname]
    bm=bmesh.from_edit_mesh(obj.data)
    assert not bm.loops.layers.uv.get(addon.SHARED_FACTOR_UV) and addon.active_shared_patch(bpy.context) is None
    assert bpy.ops.ed.redo()=={'FINISHED'}
    obj=bpy.data.objects[name];mat=bpy.data.materials[matname]
    assert mat.get(addon.SHARED_MATERIAL_TAG) and len(obj.data.sprotile_blend_patches)==1
    controls=addon.active_shared_patch(bpy.context)
    assert controls is not None
    bpy.ops.ed.undo_push(message='Before patch adjustment')
    controls.position=.1;controls.width=.15;controls.direction='TOP_BOTTOM';controls.smooth=False
    bpy.ops.ed.undo_push(message='After patch adjustment')
    assert bpy.ops.ed.undo()=={'FINISHED'}
    controls=addon.active_shared_patch(bpy.context)
    assert abs(controls.position-.5)<1e-6 and abs(controls.width-.6)<1e-6
    assert controls.direction=='LEFT_RIGHT' and controls.smooth
    assert bpy.ops.ed.redo()=={'FINISHED'}
    controls=addon.active_shared_patch(bpy.context)
    assert abs(controls.position-.1)<1e-6 and controls.direction=='TOP_BOTTOM' and not controls.smooth
    return 'Native mesh Undo/Redo restores patch membership, rendered factors and live controls together; unused shader support stays installed.'


if __name__=='__main__':
    for fn in (test_preservation,test_independent,test_reapply_subset_remove,test_bad_targets,
               test_atomic_write,test_atomic_graph,test_migration,test_methods_and_record_updates,
               test_shared_to_separate,test_multiple_materials_and_links,test_existing_graph_failure,test_vertex_selection_controls,
               test_uv_limit_and_graph_edit,
               test_undo,test_object_mode_and_save):
        try:detail=fn()
        except Exception:
            traceback.print_exc();RESULTS.append(dict(name=fn.__name__,passed=False,traceback=traceback.format_exc()))
        else:RESULTS.append(dict(name=fn.__name__,passed=True,detail=detail));print('PASS',fn.__name__)
    report=dict(results=RESULTS,passed=sum(r['passed'] for r in RESULTS),total=len(RESULTS))
    (HERE/'shared_blend_results.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    leave();addon.unregister()
    if report['passed']!=report['total']:raise SystemExit(1)
