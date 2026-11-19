"""Set up an atlas on selected, untextured faces of the active edit mesh."""

from pathlib import Path

import bmesh
import bpy
from bpy.props import StringProperty

from .blend_settings import BLEND_ORIGINAL_MATERIAL, SHARED_PATCH_ID
from .blend_storage import face_is_blended
from .core import MATERIALS_ALWAYS_USE_NODES
from .draw import tag_redraw_view3d
from .tiles import atlas_for_material_index, image_from_material, image_size, is_editable_mesh


def untextured_selection(obj, bm):
    selected = [face for face in bm.faces if face.select and not face.hide]
    if not selected:
        raise ValueError('Select the faces that need an atlas on the active mesh')
    patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
    previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
    available = {i for i in {f.material_index for f in selected} if atlas_for_material_index(obj, i) is None}
    faces = [f for f in selected if f.material_index in available and not face_is_blended(f, patch, previous)]
    if not faces:
        raise ValueError('These faces already have a texture or blend. Open Picker to map a tile')
    if obj.data.users > 1:
        # A face index is shared by all instances, but an object-linked material can differ between them.
        slots = {f.material_index for f in faces}
        for instance in bpy.data.objects:
            if instance.type == 'MESH' and instance.data == obj.data and any(
                    i < len(instance.material_slots) and instance.material_slots[i].link == 'OBJECT' for i in slots):
                raise ValueError('This mesh has linked instances with material overrides. Make it single-user first')
    return faces, len(selected) - len(faces)


def new_atlas_material(image):
    material = bpy.data.materials.new(f'{bpy.path.display_name_from_filepath(image.name)} Atlas')
    try:
        if not MATERIALS_ALWAYS_USE_NODES:
            material.use_nodes = True
        nodes = material.node_tree.nodes
        shader = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')
        texture = nodes.new('ShaderNodeTexImage')
        texture.image = image
        texture.interpolation = 'Closest'
        texture.location = (shader.location.x - 340, shader.location.y)
        material.node_tree.links.new(texture.outputs['Color'], shader.inputs['Base Color'])
        nodes.active = texture
        material['sprotile_atlas_material'] = True
    except Exception:
        bpy.data.materials.remove(material)
        raise
    return material


def assign_atlas(obj, bm, faces, image):
    mesh = obj.data
    previous_slots = [(f, f.material_index) for f in faces]
    old_index, old_active = obj.active_material_index, bm.faces.active
    original_count = len(mesh.materials)
    # Reuse only a setup material already on this mesh. An artist's other material may use the same image.
    slot = next((i for i, s in enumerate(obj.material_slots) if s.link == 'DATA' and s.material and
                 s.material.get('sprotile_atlas_material') and image_from_material(s.material) == image), None)
    material = None
    new_uv = None
    try:
        if slot is None:
            material = new_atlas_material(image)
            if not original_count and len(faces) < len(bm.faces):
                mesh.materials.append(None)  # unselected faces already use slot zero
            slot = len(mesh.materials)
            mesh.materials.append(material)
        if not bm.loops.layers.uv:
            new_uv = bm.loops.layers.uv.new('UVMap')
        for face in faces:
            face.material_index = slot
        if old_active not in faces:
            bm.faces.active = faces[0]
        obj.active_material_index = slot
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    except Exception:
        for face, index in previous_slots:
            face.material_index = index
        bm.faces.active = old_active
        if new_uv is not None:
            bm.loops.layers.uv.remove(new_uv)
        while len(mesh.materials) > original_count:
            mesh.materials.pop(index=len(mesh.materials) - 1)
        obj.active_material_index = old_index
        if material is not None and not material.users:
            bpy.data.materials.remove(material)
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        raise


class MESH_OT_sprotile_setup_atlas(bpy.types.Operator):
    """Choose an image and assign a nearest-filtered atlas material to selected untextured faces on the active mesh"""
    bl_idname = 'mesh.sprotile_setup_atlas'
    bl_label = 'Set Up Atlas'
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    filter_glob: StringProperty(default='*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.tif;*.tiff;*.webp;*.exr',
                                options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return is_editable_mesh(obj) and obj.data.is_editable

    def invoke(self, context, event):
        try:
            untextured_selection(context.active_object, bmesh.from_edit_mesh(context.active_object.data))
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        self._mesh_uid = context.active_object.data.session_uid
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.active_object
        if not self.poll(context):
            self.report({'WARNING'}, 'Select an editable mesh and enter Edit Mode')
            return {'CANCELLED'}
        if getattr(self, '_mesh_uid', obj.data.session_uid) != obj.data.session_uid:
            self.report({'WARNING'}, 'The active mesh changed. Choose Set Up Atlas on the intended mesh')
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        image = None
        try:
            faces, skipped = untextured_selection(obj, bm)
            path = bpy.path.abspath(self.filepath)
            if not path or not Path(path).is_file():
                raise ValueError('Choose an atlas image file')
            existing = {item.as_pointer() for item in bpy.data.images}
            image = bpy.data.images.load(path, check_existing=True)
            if min(image_size(image)) <= 0:
                raise ValueError('Blender could not read pixels from that image')
            assign_atlas(obj, bm, faces, image)
        except (OSError, RuntimeError, ValueError, MemoryError) as error:
            if image is not None and image.as_pointer() not in existing and not image.users:
                bpy.data.images.remove(image)
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        tag_redraw_view3d()
        message = f'{image.name} assigned to {len(faces)} face(s). Open Picker to choose a tile'
        if skipped:
            message += f' ({skipped} already textured)'
        self.report({'INFO'}, message)
        return {'FINISHED'}
