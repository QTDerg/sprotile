"""Quick start tasks, the sidebar feature toggles and the Sprotile workspace button."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys,json,traceback
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_shared_blend as t
addon,bpy=t.addon,t.bpy

class Layout:
    def __init__(self):self.props=[];self.ops=[];self.labels=[];self.panels=[];self.enums=[]
    def row(self,**kw):return self
    def column(self,**kw):return self
    def box(self):return self
    def panel(self,idname,**kw):self.panels.append(idname);return self,None
    def prop(self,owner,name,**kw):self.props.append((owner,name))
    def prop_enum(self,owner,name,value,**kw):self.enums.append((name,value))
    def label(self,text='',**kw):self.labels.append(text)
    def menu(self,*args,**kw):pass
    def separator(self,**kw):pass
    def __getattr__(self,name):return lambda *a,**k:self
    def operator(self,name,**kw):self.ops.append((name,kw));return SimpleNamespace()

def draw(prefs=None):
    layout=Layout()
    with patch.object(addon,'prefs',return_value=prefs):
        addon.VIEW3D_PT_sprotile_blend.draw(SimpleNamespace(layout=layout),bpy.context)
    return layout

def fresh():
    scene=bpy.context.scene
    scene.sprotile_blend_settings_source='NEW'
    scene.sprotile_blend_task='CUSTOM'
    return scene,scene.sprotile_blend_defaults

def test_tasks_set_new_blend_settings():
    scene,c=fresh()
    c.edge_style='SNOW';c.tiles='FACE';scene.sprotile_blend_task='CUSTOM'
    assert (c.edge_style,c.tiles)==('SNOW','FACE')
    scene.sprotile_blend_task='FADE';assert (c.edge_style,c.tiles,c.path)==('PLAIN','PATCH',False)
    scene.sprotile_blend_task='EDGE';assert (c.edge_style,c.tiles)==('GRASS','PATCH')
    scene.sprotile_blend_task='OVERLAY';assert (c.edge_style,c.tiles)==('OVERLAY','FACE')
    scene.sprotile_blend_task='PATH';assert c.edge_style=='PLAIN' and c.path
    scene.sprotile_blend_task='CUSTOM';assert c.edge_style=='PLAIN' and c.path   # Custom changes nothing
    scene.sprotile_blend_task='FADE'

def test_custom_keeps_full_layout_and_tasks_use_advanced():
    scene,c=fresh();layout=draw()
    assert 'sprotile_blend_options' in layout.panels and 'sprotile_blend_advanced' not in layout.panels
    fields=[name for _o,name in layout.props]
    assert 'sprotile_blend_task' in fields and 'edge_style' in fields and 'direction' in fields
    scene.sprotile_blend_task='FADE';layout=draw();fields=[name for _o,name in layout.props]
    assert 'sprotile_blend_advanced' in layout.panels and 'feather_percent' in fields
    assert 'edge_style' not in fields and 'direction' not in fields   # Advanced is collapsed in the fake layout
    scene.sprotile_blend_task='EDGE';fields=[name for _o,name in draw().props]
    assert 'edge_height' in fields and 'feather_percent' not in fields
    scene.sprotile_blend_task='OVERLAY';layout=draw();fields=[name for _o,name in layout.props]
    assert 'opacity' in fields and 'mesh.sprotile_make_overlay' in [n for n,_k in layout.ops]
    scene.sprotile_blend_task='PATH';fields=[name for _o,name in draw().props]
    assert 'path_width_percent' in fields and 'feather_percent' in fields
    # Editing a setting the task does not cover leaves the task alone.
    scene.sprotile_blend_defaults.position=.3;assert scene.sprotile_blend_task=='PATH'
    scene.sprotile_blend_task='CUSTOM'

def test_paint_tasks_have_their_own_button():
    scene,c=fresh()
    scene.sprotile_blend_task='PAINTED'
    assert (c.tiles,c.alpha_shaping,c.path)==('FACE',True,False) and scene.sprotile_blend_material_mode=='SHARED'
    ops=[name for name,_k in draw().ops]
    assert 'mesh.sprotile_paint_alpha' in ops and 'mesh.sprotile_apply_blend' not in ops
    c.edge_style='OVERLAY';scene.sprotile_blend_task='PATH_BRUSH'
    assert c.edge_style=='PLAIN' and c.tiles=='FACE'
    layout=draw();ops=[name for name,_k in layout.ops];fields=[name for _o,name in layout.props]
    assert 'mesh.sprotile_path_brush' in ops and 'mesh.sprotile_path_selection' in ops
    assert 'mesh.sprotile_apply_blend' not in ops and 'feather_percent' in fields and 'edge_style' in fields
    scene.sprotile_blend_task='CUSTOM'
    assert 'mesh.sprotile_apply_blend' in [name for name,_k in draw().ops]


def test_task_switch_places_the_selected_kind_of_blend():
    t.setup()
    scene, controls = fresh()
    scene.sprotile_blend_task = 'PAINTED'
    scene.sprotile_blend_task = 'PATH'
    assert (controls.tiles, controls.path, controls.alpha_shaping) == ('PATCH', True, False)
    assert bpy.ops.mesh.sprotile_apply_blend() == {'FINISHED'}
    patch = addon.active_shared_patch(bpy.context)
    assert patch is not None and patch.path and patch.tiles == 'PATCH'
    assert not patch.alpha_shaping

    scene.sprotile_blend_task = 'EDGE'
    assert (controls.edge_style, controls.path) == ('GRASS', False)
    scene.sprotile_blend_task = 'PATH'
    scene.sprotile_blend_task = 'OVERLAY'
    assert (controls.edge_style, controls.tiles, controls.path) == ('OVERLAY', 'FACE', False)
    scene.sprotile_blend_task = 'CUSTOM'
    assert (controls.edge_style, controls.tiles, controls.path) == ('OVERLAY', 'FACE', False)


def test_paint_b_remains_available_with_separate_material_setting():
    obj, _bm, _atlas, _mat = t.setup()
    scene, controls = fresh()
    scene.sprotile_blend_task = 'PAINTED'
    scene.sprotile_blend_material_mode = 'SEPARATE'
    controls.edge_style = 'OVERLAY'
    ops = [name for name, _kwargs in draw().ops]
    assert 'mesh.sprotile_paint_alpha' in ops
    assert 'mesh.sprotile_apply_blend' not in ops
    assert bpy.ops.mesh.sprotile_paint_alpha() == {'FINISHED'}
    assert obj.mode == 'VERTEX_PAINT'
    assert obj.active_material.get(addon.SHARED_MATERIAL_TAG)
    t.leave()


def test_preferences_hide_paths_and_overlays():
    scene,c=fresh()
    prefs=SimpleNamespace(show_blends=True,show_paths=False,show_overlays=False,show_unreal=True,show_library=True)
    layout=draw(prefs);fields=[name for _o,name in layout.props]
    assert 'path' not in fields and 'edge_style' not in fields
    assert ('edge_style','OVERLAY') not in layout.enums and ('edge_style','GRASS') in layout.enums
    assert not any(n=='sprotile_blend_task' and v in ('PATH','PATH_BRUSH','OVERLAY') for n,v in layout.enums)
    scene.sprotile_blend_task='OVERLAY';c.edge_style='OVERLAY'   # a leftover task must not show overlay buttons
    assert 'mesh.sprotile_make_overlay' not in [n for n,_k in draw(prefs).ops]
    scene.sprotile_blend_task='CUSTOM';c.edge_style='PLAIN'

def test_panel_polls_follow_preferences():
    on=dict(show_blends=True,show_paths=True,show_overlays=True,show_unreal=True,show_library=True)
    for cls,flag in ((addon.VIEW3D_PT_sprotile_blend,'show_blends'),(addon.VIEW3D_PT_sprotile_blend_list,'show_blends'),
                     (addon.VIEW3D_PT_sprotile_presets,'show_library'),(addon.VIEW3D_PT_sprotile_unreal,'show_unreal'),
                     (addon.VIEW3D_PT_sprotile_overlay_browser,'show_overlays'),
                     (addon.VIEW3D_PT_sprotile_overlay_browser,'show_blends')):
        with patch.object(addon,'prefs',return_value=SimpleNamespace(**on)):assert cls.poll(bpy.context)
        with patch.object(addon,'prefs',return_value=SimpleNamespace(**{**on,flag:False})):assert not cls.poll(bpy.context)
        assert cls.poll(bpy.context)   # no preferences for this package name: everything stays on

def test_prefs_helper_is_safe_without_an_installed_addon():
    assert addon.prefs(bpy.context) is None and addon.prefs(None) is None
    assert addon.prefs(SimpleNamespace()) is None
    assert addon.feature_enabled(bpy.context,'show_blends') is True
    assert issubclass(addon.SprotilePreferences,bpy.types.AddonPreferences) and addon.SprotilePreferences.bl_idname

def test_workspace_button_only_without_a_sprotile_workspace():
    layout=Layout();scene=bpy.context.scene
    ctx=SimpleNamespace(scene=scene,active_object=None)
    existing=bpy.data.workspaces.get('Sprotile')
    if existing is not None:bpy.data.workspaces.remove(existing)
    addon.VIEW3D_PT_sprotile_main.draw(SimpleNamespace(layout=layout),ctx)
    assert 'screen.sprotile_workspace' in [n for n,_k in layout.ops]
    assert hasattr(bpy.types,'SCREEN_OT_sprotile_workspace')

results=[]
for test in (test_tasks_set_new_blend_settings,test_custom_keeps_full_layout_and_tasks_use_advanced,test_paint_tasks_have_their_own_button,
             test_preferences_hide_paths_and_overlays,test_panel_polls_follow_preferences,test_prefs_helper_is_safe_without_an_installed_addon,
             test_workspace_button_only_without_a_sprotile_workspace,test_task_switch_places_the_selected_kind_of_blend,
             test_paint_b_remains_available_with_separate_material_setting):
    try:test()
    except Exception:
        traceback.print_exc();results.append(dict(name=test.__name__,passed=False,traceback=traceback.format_exc()))
    else:results.append(dict(name=test.__name__,passed=True))
report=dict(results=results,passed=sum(r['passed'] for r in results),total=len(results))
(t.HERE/'quick_start_ui_results.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
t.leave();addon.unregister()
if report['passed']!=report['total']:raise SystemExit(1)
