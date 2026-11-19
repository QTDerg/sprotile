"""Blend presets: save, load, previews, rectangle previews, Find Uses and the preset panel.
"""

import bpy
import bmesh
import math
import uuid
import base64
import zlib
from array import array
from bpy.props import StringProperty, EnumProperty, BoolProperty
from . import state


def tile_as_sample(tile):
    if tile['is_sampled']:return dict(tile)
    u,v,w,h = tile_uv_bounds(tile['col'],tile['row'],tile['width_px'],tile['height_px'],
                            tile['atlas_width'],tile['atlas_height'],tile['inset_px'])
    points = [apply_tile_transform(*p,tile['rotation'],tile['flip_u'],tile['flip_v']) for p in ((0,0),(1,0),(0,1))]
    points = [(u+x*w,v+y*h) for x,y in points]
    coeff = tuple(value for axis in (0,1) for value in
                  (points[1][axis]-points[0][axis],points[2][axis]-points[0][axis],points[0][axis]))
    sample_uv_bounds(coeff)
    return dict(tile,is_sampled=True,sample_uv=coeff,width_px=0,height_px=0,col=0,row=0,rotation=0,flip_u=False,flip_v=False,inset_px=0.)


def preset_recipe(preset):
    recipe = dict(a=tile_as_sample(validated_blend_tile(preset.a)),b=tile_as_sample(validated_blend_tile(preset.b)),
                  settings=validated_blend_settings(preset.settings),mode=preset.material_mode)
    return recipe


def selected_preset(scene):
    index = scene.sprotile_preset_index
    if not 0 <= index < len(scene.sprotile_blend_presets):raise ValueError('Select a saved blend preset first')
    return scene.sprotile_blend_presets[index]


def recipes_match(first,second):
    sa,sb=first['settings'],second['settings']
    if sa['edge_style']!=sb['edge_style']:return False
    if bool(sa.get('path')) != bool(sb.get('path')):return False
    if sa.get('path') and abs(sa['path_width'] - sb['path_width']) > 2e-6:return False
    if sa.get('tiles', 'PATCH') != sb.get('tiles', 'PATCH'):return False
    if sa['edge_style'] in SHAPED_EDGES and any(abs(sa[k]-sb[k])>2e-6 for k in ('edge_height','edge_frequency')):return False
    for slot in ('a','b'):
        a,b = first[slot],second[slot]
        if a['image']!=b['image'] or max(abs(x-y) for x,y in zip(a['sample_uv'],b['sample_uv']))>2e-6:return False
    if sa['edge_style'] == 'OVERLAY':
        return abs(sa['opacity'] - sb['opacity']) < 2e-5
    def normalized(settings):
        c=BlendSettings(**settings);x,y,_offset,reverse=blend_axis(c)
        return (-x if reverse else x,-y if reverse else y,1-c.position if reverse else c.position,c.width,c.smooth)
    a,b=normalized(first['settings']),normalized(second['settings'])
    return a[-1]==b[-1] and max(abs(x-y) for x,y in zip(a[:-1],b[:-1]))<2e-6


def clear_preset_previews():
    if state._preset_previews is not None:
        from bpy.utils import previews
        previews.remove(state._preset_previews)
        state._preset_previews=None


def preset_preview_key(scene,preset):
    # Undo can restore different pixels under the same preset UID.
    return str(scene.session_uid)+':'+preset.uid+':'+str(hash(preset.preview_data))


def image_rgb_sampler(image,uv_bounds=None, *, with_alpha=False):
    width,height=image_size(image);data=image.pixels
    channels=len(data)//(width*height) if width and height else 0
    if channels<1:raise ValueError('Atlas pixels are not available for a preview')
    # Individual RNA pixel reads repeatedly validate the entire image buffer.
    # Read the tile's row span once; keep compact floats, not a full atlas copy.
    bounds=list(uv_bounds) if uv_bounds is not None else [(0.,0.),(1.,1.)]
    if not bounds or any(not math.isfinite(v) for uv in bounds for v in uv):raise ValueError('Preview UV bounds are invalid')
    xmin=max(0,min(width-1,int(min(uv[0] for uv in bounds)*width)-1))
    xmax=max(0,min(width-1,int(max(uv[0] for uv in bounds)*width)+1))
    ymin=max(0,min(height-1,int(min(uv[1] for uv in bounds)*height)-1))
    ymax=max(0,min(height-1,int(max(uv[1] for uv in bounds)*height)+1))
    start=(ymin*width+xmin)*channels;end=(ymax*width+xmax+1)*channels
    if end-start>8*1024*1024:raise ValueError('The preview covers too much of the atlas; use a smaller tile region')
    pixels=array('f',data[start:end])
    alpha_mode = getattr(image, 'alpha_mode', 'STRAIGHT')
    def sample(uv):
        x=max(0,min(width-1,int(uv[0]*width)));y=max(0,min(height-1,int(uv[1]*height)))
        if not (xmin<=x<=xmax and ymin<=y<=ymax):raise ValueError('Preview coordinates changed outside the sampled region')
        offset=(y*width+x)*channels-start
        rgb = tuple(pixels[offset+c] for c in range(3)) if channels>=3 else (pixels[offset],)*3
        if not with_alpha:
            return rgb
        alpha = pixels[offset+channels-1] if channels in (2,4) and alpha_mode != 'NONE' else 1.0
        return (*rgb, min(1.0,max(0.0,alpha)))
    return sample


def encode_preview_pixels(pixels):
    # Clamp before quantising. Float atlases can contain infinities or NaNs.
    # max(0, NaN) keeps zero, while +/-infinity saturates at the endpoints.
    raw = bytes(round(min(1.0, max(0.0, value)) * 255) for value in pixels)
    return '1:' + base64.b64encode(zlib.compress(raw)).decode('ascii')


def refresh_preset_preview(scene,preset):
    recipe=preset_recipe(preset)
    key=preset_preview_key(scene,preset)
    if state._preset_previews is not None and key in state._preset_previews:del state._preset_previews[key]
    size=48
    mapper=shared_factor_mapper(BlendSettings(**recipe['settings']))
    edge_mapper=shared_edge_mapper(BlendSettings(**recipe['settings']))
    overlay = recipe['settings']['edge_style'] == 'OVERLAY'
    sources = []
    for slot in ('a','b'):
        tile = recipe[slot]
        bounds = [affine_uv(tile['sample_uv'],uv) for uv in ((0,0),(1,0),(1,1),(0,1))]
        sampler = (image_rgb_sampler(tile['image'], bounds, with_alpha=True) if overlay and slot=='b'
                   else image_rgb_sampler(tile['image'], bounds))
        sources.append((tile, sampler))
    pixels=[]
    for y in range(size):
        for x in range(size):
            uv=((x+.5)/size,(y+.5)/size);factor,mode=mapper(uv)
            tangent,amount=edge_mapper(uv) if recipe['settings']['edge_style'] in SHAPED_EDGES else (0.,0.)
            factor=shader_factor(factor,mode,tangent,amount,snow=recipe['settings']['edge_style']=='SNOW')
            colors=[]
            local = ((uv[0]*2) % 1, (uv[1]*2) % 1) if recipe['settings']['tiles'] == 'FACE' else uv
            for tile,sample in sources:colors.append(sample(affine_uv(tile['sample_uv'],local)))
            if overlay:
                factor = colors[1][3] * recipe['settings']['opacity'] / 100
            pixels.extend(tuple(a+(b-a)*factor for a,b in zip(*colors))+(1.,))
    preset.preview_data = encode_preview_pixels(pixels)
    preset_icon(scene,preset)


def preset_icon(scene,preset):
    return stored_preview_icon(preset_preview_key(scene,preset),preset.preview_data)


def stored_preview_icon(key,text):
    if bpy.app.background:return None
    if state._preset_previews is not None and key in state._preset_previews:return state._preset_previews[key]
    if not text.startswith('1:') or len(text)>40000:return None
    try:
        raw=base64.b64decode(text[2:],validate=True);raw=zlib.decompressobj().decompress(raw,48*48*4+1)
        if len(raw)!=48*48*4:return None
    except (ValueError,zlib.error):return None
    from bpy.utils import previews
    if state._preset_previews is None:state._preset_previews=previews.new()
    if len(state._preset_previews)>=128:
        old=next((k for k in state._preset_previews if k!='rectangle-preview'),None)
        if old is not None:del state._preset_previews[old]
    item=state._preset_previews.new(key);item.image_size=(48,48);item.image_pixels_float=[value/255 for value in raw]
    return item


def clipboard_preview_source(scene,slot):
    tile=tile_as_sample(validated_blend_tile(getattr(scene,'sprotile_blend_'+slot)))
    image=tile['image']
    show_alpha = slot == 'b' and scene.sprotile_blend_defaults.edge_style == 'OVERLAY'
    return tile,(image.session_uid,tile['atlas_width'],tile['atlas_height'],tuple(tile['sample_uv']),image.is_dirty,
                 image.alpha_mode,show_alpha)


def process_clipboard_previews():
    if not _clipboard_preview_pending:return None
    key,signature=next(iter(_clipboard_preview_pending.items()));del _clipboard_preview_pending[key]
    scene=next((scene for scene in bpy.data.scenes if scene.session_uid==key[0]),None)
    if scene is not None:
        try:
            tile,current=clipboard_preview_source(scene,key[1])
            if signature==current:
                bounds = [affine_uv(tile['sample_uv'],uv) for uv in ((0,0),(1,0),(1,1),(0,1))]
                show_alpha = current[-1]
                sample = (image_rgb_sampler(tile['image'],bounds,with_alpha=True) if show_alpha
                          else image_rgb_sampler(tile['image'],bounds))
                pixels=[]
                for y in range(48):
                    for x in range(48):
                        rgb = sample(affine_uv(tile['sample_uv'],((x+.5)/48,(y+.5)/48)))
                        if show_alpha:
                            gray = .4 if (x//6+y//6)%2 else .6
                            rgb = tuple(gray+(v-gray)*rgb[3] for v in rgb[:3])
                        pixels.extend((*rgb,1.))
                payload = encode_preview_pixels(pixels)
                if len(_clipboard_previews)>=32:_clipboard_previews.pop(next(iter(_clipboard_previews)))
                _clipboard_previews[key]=(signature,payload)
        except (ValueError,RuntimeError,ReferenceError):
            # Remember a failed preview until its input changes; don't retry every redraw.
            if len(_clipboard_previews)>=32:_clipboard_previews.pop(next(iter(_clipboard_previews)))
            _clipboard_previews[key]=(signature,'')
    tag_redraw_view3d()
    return .01 if _clipboard_preview_pending else None


def clipboard_preview_icon(scene,slot):
    if bpy.app.background:return None
    try:_tile,signature=clipboard_preview_source(scene,slot)
    except (ValueError,RuntimeError,ReferenceError):return None
    key=(scene.session_uid,slot);cached=_clipboard_previews.get(key)
    if cached is not None and cached[0]==signature:
        return stored_preview_icon('clipboard:'+str(key)+':'+str(hash(cached[1])),cached[1])
    if len(_clipboard_preview_pending)>=32:_clipboard_preview_pending.pop(next(iter(_clipboard_preview_pending)))
    _clipboard_preview_pending[key]=signature
    if not bpy.app.timers.is_registered(process_clipboard_previews):bpy.app.timers.register(process_clipboard_previews,first_interval=.05)
    return None


def rectangle_preview_signature(context):
    obj=context.active_object
    if not is_editable_mesh(obj):raise ValueError('Select a patch in Edit Mode')
    if any(other.data != obj.data for other in edit_mesh_objects(context)):
        raise ValueError('Preview one edited mesh at a time')
    bm=bmesh.from_edit_mesh(obj.data);bm.faces.index_update();bm.verts.index_update();bm.edges.index_update()
    faces=[f for f in bm.faces if f.select and not f.hide]
    scene=context.scene;layer=bm.loops.layers.uv.active;corners=bm.verts.layers.int.get(RECTANGLE_CORNER_LAYER)
    settings=tuple(getattr(scene,name) for name in ('sprotile_patch_projection','sprotile_patch_fit','sprotile_rectangle_corner_mode',
        'sprotile_active_col','sprotile_active_row','sprotile_tile_size','sprotile_tile_mode','sprotile_rotation',
        'sprotile_flip_u','sprotile_flip_v','sprotile_use_uv_inset','sprotile_uv_inset_px'))
    uv=tuple(tuple(l[layer].uv) for f in faces for l in f.loops) if layer and scene.sprotile_patch_projection=='EXISTING' else ()
    marks=tuple((v.index,v[corners]) for v in bm.verts if v[corners]) if corners else ()
    view=get_region_3d(context.area) if scene.sprotile_patch_projection=='VIEW' else None
    bindings = []
    for slot in sorted({face.material_index for face in faces}):
        material = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
        image = atlas_for_material_index(obj, slot)
        bindings.append((slot, material.session_uid if material else None,
                         (image.session_uid, image_size(image)) if image else None))
    vertices = {vertex for face in faces for vertex in face.verts}
    geometry = (tuple((v.index, tuple(v.co)) for v in sorted(vertices, key=lambda v: v.index)),
                tuple((f.index, f.material_index, tuple(v.index for v in f.verts)) for f in faces))
    return (obj.session_uid, obj.data.session_uid, layer.name if layer else None, geometry,
            tuple(value for row in obj.matrix_world for value in row),settings,uv,marks,tuple(bindings),tuple(view.view_rotation) if view else (),
            tuple(sorted({(e.index,e.seam) for f in faces for e in f.edges})))


def rectangle_preview_pixels(context,size=160):
    obj=context.active_object;bm=bmesh.from_edit_mesh(obj.data);scene=context.scene
    faces=[f for f in bm.faces if f.select and not f.hide]
    if not faces:raise ValueError('Select a patch to preview')
    slot_images = {}
    for face in faces:
        plain_paint_binding(obj,bm,face)
        if face.material_index not in slot_images:
            image = atlas_for_material_index(obj, face.material_index)
            if image is None:raise ValueError('Every face needs an atlas image')
            slot_images[face.material_index] = image
    image = next(iter(slot_images.values()))
    if any(other != image for other in slot_images.values()):raise ValueError('Preview one atlas at a time')
    method=scene.sprotile_patch_projection;view=None
    if method=='VIEW':
        rv=get_region_3d(context.area)
        if rv is None or is_quad_view(context.area):raise ValueError('View preview needs one 3D Viewport')
        view=rv.view_rotation.copy()
    chart=patch_loop_uvs(faces,obj.matrix_world,method,view,uv_layer=bm.loops.layers.uv.active,
                         reader=existing_uv_reader(obj.data,bm,bm.loops.layers.uv.active))
    if scene.sprotile_rectangle_corner_mode=='MARKED':chart.rectangle_corners=read_rectangle_corners(obj.data,bm,faces)
    width,height=image_size(image);tw,th=get_tile_dims(scene)
    col=max(0,min(width//tw-1,scene.sprotile_active_col));row=max(0,min(height//th-1,scene.sprotile_active_row))
    target=patch_tile_uvs(faces,chart,col,row,tw,th,width,height,*get_tile_transform(scene),get_uv_inset(scene),method,'RECTANGLE')
    sample=image_rgb_sampler(image,target.values());pixels=[0.]*(size*size*4)
    for tri in patch_triangles(faces):
        a,b,c=[chart[loop] for loop in tri];ta,tb,tc=[target[loop] for loop in tri]
        det=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        if abs(det)<1e-12:continue
        xmin=max(0,int(min(a[0],b[0],c[0])*size));xmax=min(size-1,int(max(a[0],b[0],c[0])*size))
        ymin=max(0,int(min(a[1],b[1],c[1])*size));ymax=min(size-1,int(max(a[1],b[1],c[1])*size))
        for y in range(ymin,ymax+1):
            for x in range(xmin,xmax+1):
                u,v=(x+.5)/size-a[0],(y+.5)/size-a[1]
                s=(u*(c[1]-a[1])-v*(c[0]-a[0]))/det;t=((b[0]-a[0])*v-(b[1]-a[1])*u)/det
                if s < -1e-8 or t < -1e-8 or s+t > 1+1e-8:continue
                uv=tuple((1-s-t)*ta[i]+s*tb[i]+t*tc[i] for i in (0,1));offset=(y*size+x)*4
                pixels[offset:offset+4]=list(sample(uv)) + [1.]
    return pixels


class MESH_OT_sprotile_preview_rectangle(bpy.types.Operator):
    """Preview atlas colour on the flattened chart without changing UVs; OK applies"""
    bl_idname='mesh.sprotile_preview_rectangle';bl_label='Preview Rectangle Fit';bl_options={'UNDO'}
    @classmethod
    def poll(cls,context):return is_editable_mesh(context.active_object) and context.scene.sprotile_patch_fit=='RECTANGLE'
    def invoke(self,context,event):
        try:
            self.signature=rectangle_preview_signature(context);pixels=rectangle_preview_pixels(context)
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        from bpy.utils import previews
        if state._preset_previews is None:state._preset_previews=previews.new()
        key='rectangle-preview'
        if key in state._preset_previews:del state._preset_previews[key]
        item=state._preset_previews.new(key);item.image_size=(160,160);item.image_pixels_float=pixels
        self.icon_id=item.icon_id
        return context.window_manager.invoke_props_dialog(self,width=360)
    def draw(self,context):
        self.layout.template_icon(icon_value=self.icon_id,scale=12)
        self.layout.label(text='Colour preview on flattened chart; no Tint/lighting')
        self.layout.label(text='OK applies the fit. Cancel leaves UVs unchanged.')
    def execute(self,context):
        try:
            if getattr(self,'signature',None)!=rectangle_preview_signature(context):raise ValueError('Selection changed; preview the fit again')
            if not map_selected_patches_to_active(context):raise ValueError('No selected faces to map')
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        return {'FINISHED'}


class SPROTILE_UL_blend_presets(bpy.types.UIList):
    def filter_items(self,context,data,propname):
        items=getattr(data,propname);helper=bpy.types.UI_UL_list
        flags=helper.filter_items_by_name(self.filter_name,self.bitflag_filter_item,items,'name') if self.filter_name else []
        order=helper.sort_items_by_name(items,'name') if self.use_filter_sort_alpha else []
        return flags,order
    def draw_item(self,context,layout,data,item,icon,active_data,active_propname,index):
        cached=preset_icon(context.scene,item)
        if cached:layout.label(text='',icon_value=cached.icon_id)
        else:layout.label(text='',icon='TEXTURE')
        layout.prop(item,'name',text='',emboss=False)


def default_preset_name(context, recipe):
    """Name offered by Save as Preset: the blend's own name once renamed, else the library's name or the style."""
    patch = active_shared_patch(context)
    if patch is not None and patch.name.strip() and patch.name != f'Patch {patch.patch_id}':
        return patch.name.strip()
    name = context.scene.sprotile_preset_name
    if name == 'New blend':
        name = dict((item[0], item[1]) for item in EDGE_STYLES)[recipe['settings']['edge_style']]
    return name


def add_preset(scene,recipe,name,select=True):
    """Append a preset holding the recipe under a free name and draw its preview; selects it unless told not to."""
    names={p.name for p in scene.sprotile_blend_presets};base=name;suffix=2
    while name in names:name=base+' '+str(suffix);suffix+=1
    preset=scene.sprotile_blend_presets.add();preset.name=name;preset.uid=uuid.uuid4().hex
    try:
        for slot in ('a','b'):
            for key,value in recipe[slot].items():setattr(getattr(preset,slot),key,value)
        for key,value in recipe['settings'].items():setattr(preset.settings,key,value)
        preset.material_mode=recipe['mode']
    except Exception:
        scene.sprotile_blend_presets.remove(len(scene.sprotile_blend_presets)-1);raise
    if select:scene.sprotile_preset_index=len(scene.sprotile_blend_presets)-1
    try:refresh_preset_preview(scene,preset)
    except (ValueError,RuntimeError,ReferenceError):pass
    return name


def ensure_preset_for_recipe(scene,recipe,name_hint):
    """Add the recipe to the library unless an identical one is there; returns the new name or None."""
    for preset in scene.sprotile_blend_presets:
        try:
            # Shared and Separate Material versions of one look are different presets (Load restores the mode).
            if recipe.get('mode', preset.material_mode) == preset.material_mode and recipes_match(recipe,preset_recipe(preset)):
                return None
        except ValueError:continue
    name=(name_hint or '').strip() or 'New blend'
    return add_preset(scene,recipe,name,select=False)   # an automatic add must not steal the library selection


def auto_preset_for_applied_blend(context):
    """After Apply: file the placed blend in the library. Never raises; the blend is already placed."""
    scene=context.scene
    try:
        if not scene.sprotile_auto_presets:return None
        try:recipe=read_blend_recipe(context)
        except ValueError:
            recipe=dict(a=tile_as_sample(validated_blend_tile(scene.sprotile_blend_a)),
                        b=tile_as_sample(validated_blend_tile(scene.sprotile_blend_b)),
                        settings=validated_blend_settings(active_blend_settings(context)),
                        mode=scene.sprotile_blend_material_mode)
        return ensure_preset_for_recipe(scene,recipe,default_preset_name(context,recipe))
    except Exception as error:
        print('Sprotile: could not add the blend to the preset library:',error)
        return None


class MESH_OT_sprotile_save_preset(bpy.types.Operator):
    """Save a named recipe in this Blend file without linking existing patches to it"""
    bl_idname='mesh.sprotile_save_preset';bl_label='Save Blend Preset';bl_options={'UNDO'}
    source: EnumProperty(items=[('ACTIVE','Selected Blend','Save the blend on the active face'),('CURRENT','New Blend','Save the New Blend tiles and settings')],default='ACTIVE')
    ask_name: BoolProperty(default=False,options={'SKIP_SAVE'})
    preset_name: StringProperty(name='Name',options={'SKIP_SAVE'})
    def invoke(self,context,event):
        if not self.ask_name:return self.execute(context)
        try:recipe=read_blend_recipe(context)
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        self.source='ACTIVE'
        self.preset_name=default_preset_name(context,recipe)
        return context.window_manager.invoke_props_dialog(self,width=300)
    def draw(self,context):
        self.layout.prop(self,'preset_name')
    def execute(self,context):
        scene=context.scene
        try:
            if self.source=='ACTIVE':recipe=read_blend_recipe(context)
            else:recipe=dict(a=tile_as_sample(validated_blend_tile(scene.sprotile_blend_a)),
                             b=tile_as_sample(validated_blend_tile(scene.sprotile_blend_b)),
                             settings=validated_blend_settings(active_blend_settings(context)),
                             mode=scene.sprotile_blend_material_mode)
            name=(self.preset_name if self.ask_name else scene.sprotile_preset_name).strip()
            if not name:raise ValueError('Enter a name for the preset')
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        name=add_preset(scene,recipe,name)
        tag_redraw_view3d();self.report({'INFO'},'Saved preset: '+name);return {'FINISHED'}


class MESH_OT_sprotile_load_preset(bpy.types.Operator):
    """Prepare this recipe for Apply; existing faces are not changed"""
    bl_idname='mesh.sprotile_load_preset';bl_label='Load Preset';bl_options={'UNDO'}
    def execute(self,context):
        try:
            preset=selected_preset(context.scene);recipe=preset_recipe(preset)
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        write_blend_recipe(context.scene,recipe);reset_preview_interaction()
        self.report({'INFO'},'Loaded '+preset.name+'; select target faces and Apply');return {'FINISHED'}


class MESH_OT_sprotile_remove_preset(bpy.types.Operator):
    """Remove a saved recipe from the library; placed blends stay unchanged"""
    bl_idname='mesh.sprotile_remove_preset';bl_label='Remove Saved Preset';bl_options={'UNDO'}
    def execute(self,context):
        try:preset=selected_preset(context.scene)
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        key=preset_preview_key(context.scene,preset)
        context.scene.sprotile_blend_presets.remove(context.scene.sprotile_preset_index)
        context.scene.sprotile_preset_index=max(0,min(context.scene.sprotile_preset_index,len(context.scene.sprotile_blend_presets)-1))
        if state._preset_previews is not None and key in state._preset_previews:del state._preset_previews[key]
        tag_redraw_view3d();return {'FINISHED'}


class MESH_OT_sprotile_refresh_presets(bpy.types.Operator):
    bl_idname='mesh.sprotile_refresh_presets';bl_label='Redraw Preset Previews'
    # Redraw Preview sets nothing; don't let it inherit Redraw All.
    all_presets: BoolProperty(default=False,options={'SKIP_SAVE'})
    def execute(self,context):
        skipped=0
        try:presets=context.scene.sprotile_blend_presets if self.all_presets else [selected_preset(context.scene)]
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        for preset in presets:
            try:refresh_preset_preview(context.scene,preset)
            except (ValueError,RuntimeError,ReferenceError):skipped+=1
        tag_redraw_view3d()
        if skipped:self.report({'WARNING'},str(skipped)+' previews could not be refreshed; check their atlas images')
        return {'FINISHED'}


def matching_recipe_faces(obj,bm,recipe,reader=None):
    if reader is None:
        reader = RecipeSources()
    groups={};separate={};membership=bm.faces.layers.int.get(SHARED_PATCH_ID)
    bm.faces.ensure_lookup_table();bm.faces.index_update()
    for face in bm.faces:
        material=obj.material_slots[face.material_index].material if face.material_index<len(obj.material_slots) else None
        if material and material.get('sprotile_blend_material'):separate.setdefault(face.material_index,[]).append(face)
        elif material and material.get(SHARED_MATERIAL_TAG) and membership is not None and face[membership]>0:
            groups.setdefault(('SHARED',face[membership]),[]).append(face)
    candidates=list(groups.values())
    for faces in separate.values():candidates.extend(connected_face_patches(faces))
    matches=set();skipped=0
    for faces in candidates:
        try:actual=blend_recipe_from_face(obj,bm,faces[0],faces,reader)
        except (ValueError,RuntimeError):skipped+=1;continue
        if recipes_match(recipe,actual):matches.update(f.index for f in faces if not f.hide)
    return matches,skipped


class MESH_OT_sprotile_find_preset(bpy.types.Operator):
    """Select faces matching this recipe's images, layouts and transition settings"""
    bl_idname='mesh.sprotile_find_preset';bl_label='Find Preset Uses';bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return context.mode in {'OBJECT','EDIT_MESH'}
    def execute(self,context):
        try:recipe=preset_recipe(selected_preset(context.scene))
        except ValueError as error:self.report({'WARNING'},str(error));return {'CANCELLED'}
        editing=context.mode=='EDIT_MESH'
        objects=edit_mesh_objects(context) if editing else [o for o in context.view_layer.objects if o.type=='MESH' and o.library is None and o.data.library is None and not o.hide_select and o.visible_get(view_layer=context.view_layer)]
        found={};matched_objects=[];skipped=0;scanned={}
        reader = RecipeSources()
        for obj in objects:
            materials=tuple(slot.material for slot in obj.material_slots)
            if not any(mat and (mat.get('sprotile_blend_material') or mat.get(SHARED_MATERIAL_TAG)) for mat in materials):continue
            scan_key=(obj.data.as_pointer(),tuple(mat.as_pointer() if mat else 0 for mat in materials))
            if scan_key in scanned:
                matches,bad=scanned[scan_key]
                if matches:found.setdefault(obj.data.as_pointer(),set()).update(matches);matched_objects.append(obj)
                continue
            bm=bmesh.from_edit_mesh(obj.data) if obj.data.is_editmode else bmesh.new()
            temporary=not obj.data.is_editmode
            try:
                if temporary:bm.from_mesh(obj.data)
                matches,bad=matching_recipe_faces(obj,bm,recipe,reader);skipped+=bad
                scanned[scan_key]=(matches,bad)
                if matches:found.setdefault(obj.data.as_pointer(),set()).update(matches);matched_objects.append(obj)
            finally:
                if temporary:bm.free()
        if not found:
            self.report({'WARNING'},'No exact matching blend found'+(' ('+str(skipped)+' unreadable groups skipped)' if skipped else ''));return {'CANCELLED'}
        if not editing:
            before=list(context.selected_objects);active=context.view_layer.objects.active
            bpy.ops.object.select_all(action='DESELECT')
            for obj in matched_objects:obj.select_set(True)
            context.view_layer.objects.active=matched_objects[0]
            try:bpy.ops.object.mode_set(mode='EDIT')
            except RuntimeError:
                for obj in matched_objects:obj.select_set(False)
                for obj in before:obj.select_set(True)
                context.view_layer.objects.active=active
                self.report({'WARNING'},'Matching objects could not enter Edit Mode');return {'CANCELLED'}
        context.tool_settings.mesh_select_mode=(False,False,True)
        seen=set()
        for obj in edit_mesh_objects(context):
            key=obj.data.as_pointer()
            if key in seen:continue
            seen.add(key);bm=bmesh.from_edit_mesh(obj.data);bm.faces.ensure_lookup_table();bm.faces.index_update()
            for face in bm.faces:face.select_set(face.index in found.get(key,set()))
            bm.select_mode={'FACE'};bm.select_flush_mode()
            bm.faces.active=next((f for f in bm.faces if f.select),None)
            bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
        tag_redraw_view3d();self.report({'INFO'},'Selected '+str(sum(len(v) for v in found.values()))+' matching faces'+(' • '+str(skipped)+' unreadable groups skipped' if skipped else ''))
        return {'FINISHED'}


class VIEW3D_PT_sprotile_presets(bpy.types.Panel):
    bl_label='Blend Preset Library';bl_idname='VIEW3D_PT_sprotile_presets'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='Sprotile';bl_parent_id='VIEW3D_PT_sprotile_blend'
    @classmethod
    def poll(cls,context):return feature_enabled(context,'show_library')
    def draw(self,context):
        layout=self.layout;scene=context.scene
        layout.label(text='Saved in this Scene',icon='FILE_BLEND')
        if scene.sprotile_blend_presets:
            layout.template_list('SPROTILE_UL_blend_presets','',scene,'sprotile_blend_presets',scene,'sprotile_preset_index',rows=3)
            row=layout.row(align=True);row.operator('mesh.sprotile_load_preset',text='Load');row.operator('mesh.sprotile_find_preset',text='Find Uses')
            header,body=layout.panel('sprotile_preset_preview',default_closed=True);header.label(text='Larger Preview')
            if body:
                try:
                    icon=preset_icon(scene,selected_preset(scene))
                    if icon:body.template_icon(icon_value=icon.icon_id,scale=5)
                except ValueError:pass
            header,manage=layout.panel('sprotile_preset_manage',default_closed=True);header.label(text='Save / Manage')
        else:manage=layout
        if manage:
            manage.prop(scene,'sprotile_preset_name',text='Name')
            manage.prop(scene,'sprotile_auto_presets')
            row=manage.row(align=True)
            row.operator('mesh.sprotile_save_preset',text='Save Selected Blend').source='ACTIVE'
            row.operator('mesh.sprotile_save_preset',text='Save New Blend').source='CURRENT'
            if scene.sprotile_blend_presets:
                row=manage.row(align=True);row.operator('mesh.sprotile_refresh_presets',text='Redraw Preview');row.operator('mesh.sprotile_remove_preset',text='Remove')
                manage.operator('mesh.sprotile_refresh_presets',text='Redraw All Previews').all_presets=True
            manage.label(text='Load prepares a copy; Apply places it')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import feature_enabled, RECTANGLE_CORNER_LAYER, _clipboard_preview_pending, _clipboard_previews
from .draw import tag_redraw_view3d
from .tiles import (
    apply_tile_transform, atlas_for_material_index, edit_mesh_objects, get_tile_dims, get_tile_transform,
    get_uv_inset, image_size, is_editable_mesh, tile_uv_bounds,
)
from .mapping import (
    connected_face_patches, map_selected_patches_to_active, patch_loop_uvs, patch_tile_uvs, patch_triangles,
    plain_paint_binding,
)
from .picker import get_region_3d, is_quad_view
from .blend_settings import (
    BlendSettings, EDGE_STYLES, SHAPED_EDGES, SHARED_MATERIAL_TAG, SHARED_PATCH_ID, blend_axis,
    read_rectangle_corners, validated_blend_settings,
)
from .blend_plan import validated_blend_tile
from .blend_shader import shader_factor, shared_edge_mapper, shared_factor_mapper
from .blend_shared import active_shared_patch
from .blend_recipes import (
    RecipeSources, active_blend_settings, affine_uv, blend_recipe_from_face, read_blend_recipe,
    sample_uv_bounds, write_blend_recipe,
)
from .blend_ops import reset_preview_interaction
from .blend_storage import existing_uv_reader
