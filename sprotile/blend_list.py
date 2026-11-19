"""Blends on This Mesh: the cached overview, the list, Select Faces and its panel.
"""

import bpy
import bmesh
import time
from array import array
from bpy.app.handlers import persistent
from bpy.props import IntProperty, BoolProperty


# ---------------------------------------------------------------------------
# Blends on This Mesh: every shared blend patch, its style and faces
# ---------------------------------------------------------------------------

_draw_cache = {}
DRAW_CACHE_SECONDS = 2.0


def draw_cached(kind, mesh, compute):
    """A per-mesh result for panel drawing. Sidebar redraws (hovering, scrolling) reuse it; any data change,
    Undo or Redo drops it (_sprotile_data_changed), and it expires after DRAW_CACHE_SECONDS as a backstop."""
    key = (kind, mesh.as_pointer(), mesh.is_editmode)
    now = time.monotonic()
    hit = _draw_cache.get(key)
    if hit is not None and now - hit[0] < DRAW_CACHE_SECONDS:
        return hit[1]
    if len(_draw_cache) > 32:
        _draw_cache.clear()
    value = compute()
    _draw_cache[key] = (now, value)
    return value


@persistent
def _sprotile_data_changed(*args):
    """After a dependency-graph update, Undo or Redo: drop what was worked out from the old data."""
    _draw_cache.clear()
    release_held_bmeshes(only_invalid=True)
    # A dependency-graph update names what changed; Undo and Redo do not, so they forget every kept occluder.
    forget_occluders(next((item for item in args if isinstance(item, bpy.types.Depsgraph)), None))


@persistent
def _sprotile_frame_changed(*_args):
    forget_occluders()   # animated objects move and deform between frames


_CHANGE_HANDLERS = ('depsgraph_update_post', 'undo_post', 'redo_post')


PATCH_STYLE_ICONS = {'PLAIN': 'IPO_LINEAR', 'GRASS': 'STRANDS', 'SNOW': 'FREEZE', 'PATH': 'CURVE_PATH',
                     'OVERLAY': 'IMAGE_RGB_ALPHA', 'PATH_BRUSH': 'BRUSH_DATA'}
_patch_overview_cache = {}


def patch_style_from_flags(bits):
    style = {16: 'GRASS', 32: 'SNOW', OVERLAY_FLAG: 'OVERLAY'}.get(bits & (48 | OVERLAY_FLAG), 'PLAIN')
    return 'PATH' if bits & PATH_FLAG and style != 'OVERLAY' else style


def blend_patch_overview(mesh):
    """{patch id: (face count, hidden count, style)} from one pass over the faces."""
    result = {}
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
        flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
        if patch is None:
            return result
        for face in bm.faces:
            identifier = face[patch]
            if identifier > 0:
                count, hidden, bits = result.get(identifier, (0, 0, face[flags] if flags is not None else 0))
                result[identifier] = (count + 1, hidden + face.hide, bits)
    else:
        patch = mesh.attributes.get(SHARED_PATCH_ID)
        if patch is None or patch.domain != 'FACE' or len(patch.data) != len(mesh.polygons):
            return result
        count = len(mesh.polygons)
        ids = array('i', [0]) * count
        patch.data.foreach_get('value', ids)
        hide = array('b', [0]) * count
        mesh.polygons.foreach_get('hide', hide)
        flags = mesh.attributes.get(SHARED_PATCH_FLAGS)
        bits = array('i', [0]) * count
        if flags is not None and len(flags.data) == count:
            flags.data.foreach_get('value', bits)
        for identifier, hidden, value in zip(ids, hide, bits):
            if identifier > 0:
                faces, hidden_faces, first = result.get(identifier, (0, 0, value))
                result[identifier] = (faces + 1, hidden_faces + hidden, first)
    return {identifier: (count, hidden, patch_style_from_flags(bits))
            for identifier, (count, hidden, bits) in result.items()}


def path_groups(mesh):
    """{patch id: the id that stands for its path} for the patches the Path brush made. Path faces
    (PATH_PAINT_ATTRIBUTE > 0) that touch at a corner form one path, whatever their patch ids (a seam made by Blend
    This Seam is grouped the same way, apart from any path it touches); the lowest patch id
    with a record stands for it, so the list shows one row per path instead of one per edge face."""
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        paint, patch = bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE), bm.faces.layers.int.get(SHARED_PATCH_ID)
        if paint is None or patch is None:
            return {}
        # BMVerts themselves: vertices made in this Edit Mode session can all have index -1.
        pairs = [(face[patch], tuple((vert, face[paint] == SEAM) for vert in face.verts)) for face in bm.faces
                 if face[paint] and face[patch] > 0]
    else:
        paint, patch = mesh.attributes.get(PATH_PAINT_ATTRIBUTE), mesh.attributes.get(SHARED_PATCH_ID)
        count = len(mesh.polygons)
        if (paint is None or patch is None or paint.domain != 'FACE' or patch.domain != 'FACE'
                or len(paint.data) != count or len(patch.data) != count):
            return {}
        flags, ids = array('i', [0]) * count, array('i', [0]) * count
        paint.data.foreach_get('value', flags)
        patch.data.foreach_get('value', ids)
        pairs = [(ids[i], tuple((vertex, flags[i] == SEAM) for vertex in mesh.polygons[i].vertices))
                 for i in range(count) if flags[i] and ids[i] > 0]
    if not pairs:
        return {}
    parent = list(range(len(pairs)))

    def root(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item
    owner = {}
    for position, (_identifier, vertices) in enumerate(pairs):
        for vertex in vertices:
            other = owner.setdefault(vertex, position)
            a, b = root(position), root(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    components = {}
    for position, (identifier, _vertices) in enumerate(pairs):
        components.setdefault(root(position), set()).add(identifier)
    recorded = {record.patch_id for record in mesh.sprotile_blend_patches}
    result = {}
    for identifiers in components.values():
        listed = [identifier for identifier in identifiers if identifier in recorded]
        if listed:
            lead = min(listed)
            for identifier in identifiers:
                result.setdefault(identifier, lead)   # an id shared by two separate paths stays with the first
    return result


def live_patch_records(mesh, overview=None):
    """The record each live patch uses (the first with its id, as Edit Active does)."""
    overview = blend_patch_overview(mesh) if overview is None else overview
    seen, records = set(), []
    for index, record in enumerate(mesh.sprotile_blend_patches):
        if record.patch_id in overview and record.patch_id not in seen:
            seen.add(record.patch_id)
            records.append((index, record))
    return records


def patch_list_visibility(mesh, name_filter='', overview=None, paths=None):
    """Which patch records the list shows: live ones, matching the name filter, and for a path only the record that
    stands for it. Caches counts for drawing (a path's row counts all its faces)."""
    overview = blend_patch_overview(mesh) if overview is None else overview
    paths = path_groups(mesh) if paths is None else paths
    if paths:
        overview = dict(overview)
        totals = {}
        for identifier, lead in paths.items():
            count, hidden, _style = overview.get(identifier, (0, 0, 'PLAIN'))
            total = totals.get(lead, (0, 0))
            totals[lead] = (total[0] + count, total[1] + hidden)
        for lead, (count, hidden) in totals.items():
            overview[lead] = (count, hidden, 'PATH_BRUSH')
    if len(_patch_overview_cache) > 16:
        _patch_overview_cache.clear()   # only the meshes being drawn need an entry
    _patch_overview_cache[mesh.as_pointer()] = overview
    live = {index for index, _record in live_patch_records(mesh, overview)}
    needle = (name_filter or '').lower()
    return [index in live and paths.get(record.patch_id, record.patch_id) == record.patch_id
            and (not needle or needle in record.name.lower())
            for index, record in enumerate(mesh.sprotile_blend_patches)]


class SPROTILE_UL_blend_patches(bpy.types.UIList):
    """Shared blend patches on the active mesh; records kept only for Undo are hidden"""
    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        overview = draw_cached('overview', data, lambda: blend_patch_overview(data))
        paths = draw_cached('paths', data, lambda: path_groups(data))
        flags = [self.bitflag_filter_item if shown else 0
                 for shown in patch_list_visibility(data, self.filter_name, overview, paths)]
        order = bpy.types.UI_UL_list.sort_items_by_name(items, 'name') if self.use_filter_sort_alpha else []
        return flags, order

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        count, hidden, style = _patch_overview_cache.get(data.as_pointer(), {}).get(item.patch_id, (0, 0, 'PLAIN'))
        row = layout.row(align=True)
        row.label(text='', icon=PATCH_STYLE_ICONS[style])
        row.prop(item, 'name', text='', emboss=False)
        label = f'{count} face' + ('s' if count != 1 else '')
        if style == 'PATH_BRUSH':
            label = ('seam, ' if item.name.startswith('Seam ') else 'path, ') + label
        if hidden:
            label += f' ({hidden} hidden)'
        row.label(text=label)


def selected_patch_record(context):
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    records = obj.data.sprotile_blend_patches
    index = context.scene.sprotile_patch_list_index
    return records[index] if 0 <= index < len(records) else None


class MESH_OT_sprotile_separate_blend_copies(bpy.types.Operator):
    """Give each copy of a duplicated or joined blend its own entry, so changing one no longer changes the others"""
    bl_idname = 'mesh.sprotile_separate_blend_copies'
    bl_label = 'Separate Blend Copies'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        count, seen = 0, set()
        for obj in edit_mesh_objects(context):
            if obj.data.as_pointer() in seen or obj.data.library is not None:
                continue
            seen.add(obj.data.as_pointer())
            count += separate_selected_blend_copies(obj.data, bmesh.from_edit_mesh(obj.data))
        if not count:
            self.report({'INFO'}, 'The selected blends have no copies')
            return {'CANCELLED'}
        self.report({'INFO'}, ('1 copy' if count == 1 else f'{count} copies') + ' separated; each now has its own row')
        return {'FINISHED'}


class MESH_OT_sprotile_select_blend_patch(bpy.types.Operator):
    """Select every face of this blend patch, make one of them active and frame them, so Edit Active shows the blend"""
    bl_idname = 'mesh.sprotile_select_blend_patch'
    bl_label = 'Select Blend Faces'
    bl_options = {'REGISTER', 'UNDO'}
    patch_id: IntProperty(name='Patch', default=0, min=0, options={'SKIP_SAVE'})
    extend: BoolProperty(name='Extend', description='Add to the current selection', default=False, options={'SKIP_SAVE'})
    frame: BoolProperty(name='Frame', description='Zoom the view to the selected faces', default=True, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.library is None and obj.data.library is None

    def execute(self, context):
        obj = context.active_object
        patch_id = self.patch_id
        if not patch_id:
            record = selected_patch_record(context)
            patch_id = record.patch_id if record is not None else 0
        if not patch_id:
            self.report({'WARNING'}, 'Choose a blend in the list first')
            return {'CANCELLED'}
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.faces.layers.int.get(SHARED_PATCH_ID)
        if layer is None:
            self.report({'WARNING'}, 'This mesh has no shared blends')
            return {'CANCELLED'}
        # A path's row stands for every patch the Path brush made for that path.
        paths = path_groups(obj.data)
        lead = paths.get(patch_id)
        wanted = {identifier for identifier, owner in paths.items() if owner == lead} if lead is not None else {patch_id}
        # Copies made by duplicating or joining get their own rows, so this selects only the listed blend.
        separated = separate_blend_copies(obj.data, bm, [patch_id]) if obj.data.library is None and lead is None else 0
        if not self.extend:
            for element in (*bm.verts, *bm.edges, *bm.faces):
                element.select = False
        first, selected, hidden = None, 0, 0
        for face in bm.faces:
            if face[layer] not in wanted:
                continue
            if face.hide:
                hidden += 1
                continue
            face.select_set(True)
            selected += 1
            if first is None or (face[layer] == patch_id and first[layer] != patch_id):
                first = face
        if first is None:
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            self.report({'WARNING'}, f'All {hidden} faces of this blend are hidden; reveal them with Alt+H'
                        if hidden else 'No faces use this blend any more')
            return {'CANCELLED'}
        bm.faces.active = first
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        context.scene.sprotile_blend_settings_source = 'ACTIVE'
        if self.frame and context.area is not None and context.area.type == 'VIEW_3D':
            region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if region is not None:
                with context.temp_override(area=context.area, region=region):
                    bpy.ops.view3d.view_selected()
        message = f'{selected} face' + ('s' if selected != 1 else '') + ' selected'
        if separated:
            message += '; ' + (f'{separated} copies' if separated != 1 else '1 copy') + ' now listed separately'
        self.report({'INFO'}, message + (f'; {hidden} hidden' if hidden else ''))
        return {'FINISHED'}


class VIEW3D_PT_sprotile_blend_list(bpy.types.Panel):
    bl_label = 'Blends on This Mesh'
    bl_idname = 'VIEW3D_PT_sprotile_blend_list'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sprotile'
    bl_parent_id = 'VIEW3D_PT_sprotile_blend'
    @classmethod
    def poll(cls, context):
        return feature_enabled(context, 'show_blends')


    def draw(self, context):
        layout, scene = self.layout, context.scene
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            layout.label(text='Select a mesh', icon='INFO')
            return
        mesh = obj.data
        if not live_patch_records(mesh, draw_cached('overview', mesh, lambda: blend_patch_overview(mesh))):
            layout.label(text='No shared blends on this mesh', icon='INFO')
        else:
            layout.template_list('SPROTILE_UL_blend_patches', '', mesh, 'sprotile_blend_patches',
                                 scene, 'sprotile_patch_list_index', rows=4)
            row = layout.row(align=True)
            row.operator('mesh.sprotile_select_blend_patch', text='Select Faces', icon='RESTRICT_SELECT_OFF')
            add = row.operator('mesh.sprotile_select_blend_patch', text='Add', icon='ADD')
            add.extend = True
            add.frame = False
            layout.label(text='Double-click a name to rename it', icon='INFO')
        separate = sorted({m.name for m in mesh.materials if m is not None and m.get('sprotile_blend_material')})
        if separate:
            layout.label(text=f'{len(separate)} Separate Material blend(s):', icon='MATERIAL')
            for name in separate[:6]:
                layout.label(text='  ' + name)
            layout.label(text='Select them in Material Properties')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .blend_settings import OVERLAY_FLAG, PATH_FLAG, SHARED_PATCH_FLAGS, SHARED_PATCH_ID
from .tiles import edit_mesh_objects, is_editable_mesh
from .blend_shared import separate_blend_copies, separate_selected_blend_copies
from .core import feature_enabled, release_held_bmeshes
from .path_brush import PATH_PAINT_ATTRIBUTE
from .seam import SEAM
from .brush import forget_occluders
