"""Blend operators: capture and swap tiles, Apply, Remove Blend and the Feather menu.
"""

import bpy
import bmesh
from bpy.props import FloatProperty, EnumProperty
from . import state


def invalidate_clipboard_slot(scene, slot):
    key = (scene.session_uid, slot)
    _clipboard_previews.pop(key, None)
    _clipboard_preview_pending.pop(key, None)


def reset_preview_interaction():
    """A capture starts a new picker interaction, retaining its zoom and pan."""
    preview = state._preview_operator
    if preview is not None:
        preview.is_panning = False
        preview.panel_drag = None
        preview.last_click_time = 0.0
        preview.last_click_tile = (-1, -1)
        preview.hover_col = preview.hover_row = -1
    tag_redraw_view3d()


class MESH_OT_sprotile_capture_blend_tile(bpy.types.Operator):
    """Remember the atlas tile currently selected in Sprotile, including orientation and inset"""
    bl_idname = 'mesh.sprotile_capture_blend_tile'
    bl_label = 'Capture Blend Tile'
    # Captures are bookmarks, not parameterised redo operations. A last-op
    # popup overlays the picker and changing its slot can recapture a new tile.
    bl_options = {'UNDO'}
    slot: EnumProperty(items=[('A', 'A', 'Texture A'), ('B', 'B', 'Texture B')], default='A')

    def execute(self, context):
        try:
            capture_blend_tile(context.scene, self.slot, resolve_atlas_image(context.active_object))
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        reset_preview_interaction()
        self.report({'INFO'}, 'Captured texture ' + self.slot)
        return {'FINISHED'}


class MESH_OT_sprotile_swap_blend_tiles(bpy.types.Operator):
    """Exchange the two captured texture choices"""
    bl_idname = 'mesh.sprotile_swap_blend_tiles'
    bl_label = 'Swap A and B'
    bl_options = {'UNDO'}

    def execute(self, context):
        a, b = context.scene.sprotile_blend_a, context.scene.sprotile_blend_b
        first = {name: tuple(getattr(a,name)) if name=='sample_uv' else getattr(a, name) for name in BLEND_TILE_FIELDS}
        second = {name: tuple(getattr(b,name)) if name=='sample_uv' else getattr(b, name) for name in BLEND_TILE_FIELDS}
        for name in BLEND_TILE_FIELDS:
            setattr(a, name, second[name])
            setattr(b, name, first[name])
        return {'FINISHED'}


class MESH_OT_sprotile_apply_blend(bpy.types.Operator):
    """Blend A into B across the selected faces; Shared Material keeps the atlas material and its Tint"""
    bl_idname = 'mesh.sprotile_apply_blend'
    bl_label = 'Apply Blend'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not is_editable_mesh(context.active_object):
            cls.poll_message_set('Enter Edit Mode on a mesh first')   # shown in the greyed-out button's tooltip
            return False
        return True

    def execute(self, context):
        try:
            applied = apply_texture_blend(context)
        except (ValueError, RuntimeError, MemoryError) as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not applied:
            self.report({'WARNING'}, 'Select visible faces in Edit Mode first')
            return {'CANCELLED'}
        auto_preset_for_applied_blend(context)
        self.report({'INFO'}, 'Blend applied; use Material Preview and adjust the active blend controls')
        return {'FINISHED'}


class MESH_OT_sprotile_use_one_atlas(bpy.types.Operator):
    """Make every atlas sampler of the shared blend material read one image, when the images are the same size"""
    bl_idname = 'mesh.sprotile_use_one_atlas'
    bl_label = 'Use One Atlas'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        materials = [slot.material for slot in context.active_object.material_slots
                     if slot.material is not None and slot.material.get(SHARED_MATERIAL_TAG)]
        materials = list(dict.fromkeys(materials))
        if not materials:
            self.report({'WARNING'}, 'The active object has no shared blend material')
            return {'CANCELLED'}
        groups = []
        for material in materials:
            if material.library is not None:
                self.report({'WARNING'}, f"Material '{material.name}' is linked and cannot be edited")
                return {'CANCELLED'}
            nodes = shared_atlas_nodes(material)
            if nodes:
                groups.append((material, nodes))
        target = scene.sprotile_blend_a.image
        if target is None:
            original = next((node for _material, nodes in groups for node in nodes
                             if node.get('sprotile_shared_role') == 'original' and node.image is not None), None)
            target = original.image if original else None
        if target is None:
            self.report({'WARNING'}, 'No atlas image to use; capture a New Blend A tile first')
            return {'CANCELLED'}
        # An image whose file is missing has size (0, 0): nothing to protect, so it is always replaceable.
        wrong = sorted({node.image.name for _material, nodes in groups for node in nodes
                        if node.image is not None and min(node.image.size) > 0
                        and tuple(node.image.size) != tuple(target.size)})
        if wrong:
            self.report({'WARNING'}, f"Not changed: {', '.join(wrong)} is a different size from '{target.name}' "
                        f"({target.size[0]}x{target.size[1]}); a different-sized atlas needs its tiles captured again")
            return {'CANCELLED'}
        changed = 0
        for _material, nodes in groups:
            for node in nodes:
                if node.image != target:
                    node.image = target
                    changed += 1
        for tile in (scene.sprotile_blend_a, scene.sprotile_blend_b):
            image = tile.image
            if image is not None and image != target and (min(image.size) == 0 or tuple(image.size) == tuple(target.size)):
                tile.image = target
        tag_redraw_view3d()
        self.report({'INFO'}, f"Atlas '{target.name}' is now used by {changed} node{'s' if changed != 1 else ''}")
        return {'FINISHED'}


class MESH_OT_sprotile_remove_blend(bpy.types.Operator):
    """Restore selected blended faces to their recorded previous material; retain reusable UV maps"""
    bl_idname = 'mesh.sprotile_remove_blend'
    bl_label = 'Remove Blend from Selection'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not is_editable_mesh(context.active_object):
            cls.poll_message_set('Enter Edit Mode on a mesh first')   # shown in the greyed-out button's tooltip
            return False
        return True

    def execute(self, context):
        changed, skipped_slots, readded, seen, conflict = False, False, False, set(), None
        owners = {}   # history key -> material, resolved once per Remove
        upgrade_file_blends()
        for obj in edit_mesh_objects(context):
            if obj.data.as_pointer() in seen:
                continue
            seen.add(obj.data.as_pointer())
            bm = bmesh.from_edit_mesh(obj.data)
            previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
            patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
            path_mark = bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE)
            factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
            stored = edit_blend_layers(obj.data, bm)
            for face in bm.faces:
                if not face.select or face.hide:
                    continue
                material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
                separate = bool(material and material.get('sprotile_blend_material'))
                if patch is not None and face[patch] and factor is not None and not separate:
                    if material and material.get(SHARED_MATERIAL_TAG):
                        restore_base_tile(face, stored)   # the tile the face showed before its blend
                    # Otherwise the face was given another material by hand and already shows that: it only
                    # stops being listed as a blend.
                    face[patch] = 0
                    if path_mark is not None:
                        face[path_mark] = 0
                    for loop in face.loops:
                        loop[factor].uv = (0.0, 0.0)
                    if previous is not None:
                        face[previous] = 0
                    changed = True
                    continue
                if previous is None or not face[previous]:
                    continue
                old_slot = history_slot(obj, face[previous], owners)
                if old_slot < 0 and material and material.get('sprotile_blend_material'):
                    # The material it had is no longer on the object (its slot was removed): give it a slot again.
                    original = history_material(face[previous], obj.data, owners)
                    if original is not None and not original.get('sprotile_blend_material'):
                        obj.data.materials.append(original)
                        old_slot = len(obj.data.materials) - 1
                        readded = True
                    elif original is None:
                        conflict = history_conflict(obj, face[previous]) or conflict
                if material and material.get('sprotile_blend_material') and 0 <= old_slot < len(obj.material_slots):
                    target = obj.material_slots[old_slot].material
                    if target is not None and target == material:
                        # A history from before 5.15.1 stores a slot number, and the slots were reordered since: the
                        # "previous" slot now holds the very blend material being removed. A blend layered over an
                        # earlier Separate blend legitimately points at a blend material, so only this case is refused.
                        skipped_slots = True
                        continue
                    restore_base_tile(face, stored)
                    face.material_index = old_slot
                    if path_mark is not None:
                        face[path_mark] = 0
                    face[previous] = 0
                    changed = True
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        tag_redraw_view3d()
        if readded:
            self.report({'INFO'}, 'The material these faces had before their blend was no longer on the object; '
                        'it has a slot again')
        if skipped_slots:
            self.report({'WARNING'}, 'Some Separate Material faces were left alone: their original material slot now holds '
                        'a blend material (slots were reordered). Assign the atlas material to them by hand')
        if conflict:
            self.report({'WARNING'}, 'Some faces were left alone. ' + conflict)
        elif not changed:
            self.report({'WARNING'}, 'No selected blended faces to restore')
        return {'FINISHED'} if changed else {'CANCELLED'}


class MESH_OT_sprotile_feather(bpy.types.Operator):
    """Set a small, smooth fade band; existing textures and shape stay in place"""
    bl_idname='mesh.sprotile_feather';bl_label='Set Feather';bl_options={'REGISTER','UNDO'}
    percent: FloatProperty(default=2,min=0,max=200,options={'HIDDEN','SKIP_SAVE'})
    def execute(self,context):
        controls=active_blend_settings(context)
        if abs(controls.feather_percent-self.percent)>1e-6:controls.feather_percent=self.percent
        if not controls.smooth:controls.smooth=True
        tag_redraw_view3d();return {'FINISHED'}


class VIEW3D_MT_sprotile_feather(bpy.types.Menu):
    bl_label='Feather Presets'
    def draw(self,context):
        for value,label in ((0,'Hard edge — 0%'),(2,'Small fade — 2%'),(5,'Soft edge — 5%'),(10,'Broad fade — 10%')):
            self.layout.operator('mesh.sprotile_feather',text=label).percent=value


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import _clipboard_preview_pending, _clipboard_previews
from .draw import tag_redraw_view3d
from .tiles import edit_mesh_objects, is_editable_mesh, resolve_atlas_image
from .blend_settings import (
    BLEND_ORIGINAL_MATERIAL, BLEND_TILE_FIELDS, PATH_PAINT_ATTRIBUTE, SHARED_FACTOR_UV, SHARED_MATERIAL_TAG, SHARED_PATCH_ID,
)
from .blend_plan import capture_blend_tile
from .blend_recipes import active_blend_settings, apply_texture_blend
from .blend_shader import shared_atlas_nodes
from .presets import auto_preset_for_applied_blend
from .blend_storage import (edit_blend_layers, history_conflict, history_material, history_slot, restore_base_tile,
                            upgrade_file_blends)
