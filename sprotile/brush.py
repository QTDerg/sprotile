"""The paint brush: paint targets, scene occlusion, the brush operator and its tool.
"""

import base64
import os

import bpy
import bmesh
import gpu
import traceback
import mathutils
from bpy.props import FloatProperty
from collections import deque
from mathutils.bvhtree import BVHTree
from gpu_extras.batch import batch_for_shader
from . import state


# ---------------------------------------------------------------------------
# Brush paint targets
#
# One per mesh in Edit Mode.  The BVH is built from the BMesh itself, so hits
# map straight back to editable faces - unlike scene.ray_cast, whose polygon
# index refers to the evaluated (post-modifier) mesh and silently points at the
# wrong face as soon as a Mirror/Array/Subsurf modifier is in the stack.
# ---------------------------------------------------------------------------

def hit_within_face_bounds(point, face):
    """Cheap sanity check that a BVH hit belongs to the face it reported.

    Bounding box only - a plane test would reject legitimate hits on the
    slightly non-planar quads that tiling meshes are full of.
    """
    try:
        verts = [v.co for v in face.verts]
        if not verts:
            return False
        size = 0.0
        for axis in range(3):
            size = max(size, max(c[axis] for c in verts) - min(c[axis] for c in verts))
        tol = max(1e-4, size * 1e-3)
        for axis in range(3):
            lo = min(c[axis] for c in verts) - tol
            hi = max(c[axis] for c in verts) + tol
            if not (lo <= point[axis] <= hi):
                return False
        return True
    except Exception:
        return True


def visible_face_bvh(bm, world_matrix=None):
    """(BVH of the visible faces of an edit BMesh, the faces its hit indices refer to).

    Hidden faces are left out, so they never block a ray (stepping through hidden hits with a fixed count skipped
    visible surfaces behind eight hidden layers, and a fixed epsilon could skip close or very small faces).
    A mesh with no hidden face, the usual case, takes Blender's own BMesh builder, which is several times faster
    than handing it Python lists. world_matrix puts the tree in world space, for an object whose matrix cannot be
    inverted (a surface flattened to zero scale on one axis)."""
    bm.faces.ensure_lookup_table()
    if world_matrix is None and not any(face.hide for face in bm.faces):
        tree = BVHTree.FromBMesh(bm)   # numbers the faces in order, so a hit index is a position in bm.faces
        bm.faces.ensure_lookup_table()
        return tree, bm.faces
    faces = tuple(face for face in bm.faces if not face.hide)
    bm.verts.ensure_lookup_table()
    vertex_indices = {vertex: index for index, vertex in enumerate(bm.verts)}
    vertices = [vertex.co.copy() if world_matrix is None else world_matrix @ vertex.co for vertex in bm.verts]
    tree = BVHTree.FromPolygons(vertices, [tuple(vertex_indices[vertex] for vertex in face.verts) for face in faces],
                                all_triangles=False)
    return tree, faces


# Building the tree of a scene object is what a stroke used to wait for: every click rebuilt one for every visible
# object (0.4 s in a test scene of 42 objects and 290,000 faces). The trees of ordinary scene objects are now kept
# between strokes, in the object's own space, so moving an object costs nothing. One is dropped when the
# dependency graph reports new geometry for its object or its data (forget_occluders), on Undo, Redo, a frame
# change and file load, and it is only reused while the evaluated object still has the same vertex and face counts
# and bounding box.
_occluder_trees = {}
OCCLUDER_TREE_LIMIT = 4096


def _identity(datablock):
    return getattr(datablock, 'session_uid', 0) or datablock.as_pointer()


def _occluder_fingerprint(evaluated_object):
    data = evaluated_object.data
    counts = (len(data.vertices), len(data.polygons)) if isinstance(data, bpy.types.Mesh) else (-1, -1)
    return counts + tuple(round(value, 6) for corner in evaluated_object.bound_box for value in corner)


def forget_occluders(depsgraph=None):
    """Drop kept occluder trees: all of them, or with a dependency graph those whose geometry it updated."""
    if not _occluder_trees:
        return
    if depsgraph is None:
        _occluder_trees.clear()
        return
    try:
        for update in depsgraph.updates:
            if not update.is_updated_geometry:
                continue
            identity = _identity(update.id.original)
            if isinstance(update.id, bpy.types.Object):
                _occluder_trees.pop(identity, None)
            else:
                for key in [key for key, entry in _occluder_trees.items() if entry[0] == identity]:
                    del _occluder_trees[key]
    except Exception:
        _occluder_trees.clear()


class _BrushSceneOccluder:
    """Evaluated scene geometry used only to block a brush ray.

    Its polygon indices must never be interpreted as editable BMesh indices.
    A copied BVH also avoids retaining temporary evaluated meshes.
    """

    def __init__(self, evaluated_object, matrix, depsgraph, keep=False):
        self.matrix = matrix.copy()
        try:
            self.matrix_inv = self.matrix.inverted()
        except ValueError:
            self.matrix_inv = None
        # The eight corners of its bounding box in world space: occluders_near() projects them to tell which
        # occluders a brush circle can touch at all.
        try:
            self.corners = [self.matrix @ mathutils.Vector(corner) for corner in evaluated_object.bound_box]
        except Exception:
            self.corners = None
        self.bvh = None
        source = evaluated_object.original
        # Not a mesh that is itself being edited (a linked duplicate of the edited object): its evaluated mesh is
        # the edit mesh, which changes with every stroke and which only to_mesh() can read.
        keep = (keep and self.matrix_inv is not None and evaluated_object.type == 'MESH'
                and source.data is not None and not source.data.is_editmode)
        if keep:
            key, fingerprint = _identity(source), _occluder_fingerprint(evaluated_object)
            if fingerprint[1] == 0:
                raise ValueError("object has no evaluated surface")
            entry = _occluder_trees.get(key)
            if entry is not None and entry[1] == fingerprint:
                self.bvh = entry[2]
                return
            try:
                # Blender builds this one itself from the evaluated mesh, with no Python lists in between.
                self.bvh = BVHTree.FromObject(source, depsgraph)
            except Exception:
                self.bvh = None
        if self.bvh is None:
            mesh = evaluated_object.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
            try:
                if mesh is None or not mesh.polygons:
                    raise ValueError("object has no evaluated surface")
                self.bvh = BVHTree.FromPolygons(
                    [vertex.co.copy() if self.matrix_inv is not None else self.matrix @ vertex.co
                     for vertex in mesh.vertices],
                    [tuple(polygon.vertices) for polygon in mesh.polygons],
                    all_triangles=False,
                )
            finally:
                evaluated_object.to_mesh_clear()
        if keep:
            if len(_occluder_trees) >= OCCLUDER_TREE_LIMIT:
                _occluder_trees.clear()
            _occluder_trees[key] = (_identity(source.data) if source.data is not None else 0, fingerprint, self.bvh)

    def distance(self, origin_world, direction_world):
        origin = origin_world if self.matrix_inv is None else self.matrix_inv @ origin_world
        direction = direction_world if self.matrix_inv is None else self.matrix_inv.to_3x3() @ direction_world
        if direction.length_squared == 0:
            return None
        location, _normal, _index, _distance = self.bvh.ray_cast(origin, direction.normalized())
        if location is None:
            return None
        world_hit = location if self.matrix_inv is None else self.matrix @ location
        return (world_hit - origin_world).length

    def screen_rect(self, project):
        """(x0, y0, x1, y1) around its bounding box on screen, or None when that cannot be told (a corner is
        behind the view), in which case it has to be tested for every ray."""
        if not self.corners:
            return None
        xs, ys = [], []
        for corner in self.corners:
            point = project(corner)
            if point is None:
                return None
            xs.append(point[0])
            ys.append(point[1])
        return min(xs), min(ys), max(xs), max(ys)


def occluders_near(occluders, region, rv3d, x, y, radius, cache, project):
    """The occluders a ray through the circle of `radius` pixels around (x, y) could hit: those whose bounding box
    comes that close on screen. A scene with hundreds of objects otherwise costs every brush ray a test against
    each of them. cache (a dict the stroke keeps) holds the screen boxes until the view changes."""
    if not occluders:
        return occluders
    try:
        key = (tuple(tuple(row) for row in rv3d.perspective_matrix), region.width, region.height, len(occluders))
    except Exception:
        return occluders
    if cache.get('key') != key or cache.get('project') is not project:
        cache['rects'] = [occluder.screen_rect(lambda point: project(region, rv3d, point)) for occluder in occluders]
        cache['key'], cache['project'] = key, project
    return [occluder for occluder, rect in zip(occluders, cache['rects'])
            if rect is None or (rect[0] - radius <= x <= rect[2] + radius and rect[1] - radius <= y <= rect[3] + radius)]


def _brush_scene_occluders(context, area, targets):
    """Visible non-edited surfaces, including modifier results and instances."""
    depsgraph = context.evaluated_depsgraph_get()
    viewport = area.spaces.active
    edited_objects = {target.obj.original.as_pointer() for target in targets}
    occluders = []
    for instance in depsgraph.object_instances:
        evaluated_object = instance.object
        source = evaluated_object.original
        if source.as_pointer() in edited_objects:
            continue
        if evaluated_object.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        # Object display overrides deliberately expose the surface behind it.
        if source.display_type in {'WIRE', 'BOUNDS'}:
            continue
        visibility_source = instance.parent.original if instance.is_instance and instance.parent else source
        try:
            if not visibility_source.visible_get(view_layer=context.view_layer, viewport=viewport):
                continue
            # visible_get honours local view in current Blender versions; the
            # explicit test also supports older versions of its Python API.
            if viewport.local_view is not None and not visibility_source.local_view_get(viewport):
                continue
            # Instances (collection or geometry-nodes ones) can be temporary objects: only real scene objects
            # have a tree that is safe to keep between strokes.
            occluders.append(_BrushSceneOccluder(evaluated_object, instance.matrix_world, depsgraph,
                                                 keep=not instance.is_instance))
        except (ReferenceError, RuntimeError, ValueError):
            continue  # deleted object or unsupported conversion
    return occluders


class PaintTarget:
    """One editable mesh: its BMesh, BVH, matrices and per-slot atlas sizes."""

    def __init__(self, obj, shared_uv_state=None):
        self.obj = obj
        self.me = obj.data
        self.bm = bmesh.from_edit_mesh(self.me)
        self.bm.faces.ensure_lookup_table()
        if not self.bm.faces:
            raise ValueError("mesh has no faces")
        # Building a target is a read-only operation. A rejected brush launch
        # or a stroke over empty space must not create a UV map with no undo
        # step behind it. Allocate the layer only when a valid face is painted.
        self._uv_state = shared_uv_state if shared_uv_state is not None else {}
        self._uv_state.setdefault('layer', self.bm.loops.layers.uv.active)
        self._uv_state.setdefault('original_uvs', {})
        self._uv_state.setdefault('original_blends', {})
        self._uv_state.setdefault('created', False)
        self.matrix = obj.matrix_world.copy()
        self.orientation_matrix = paint_orientation_matrix(obj).copy()
        try:
            self.matrix_inv = self.matrix.inverted()
        except ValueError:
            self.matrix_inv = None
        # Hidden faces are left out of the tree. Flattened objects can have a perfectly valid XY surface with zero
        # Z scale: those are raycast in world space rather than inventing an inverse or rejecting the valid
        # surface. Truly collapsed faces have no hits.
        self.bvh, self._faces = visible_face_bvh(self.bm, None if self.matrix_inv is not None else self.matrix)
        self._atlas_cache = {}
        self._orientation_cache = {}  # Numeric coordinates only, scoped to this stroke.
        self._mesh_users = self.me.users
        self._counts = (len(self.bm.verts), len(self.bm.edges), len(self.bm.faces))

    # Linked objects can share one edit BMesh while needing separate world
    # transforms/BVHs. Their UV layer and rollback journal belong to the mesh,
    # so creating or restoring a layer through either object updates both.
    @property
    def _uv_layer(self):
        return self._uv_state['layer']

    @_uv_layer.setter
    def _uv_layer(self, value):
        self._uv_state['layer'] = value

    @property
    def _original_uvs(self):
        return self._uv_state['original_uvs']

    @property
    def _created_uv_layer(self):
        return self._uv_state['created']

    @_created_uv_layer.setter
    def _created_uv_layer(self, value):
        self._uv_state['created'] = value

    @property
    def uv_layer(self):
        if self._uv_layer is None:
            self._uv_layer = self.bm.loops.layers.uv.verify()
            self._created_uv_layer = True
        return self._uv_layer

    @property
    def is_valid(self):
        try:
            if not self.bm.is_valid:
                return False
            active_uv = self.bm.loops.layers.uv.active
            same_uv = ((active_uv is None and self._uv_layer is None)
                       or (active_uv is not None and self._uv_layer is not None
                           and active_uv.name == self._uv_layer.name))
            return (self.bm.is_valid and self.obj.data == self.me
                    and self.me.is_editmode
                    and self.me.users == self._mesh_users
                    and self.obj.matrix_world == self.matrix
                    and (len(self.bm.verts), len(self.bm.edges), len(self.bm.faces)) == self._counts
                    and same_uv)
        except Exception:
            return False

    def map_face(self, face, col, row, tile_w_px, tile_h_px, atlas_width, atlas_height,
                 rotation, flip_u, flip_v, inset_px):
        """Paint once, journalling only real changes so Escape can restore them."""
        binding = plain_paint_binding(self.obj,self.bm,face)
        col = max(0, min(max(1, int(atlas_width / tile_w_px)) - 1, col))
        row = max(0, min(max(1, int(atlas_height / tile_h_px)) - 1, row))
        u_start, v_start, span_u, span_v = tile_uv_bounds(
            col, row, tile_w_px, tile_h_px, atlas_width, atlas_height, inset_px)
        signature = tuple(tuple(loop.vert.co) for loop in face.loops)
        local_uvs = self._orientation_cache.get(signature)
        if local_uvs is None:
            try:
                local_uvs = tuple(upright_face_uvs(face, self.orientation_matrix))
            except ValueError:
                return False  # A collapsed face cannot define an upright direction.
            if len(self._orientation_cache) >= 512:
                self._orientation_cache.clear()
            self._orientation_cache[signature] = local_uvs
        expected = []
        for uv in local_uvs:
            u, v = apply_tile_transform(*uv, rotation, flip_u, flip_v)
            expected.append((u_start + u * span_u, v_start + v * span_v))
        # UV coordinates use float32 storage, so compare at that precision.
        if not binding[0] and self._uv_layer is not None and all(
                abs(loop[self._uv_layer].uv.x - uv[0]) < 1e-7
                and abs(loop[self._uv_layer].uv.y - uv[1]) < 1e-7
                for loop, uv in zip(face.loops, expected)):
            return False
        if self._uv_layer is not None and face not in self._original_uvs:
            self._original_uvs[face] = tuple(tuple(loop[self._uv_layer].uv) for loop in face.loops)
        if binding[0] and face not in self._uv_state['original_blends']:
            self._uv_state['original_blends'][face] = paint_blend_snapshot(self.bm,face)
        layer = self.uv_layer
        clear_painted_blend(self.bm,face,binding)
        for loop, uv in zip(face.loops, expected):
            loop[layer].uv = uv
        return True

    def restore(self):
        """Restore authored UVs, or remove the UV map this stroke created."""
        if not self.is_valid:
            return False
        if any(not face.is_valid or len(face.loops) != len(values)
               for face, values in self._original_uvs.items()):
            return False
        for face,(_slot,metadata,factors) in self._uv_state['original_blends'].items():
            if (not face.is_valid or any(self.bm.faces.layers.int.get(name) is None for name in metadata)
                    or (factors is not None and (len(face.loops)!=len(factors) or self.bm.loops.layers.uv.get(SHARED_FACTOR_UV) is None))):
                return False
        if not self._created_uv_layer and not self._original_uvs and not self._uv_state['original_blends']:
            return True
        if self._created_uv_layer:
            self.bm.loops.layers.uv.remove(self._uv_layer)
            self._uv_layer = None
            self._created_uv_layer = False
        else:
            for face, values in self._original_uvs.items():
                if not face.is_valid:
                    return False
                for loop, uv in zip(face.loops, values):
                    loop[self._uv_layer].uv = uv
        self._original_uvs.clear()
        for face,(slot,metadata,factors) in self._uv_state['original_blends'].items():
            face.material_index = slot
            for name,value in metadata.items():face[self.bm.faces.layers.int[name]] = value
            if factors is not None:
                factor = self.bm.loops.layers.uv[SHARED_FACTOR_UV]
                for loop,uv in zip(face.loops,factors):loop[factor].uv = uv
        self._uv_state['original_blends'].clear()
        bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)
        return True

    def atlas_dims(self, material_index):
        dims = self._atlas_cache.get(material_index)
        if dims is None:
            image = atlas_for_material_index(self.obj, material_index)
            dims = image_size(image) if image is not None else (0, 0)
            self._atlas_cache[material_index] = dims
        return dims

    def has_atlas(self):
        return any(self.atlas_dims(i)[0] > 0
                   for i in range(max(1, len(self.obj.material_slots))))

    def raycast(self, origin_world, direction_world):
        """Nearest non-hidden face hit, as (face, world_distance) or None."""
        origin = origin_world if self.matrix_inv is None else self.matrix_inv @ origin_world
        direction = direction_world if self.matrix_inv is None else self.matrix_inv.to_3x3() @ direction_world
        if direction.length_squared == 0.0:
            return None
        direction = direction.normalized()

        location, _normal, index, _dist = self.bvh.ray_cast(origin, direction)
        if location is None or index is None or not (0 <= index < len(self._faces)):
            return None
        face = self._faces[index]
        if (not face.is_valid or face.hide
                or (self.matrix_inv is not None and not hit_within_face_bounds(location, face))):
            return None
        world_hit = location if self.matrix_inv is None else self.matrix @ location
        return face, (world_hit - origin_world).length


# ---------------------------------------------------------------------------
# Sprotile Paint Brush Operator
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_brush_paint(bpy.types.Operator):
    """Paint selected locked-in texture tile onto faces with a viewport brush"""
    bl_idname = "mesh.sprotile_brush_paint"
    bl_label = "Paint Tiles"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (is_editable_mesh(context.active_object)
                and context.area is not None and context.area.type == 'VIEW_3D')

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        objects = edit_mesh_objects(context)
        if not objects:
            self.report({'ERROR'}, "Active object must be a Mesh in Edit Mode")
            return {'CANCELLED'}

        self.area = context.area
        if is_quad_view(self.area):
            self.report({'ERROR'}, "Sprotile Paint does not support Quad View")
            return {'CANCELLED'}
        if get_region_3d(self.area) is None:
            return {'CANCELLED'}

        self.stop_requested = False
        self.draw_handle = None
        self.painted = False
        self.targets = []
        self.occluders = []

        mesh_uv_states = {}
        for obj in objects:
            if not obj.visible_get(view_layer=context.view_layer, viewport=self.area.spaces.active):
                continue
            try:
                shared_uv_state = mesh_uv_states.setdefault(obj.data.as_pointer(), {})
                target = PaintTarget(obj, shared_uv_state=shared_uv_state)
            except ValueError:
                continue  # empty mesh, nothing to paint on
            except Exception as exc:
                print(f"Sprotile: skipping {obj.name}: {exc}")
                continue
            # Every edited mesh blocks the brush ray, including meshes with
            # no paintable atlas. Filtering them out lets strokes pass through
            # the front object and repaint a textured object behind it.
            self.targets.append(target)

        if not any(target.has_atlas() for target in self.targets):
            self.targets = []
            self.report({'ERROR'}, "No active image found inside materials")
            return {'CANCELLED'}

        region = get_window_region(self.area)
        if region is None:
            return {'CANCELLED'}

        try:
            self.occluders = _brush_scene_occluders(context, self.area, self.targets)
            self.mouse_pos = (event.mouse_x - region.x, event.mouse_y - region.y)

            self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                self.draw_callback, (), 'WINDOW', 'POST_PIXEL'
            )
            _brush_draw_handles.append(self.draw_handle)
            _active_brush_operators.append(self)

            self.paint_at_mouse(context)
            context.window_manager.modal_handler_add(self)
        except ValueError as exc:
            restored = self.restore_stroke()
            self.cleanup(context)
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'} if restored else {'FINISHED'}
        except Exception:
            traceback.print_exc()
            restored = self.restore_stroke()
            self.cleanup(context)
            self.report({'WARNING'}, "Brush stopped after an error; stroke reverted" if restored
                        else "Brush stopped after an error; use Undo to restore the stroke")
            return {'CANCELLED'} if restored else {'FINISHED'}
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except ValueError as exc:
            restored = self.restore_stroke()
            self.cleanup(context)
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'} if restored else {'FINISHED'}
        except Exception:
            traceback.print_exc()
            restored = self.restore_stroke()
            self.cleanup(context)
            return {'CANCELLED'} if restored else {'FINISHED'}

    def _modal(self, context, event):
        # force_cleanup() sets this when the addon is being torn down under us.
        if self.stop_requested:
            changed = self.painted
            self.cleanup(context)
            return {'FINISHED'} if changed else {'CANCELLED'}

        # Anything that invalidates the live BMesh (leaving Edit Mode, an undo
        # step, switching object, closing the viewport) must stop the stroke -
        # using the stale BMesh afterwards crashes Blender outright.
        if not self.state_is_valid(context):
            changed = self.painted
            self.cleanup(context)
            return {'FINISHED'} if changed else {'CANCELLED'}

        self.area.tag_redraw()

        if event.type == 'MOUSEMOVE':
            region = get_window_region(self.area)
            if region is not None:
                self.mouse_pos = (event.mouse_x - region.x, event.mouse_y - region.y)
                self.paint_at_mouse(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            changed = self.painted
            self.cleanup(context)
            return {'FINISHED'} if changed else {'CANCELLED'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            restored = self.restore_stroke()
            self.cleanup(context)
            return {'CANCELLED'} if restored else {'FINISHED'}

        if event.type == 'WINDOW_DEACTIVATE':
            changed = self.painted
            self.cleanup(context)
            return {'FINISHED'} if changed else {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def state_is_valid(self, context):
        if context.mode != 'EDIT_MESH':
            return False
        if not area_is_alive(self.area):
            return False
        if not self.targets:
            return False
        for target in self.targets:
            if not target.is_valid:
                return False
        return True

    def restore_stroke(self):
        # If Undo or a mode change has invalidated an edit mesh, do not touch
        # its old BMesh. Finish in that exceptional case so retained changes
        # still get an undo step instead of being silently committed by cancel.
        if not all(target.is_valid and all(
                face.is_valid and len(face.loops) == len(values)
                for face, values in target._original_uvs.items()) for target in self.targets):
            return False
        for target in self.targets:
            if not target.restore():
                return False
        self.painted = False
        return True

    def cleanup(self, context=None):
        if getattr(self, 'draw_handle', None) is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            except Exception:
                pass
            if self.draw_handle in _brush_draw_handles:
                _brush_draw_handles.remove(self.draw_handle)
            self.draw_handle = None

        if self in _active_brush_operators:
            _active_brush_operators.remove(self)

        if self.painted:
            for target in self.targets:
                try:
                    if target.is_valid:
                        bmesh.update_edit_mesh(target.me, loop_triangles=False, destructive=False)
                except Exception:
                    pass
        self.targets = []
        self.occluders = []
        tag_redraw_view3d()

    def cancel(self, context):
        self.restore_stroke()
        self.cleanup(context)

    def blocked_by_preview(self, scene, region, mouse_x, mouse_y):
        """Don't paint through the picker panel - but only while it is open.

        The old build applied this cutoff unconditionally, which made the whole
        left edge of the viewport unpaintable even with the panel closed.
        """
        # The sidebar/tool shelf can overlap the WINDOW region. A running
        # stroke receives mouse moves there too, even with the picker closed.
        if visible_overlap_region(self.area, region.x + mouse_x, region.y + mouse_y) is not None:
            return True
        if state._preview_operator is None:
            return False
        if getattr(state._preview_operator, 'area', None) != self.area:
            return False
        left, width, top = preview_panel_rect(scene, region)
        if left <= mouse_x <= left + width and 0 <= mouse_y <= top:
            return True
        recent_layout = _recent_tiles.layout(scene, region, self.area, state._preview_operator.image)
        return recent_layout is not None and recent_layout.contains(mouse_x, mouse_y)

    def paint_at_mouse(self, context):
        from bpy_extras.view3d_utils import (
            region_2d_to_vector_3d, region_2d_to_origin_3d, location_3d_to_region_2d,
        )

        scene = context.scene
        region = get_window_region(self.area)
        rv3d = get_region_3d(self.area)
        if region is None or rv3d is None:
            return

        mouse_x, mouse_y = self.mouse_pos
        if not (0 <= mouse_x <= region.width and 0 <= mouse_y <= region.height):
            return
        if self.blocked_by_preview(scene, region, mouse_x, mouse_y):
            return

        mouse_coord = (mouse_x, mouse_y)
        mouse_vec = mathutils.Vector(mouse_coord)

        # 1. Project screen space ray
        ray_origin = region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_direction = region_2d_to_vector_3d(region, rv3d, mouse_coord)
        if ray_origin is None or ray_direction is None:
            return

        # Every ray of this call goes through the brush circle, so only occluders near it on screen are tested.
        cull = getattr(self, '_occluder_cull', None)
        if cull is None:
            cull = self._occluder_cull = {}
        occluders = occluders_near(getattr(self, 'occluders', ()), region, rv3d, mouse_x, mouse_y,
                                   scene.sprotile_brush_radius + 2.0, cull, location_3d_to_region_2d)

        # Nothing in this call changes a target before its faces are painted, so each is checked once, not per ray.
        targets = [candidate for candidate in self.targets if candidate.is_valid]

        def nearest_hit(origin, direction):
            best = None
            for candidate in targets:
                hit = candidate.raycast(origin, direction)
                if hit is not None and (best is None or hit[1] < best[2]):
                    best = (candidate, hit[0], hit[1])
            if best is not None:
                for occluder in occluders:
                    distance = occluder.distance(origin, direction)
                    # Avoid numerical flicker when a scene surface coincides
                    # with the editable one (for example duplicate geometry).
                    if distance is not None and distance < best[2] - max(1e-6, best[2] * 1e-7):
                        return None
            return best

        # 2. Nearest hit across every mesh being edited.
        best = nearest_hit(ray_origin, ray_direction)

        if best is None:
            return
        target, hit_face, _distance = best

        col = scene.sprotile_active_col
        row = scene.sprotile_active_row
        tile_w_px, tile_h_px = get_tile_dims(scene)
        rotation, flip_u, flip_v = get_tile_transform(scene)
        inset_px = get_uv_inset(scene)

        radius_pixels = scene.sprotile_brush_radius
        matrix_world = target.matrix

        def is_face_in_brush(face):
            """Require both brush overlap and a visible point on this face.

            Screen overlap alone floods around folded geometry onto its hidden
            back. Cast a fresh ray at each sample instead of using its normal:
            curved surfaces can remain visible at very different face angles.
            Move boundary samples slightly inward to avoid shared-edge ties.
            """
            center = face.calc_center_median()
            samples = [center]
            samples.extend(vertex.co.lerp(center, 0.001) for vertex in face.verts)
            samples.extend(((edge.verts[0].co + edge.verts[1].co) * 0.5).lerp(center, 0.001)
                           for edge in face.edges)
            for sample in samples:
                p2d = location_3d_to_region_2d(region, rv3d, matrix_world @ sample)
                if p2d is None or (p2d - mouse_vec).length > radius_pixels:
                    continue
                if not (0 <= p2d.x < region.width and 0 <= p2d.y < region.height):
                    continue
                origin = region_2d_to_origin_3d(region, rv3d, p2d)
                direction = region_2d_to_vector_3d(region, rv3d, p2d)
                if origin is None or direction is None:
                    continue
                hit = nearest_hit(origin, direction)
                if hit is not None and hit[0] is target and hit[1] is face:
                    return True
            return False

        # BFS across edge-connected, visible faces that fall inside the brush
        queue = deque((hit_face,))
        visited = {hit_face}
        faces_to_paint = [hit_face]  # the face under the cursor always paints

        iterations = 0
        while queue and iterations < BRUSH_MAX_FACES:
            iterations += 1
            face = queue.popleft()
            for edge in face.edges:
                for nf in edge.link_faces:
                    if nf in visited:
                        continue
                    visited.add(nf)
                    if nf.hide:
                        continue
                    if is_face_in_brush(nf):
                        faces_to_paint.append(nf)
                        queue.append(nf)

        painted_here = False
        painted_materials = set()
        # Materials/images can be reloaded between modal events by scripts.
        target._atlas_cache.clear()
        for f in faces_to_paint:
            atlas_width, atlas_height = target.atlas_dims(f.material_index)
            if (atlas_width <= 0 or atlas_height <= 0
                    or tile_w_px > atlas_width or tile_h_px > atlas_height):
                continue
            changed = target.map_face(
                f, col, row,
                tile_w_px, tile_h_px,
                atlas_width, atlas_height,
                rotation, flip_u, flip_v,
                inset_px=inset_px,
            )
            if changed:
                painted_here = True
                painted_materials.add(f.material_index)

        if painted_here:
            for material_index in painted_materials:
                image = atlas_for_material_index(target.obj, material_index)
                _recent_tiles.remember(scene, image, col, row)
            self.painted = True
            bmesh.update_edit_mesh(target.me, loop_triangles=False, destructive=False)

    def draw_callback(self):
        blend_on = False
        try:
            context = bpy.context
            if context.area is None or context.area != self.area:
                return
            region = context.region
            if region is None or region.type != 'WINDOW':
                return

            radius = context.scene.sprotile_brush_radius
            x, y = self.mouse_pos

            gpu.state.blend_set('ALPHA')
            blend_on = True

            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.bind()

            # Inner brush fill
            shader.uniform_float("color", (1.0, 0.9, 0.0, 0.04))
            draw_disc(shader, x, y, radius)

            # Brush outline
            shader.uniform_float("color", (1.0, 0.85, 0.0, 0.7))
            batch_line = batch_for_shader(shader, 'LINE_STRIP', {"pos": circle_points(x, y, radius)})
            batch_line.draw(shader)
        except Exception:
            traceback.print_exc()
        finally:
            try:
                if blend_on:
                    gpu.state.blend_set('NONE')
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sprotile Paint Workspace Toolbar Definition
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_brush_size(bpy.types.Operator):
    """Make the Sprotile brush smaller or larger"""
    bl_idname = 'mesh.sprotile_brush_size'
    bl_label = 'Sprotile Brush Size'
    bl_options = {'INTERNAL'}   # no Undo step: the size is a setting, not a change to the mesh
    factor: FloatProperty(default=1.25, min=0.1, max=10.0, options={'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        scene.sprotile_brush_radius = max(2.0, min(500.0, scene.sprotile_brush_radius * self.factor))
        tag_redraw_view3d()
        return {'FINISHED'}


# The tool's toolbar icon, in Blender's triangle-icon format: four tiles and a brush. The stock icon this tool
# named until 5.15.2 ("ops.paint.texture_paint") is not in Blender 4.4 or later, which left a blank button and a
# "Missing icons" line in the console.
_TOOL_ICON = (
    'VkNPAP//AAAODm4Obm4ODm5uDm54DtgO2G54DthueG4OeG54btgOeG7YDth4eNh42Nh4eNjYeNj94evzu8P94bvDzbHQrrjGpLLQ'
    'rqSyvJq8mqSyiIa8moiGkH6Hh5F9hHpoqFT/aKhU/2ioVP9oqFT/aKhU/2ioVP+wtL7/sLS+/7C0vv+wtL7/sLS+/7C0vv+wtL7/'
    'sLS+/7C0vv+wtL7/sLS+/7C0vv/sulz/7Lpc/+y6XP/sulz/7Lpc/+y6XP/w8PD/8PDw//Dw8P/w8PD/8PDw//Dw8P9Wnv//Vp7/'
    '/1ae//9Wnv//Vp7//1ae//88QEj/PEBI/zxASP88QEj/PEBI/zxASP88QEj/PEBI/zxASP8=')
_STOCK_ICONS = ('brush.sculpt.paint', 'brush.generic', 'ops.generic.select_paint')


def paint_tool_icon():
    """What the tool's bl_icon should be: the path (without .dat) of Sprotile's own icon, written to Blender's
    temporary folder for this session, or the name of a stock icon this Blender has if that cannot be written."""
    try:
        path = os.path.join(bpy.app.tempdir, 'sprotile.paint')
        data = base64.b64decode(_TOOL_ICON)
        if not os.path.exists(path + '.dat') or os.path.getsize(path + '.dat') != len(data):
            with open(path + '.dat', 'wb') as stream:
                stream.write(data)
        return path
    except Exception:
        pass
    try:
        folder = bpy.utils.system_resource('DATAFILES', path='icons')
        for name in _STOCK_ICONS:
            if os.path.exists(os.path.join(folder, name + '.dat')):
                return name
    except Exception:
        pass
    return 'none'


class MESH_WT_sprotile_paint_tool(bpy.types.WorkSpaceTool):
    """Integrate Sprotile Painting directly as a brush tool in the main Edit Toolbar"""
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'EDIT_MESH'
    bl_idname = "mesh.sprotile_paint_tool"
    bl_label = "Sprotile Paint"
    bl_description = ("Paint the picked atlas tile onto faces with a brush.\n"
                      "[ and ] change the brush size")
    bl_icon = 'none'   # set by register(), see paint_tool_icon()
    bl_widget = None
    bl_keymap = (
        ("mesh.sprotile_brush_paint", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
        ("mesh.sprotile_brush_size", {"type": 'LEFT_BRACKET', "value": 'PRESS', "repeat": True},
         {"properties": [("factor", 0.8)]}),
        ("mesh.sprotile_brush_size", {"type": 'RIGHT_BRACKET', "value": 'PRESS', "repeat": True},
         {"properties": [("factor", 1.25)]}),
    )

    @staticmethod
    def draw_cursor(_context, _tool, xy):
        """The brush circle under the mouse while the tool is active, so its size shows before the first click.
        Not over the picker or the recent tiles: a click there picks a tile."""
        try:
            context = bpy.context
            scene, region, area = context.scene, context.region, context.area
            if scene is None or region is None or area is None or region.type != 'WINDOW':
                return
            x, y = xy[0] - region.x, xy[1] - region.y
            preview = state._preview_operator
            picker = preview is not None and getattr(preview, 'area', None) == area
            if status_enabled(context):
                # Beside the picker when it is open, so the two never overlap.
                taken = sum(preview_panel_rect(scene, region)[:2]) if picker else 0
                draw_status(paint_status(scene), region.width - taken, (region.x + taken, region.y))
            if picker:
                left, width, top = preview_panel_rect(scene, region)
                if left <= x <= left + width and 0 <= y <= top:
                    return
                recent = _recent_tiles.layout(scene, region, area, preview.image)
                if recent is not None and recent.contains(x, y):
                    return
            from gpu_extras.presets import draw_circle_2d
            gpu.state.blend_set('ALPHA')
            draw_circle_2d(xy, (1.0, 0.85, 0.0, 0.7), scene.sprotile_brush_radius, segments=48)
            gpu.state.blend_set('NONE')
        except Exception:
            pass   # a cursor must never break the viewport's drawing

    def draw_settings(context, layout, _tool):
        """The brush size in the Active Tool panels. Not in the strip along the top of the viewport: that is
        where the picker writes its tile and atlas lines."""
        if getattr(context.region, 'type', '') == 'TOOL_HEADER':
            return
        scene = context.scene
        layout.prop(scene, 'sprotile_brush_radius', text='Radius')
        layout.label(text='[ and ] change it', icon='INFO')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import BRUSH_MAX_FACES, _active_brush_operators, _brush_draw_handles
from .draw import circle_points, draw_disc, tag_redraw_view3d
from .hints import draw_status, paint_status, status_enabled
from .tiles import (
    _recent_tiles, apply_tile_transform, atlas_for_material_index, edit_mesh_objects, get_tile_dims,
    get_tile_transform, get_uv_inset, image_size, is_editable_mesh, preview_panel_rect, tile_uv_bounds,
)
from .mapping import (
    clear_painted_blend, paint_blend_snapshot, paint_orientation_matrix, plain_paint_binding,
    upright_face_uvs,
)
from .picker import area_is_alive, get_region_3d, get_window_region, is_quad_view, visible_overlap_region
from .blend_settings import SHARED_FACTOR_UV
