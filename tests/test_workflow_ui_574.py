"""New/Edit workflow scope, saving the active recipe and deferred thumbnail lifecycle."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import base64, sys, json, traceback, zlib
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_shared_blend as t
addon,bpy=t.addon,t.bpy

class Layout:
    def __init__(self):self.props=[];self.ops=[];self.labels=[]
    def row(self,**kw):return self
    def split(self,**kw):return self
    def column(self,**kw):return self
    def box(self):return self
    def panel(self,*args,**kw):return self,None
    def prop(self,owner,name,**kw):self.props.append((owner,name))
    def label(self,text='',**kw):self.labels.append(text)
    def menu(self,*args,**kw):pass
    def operator(self,name,**kw):self.ops.append((name,kw));return SimpleNamespace()

def setup():
    obj,bm,atlas,mat=t.setup();scene=bpy.context.scene
    scene.sprotile_blend_settings_source='NEW';scene.sprotile_blend_defaults.edge_style='SNOW'
    scene.sprotile_blend_presets.clear();assert addon.apply_texture_blend(bpy.context)
    return obj,bm,atlas,mat

def test_edit_hides_capture_and_new_exposes_apply():
    obj,bm,atlas,mat=setup();scene=bpy.context.scene
    layout=Layout()
    with patch.object(addon,'clipboard_preview_icon',side_effect=AssertionError('Collapsed inputs requested a thumbnail')):
        addon.VIEW3D_PT_sprotile_blend.draw(SimpleNamespace(layout=layout),bpy.context)
    names=[name for name,_kw in layout.ops]
    assert 'mesh.sprotile_save_preset' in names and 'mesh.sprotile_pick_blend' in names
    assert 'mesh.sprotile_capture_blend_tile' not in names and 'mesh.sprotile_apply_blend' not in names
    assert 'New Blend tiles' in layout.labels
    scene.sprotile_blend_settings_source='NEW';layout=Layout()
    addon.VIEW3D_PT_sprotile_blend.draw(SimpleNamespace(layout=layout),bpy.context)
    names=[name for name,_kw in layout.ops]
    assert names.count('mesh.sprotile_capture_blend_tile')==2 and 'mesh.sprotile_apply_blend' in names
    fields=[name for _owner,name in layout.props]
    assert fields.index('edge_style')<fields.index('feather_percent')<fields.index('position')<fields.index('direction')

def test_save_this_blend_ignores_next_tiles_and_does_not_rename_clipboard():
    obj,bm,atlas,mat=setup();scene=bpy.context.scene;expected=addon.read_blend_recipe(bpy.context)
    t.capture(atlas,2,3);scene.sprotile_preset_name='Unrelated next recipe'
    uv=[t.uv_values(bm,n) for n in t.BLEND_ROLES]
    assert bpy.ops.mesh.sprotile_save_preset(source='ACTIVE',ask_name=True,preset_name='Snow at wall')=={'FINISHED'}
    preset=addon.selected_preset(scene);assert preset.name=='Snow at wall' and addon.recipes_match(expected,addon.preset_recipe(preset))
    assert scene.sprotile_blend_a.col==2 and scene.sprotile_preset_name=='Unrelated next recipe'
    assert [t.uv_values(bm,n) for n in t.BLEND_ROLES]==uv
    count=len(scene.sprotile_blend_presets)
    assert bpy.ops.mesh.sprotile_save_preset(source='ACTIVE',ask_name=True,preset_name='  ')=={'CANCELLED'}
    assert len(scene.sprotile_blend_presets)==count

def test_deferred_previews_are_bounded_and_cleaned():
    obj,bm,atlas,mat=setup();scene=bpy.context.scene
    addon._clipboard_previews.clear();addon._clipboard_preview_pending.clear()
    tile,signature=addon.clipboard_preview_source(scene,'a');key=(scene.session_uid,'a')
    addon._clipboard_preview_pending[key]=signature
    assert addon.process_clipboard_previews() is None
    assert addon._clipboard_previews[key][0]==signature and addon._clipboard_previews[key][1].startswith('1:')
    cached=addon._clipboard_previews[key]
    # No queued work means no pixel reads, even when called again.
    with patch.object(addon,'image_rgb_sampler',side_effect=AssertionError('Unqueued work sampled pixels')):
        assert addon.process_clipboard_previews() is None
    addon._clipboard_preview_pending[key]=signature
    scene.sprotile_blend_a.image=None
    addon.process_clipboard_previews();assert addon._clipboard_previews[key][1]==''
    addon._clipboard_preview_pending[(999999999,'b')]=signature
    assert addon.process_clipboard_previews() is None
    bpy.app.timers.register(addon.process_clipboard_previews,first_interval=10)
    addon.force_cleanup()
    assert not addon._clipboard_previews and not addon._clipboard_preview_pending
    assert not bpy.app.timers.is_registered(addon.process_clipboard_previews)


@patch.object(addon, 'OVERLAY_THUMBNAIL_READ_LIMIT', 1)
@patch.object(addon, 'OVERLAY_THUMBNAIL_BUDGET', 144)
def test_selected_overlay_preview_is_deferred_and_composites_alpha():
    scene = bpy.context.scene
    scene.sprotile_blend_a.image = bpy.data.images.new('Independent base atlas', width=16, height=16)
    atlas = bpy.data.images.new('Overlay thumbnail fixture', width=33, height=17, alpha=True)
    atlas.pixels.foreach_set([channel for y in range(17) for x in range(33) for channel in
                              ((1., 0., 0., float(y >= 9)) if x < 16 else (0., 0., 1., float(y >= 9)))])
    scene.sprotile_overlay_source = 'SHEET'
    scene.sprotile_overlay_sheet = atlas
    scene.sprotile_overlay_sheet_size = 16
    # A one-pixel cell can never contain both clear and solid texels. No Blender data read is needed.
    assert addon.scan_overlay_cells(SimpleNamespace(size=(100000, 100000)), cell=1) == []
    expected = addon.scan_overlay_cells(atlas, cell=16)
    assert addon.scan_overlay_cells(atlas, cell=16, force=True) == expected
    assert [(col, row) for col, row, _kind, _colour in expected] == [(0, 0), (1, 0)]
    first = addon.overlay_thumbnail_pixels(atlas, 16, 0, 0)
    second = addon.overlay_thumbnail_pixels(atlas, 16, 1, 0)
    assert first.shape == second.shape == (2, 2, 4)
    assert first.nbytes + second.nbytes + 2 * 8 <= 144
    scene.sprotile_overlay_scan_image = atlas
    scene.sprotile_overlay_scan_grid = (33, 17, 16)
    scene.sprotile_overlay_cells.clear()
    for col in range(2):
        item = scene.sprotile_overlay_cells.add()
        item.col, item.row, item.kind = col, 0, 'extra'
    scene.sprotile_overlay_kind = 'ALL'
    scene.sprotile_overlay_index = 0
    slot = 'overlay_cell'
    key = (scene.session_uid, slot)
    sampler = addon.image_rgb_sampler
    presets = sys.modules[sampler.__module__]
    timers = Mock()
    timers.is_registered.side_effect = [False, True]
    fake_app = SimpleNamespace(background=False, timers=timers)
    addon.invalidate_clipboard_slot(scene, slot)
    with (patch.object(presets, 'bpy', SimpleNamespace(app=fake_app)),
          patch.object(presets, 'image_rgb_sampler', side_effect=AssertionError('Redraw read pixels'))):
        assert addon.clipboard_preview_icon(scene, slot) is None
        assert addon.clipboard_preview_icon(scene, slot) is None
    timers.register.assert_called_once_with(addon.process_clipboard_previews, first_interval=.05)
    assert list(addon._clipboard_preview_pending) == [key]
    first_signature = addon._clipboard_preview_pending[key]
    scene.sprotile_overlay_index = 1
    _, second_signature = addon.clipboard_preview_source(scene, slot)
    assert second_signature != first_signature
    with patch.object(presets, 'image_rgb_sampler', side_effect=AssertionError('Stale selection read pixels')):
        assert addon.process_clipboard_previews() is None
    assert key not in addon._clipboard_previews

    def rgba_at(data, x, y):
        start = (y * 48 + x) * 4
        return tuple(data[start:start + 4])

    scene.sprotile_overlay_index = 0
    addon._clipboard_preview_pending[key] = first_signature
    with patch.object(presets, 'image_rgb_sampler', side_effect=AssertionError('Cached large atlas read from RNA')):
        assert addon.process_clipboard_previews() is None
    signature, payload = addon._clipboard_previews[key]
    assert signature == first_signature
    data = zlib.decompress(base64.b64decode(payload[2:]))
    assert rgba_at(data, 0, 0) == (153, 153, 153, 255)
    assert rgba_at(data, 6, 0) == (102, 102, 102, 255)
    assert rgba_at(data, 0, 47) == (255, 0, 0, 255)

    scene.sprotile_overlay_index = 1
    scene.sprotile_overlay_sheet_size = 32
    with patch.object(presets, 'bpy', SimpleNamespace(app=fake_app)):
        assert addon.clipboard_preview_icon(scene, slot) is None
    assert not addon._clipboard_preview_pending
    scene.sprotile_overlay_sheet_size = 16
    scene.sprotile_overlay_index = 99
    with patch.object(presets, 'bpy', SimpleNamespace(app=fake_app)):
        assert addon.clipboard_preview_icon(scene, slot) is None
    assert not addon._clipboard_preview_pending
    scene.sprotile_overlay_index = 1
    addon.force_cleanup()
    assert addon.overlay_thumbnail_pixels(atlas, 16, 1, 0) is None
    with (patch.object(presets, 'bpy', SimpleNamespace(app=fake_app)),
          patch.object(presets, 'image_rgb_sampler', side_effect=AssertionError('Uncached large atlas read from RNA'))):
        assert addon.clipboard_preview_icon(scene, slot) is None
    assert not addon._clipboard_preview_pending
    addon.invalidate_clipboard_slot(scene, slot)
    assert key not in addon._clipboard_previews


results=[]
for test in (test_edit_hides_capture_and_new_exposes_apply,test_save_this_blend_ignores_next_tiles_and_does_not_rename_clipboard,
             test_deferred_previews_are_bounded_and_cleaned,test_selected_overlay_preview_is_deferred_and_composites_alpha):
    try:test()
    except Exception:
        traceback.print_exc();results.append(dict(name=test.__name__,passed=False,traceback=traceback.format_exc()))
    else:results.append(dict(name=test.__name__,passed=True))
report=dict(results=results,passed=sum(r['passed'] for r in results),total=len(results))
(t.HERE/'workflow_ui_574_results.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
t.leave();addon.unregister()
if report['passed']!=report['total']:raise SystemExit(1)
