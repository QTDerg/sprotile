"""Path brush (5.15, experimental): paint whole faces with tile B; the faces around them blend into it.

Spyro builds paths from whole tiles plus transition tiles, and this does the same with shared blends:
- a painted face becomes B (a Painted Only patch at Position 1), keeping its own tile underneath as A;
- every face that shares a corner with a painted face, has the same material and does not bend away by more than
  BORDER_MAX_ANGLE (so a wall beside the path stays as it is), gets its own blend: its corners that touch painted
  faces are B and the rest A, fitted with one straight transition (direction, centre and width) in the face's own
  tile layout. With Grass Edge or Snow Drift the transition gets tufts or drifts.
- painting is remembered per face in PATH_PAINT_ATTRIBUTE (1 painted, 2 border), so later strokes, erasing and
  Update Path only redo the faces around the change.

Both kinds are ordinary shared patches: Remove Blend, Edit Active and Unreal export treat them like any other.
A face's A stays the tile it shows now, and B lies on it with the same turn and flip (Existing UVs per face).
"""

import math
import time
from types import SimpleNamespace

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty

PAINTED, BORDER = 1, 2
BORDER_MAX_ANGLE = math.radians(50.0)
STROKE_FACE_LIMIT = 4096
APPLY_INTERVAL = 0.04   # seconds between batches while the Path Brush drags (a batch now costs what it changes)


def path_layer(mesh, bm, create=True):
    layer = bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE)
    if layer is None:
        existing = mesh.attributes.get(PATH_PAINT_ATTRIBUTE)
        if existing is not None:
            raise ValueError(f"Reserved attribute '{PATH_PAINT_ATTRIBUTE}' already exists with another type; rename it")
        if not create:
            return None
        layer = bm.faces.layers.int.new(PATH_PAINT_ATTRIBUTE)
    return layer


def plane_fit(points, values):
    """Least-squares (a, b, c) with a*u + b*v + c close to the values, or None when the points are on a line."""
    n = len(points)
    su = sum(p[0] for p in points); sv = sum(p[1] for p in points)
    suu = sum(p[0] * p[0] for p in points); svv = sum(p[1] * p[1] for p in points); suv = sum(p[0] * p[1] for p in points)
    sf = sum(values); suf = sum(p[0] * f for p, f in zip(points, values)); svf = sum(p[1] * f for p, f in zip(points, values))
    matrix = ((suu, suv, su), (suv, svv, sv), (su, sv, n))
    right = (suf, svf, sf)

    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    whole = det(matrix)
    if abs(whole) < 1e-12:
        return None
    result = []
    for column in range(3):
        swapped = tuple(tuple(right[r] if c == column else matrix[r][c] for c in range(3)) for r in range(3))
        result.append(det(swapped) / whole)
    return tuple(result)


def border_settings(mask, targets, feather):
    """Blend settings for one border face: B at the corners whose target is 1, A at 0, one straight transition.

    The fit f = a*u + b*v + c over the face's Mask UVs becomes Direction Angle (along (a, b)), Position (where
    f = 0.5) and Width (feather / |(a, b)|, so Feather 100% fades across the whole face)."""
    fit = plane_fit(mask, targets)
    mean = sum(targets) / len(targets)
    if fit is None or math.hypot(fit[0], fit[1]) < 1e-4:
        # Flat: every corner touches the path (a hole in it), or two opposite corners do. Fill it with B.
        return dict(direction='PAINTED', position=1.0 if mean >= .5 else 0.0)
    a, b, c = fit
    length = math.hypot(a, b)
    angle = math.atan2(b, a) % math.tau
    x, y = math.cos(angle), math.sin(angle)
    x, y = (0.0 if abs(value) < 1e-6 else value for value in (x, y))
    offset = .5 - .5 * (x + y)
    centre = (.5 - c) / length
    width = min(2.0, max(0.0, feather / length))
    position = min(1.0, max(0.0, centre + offset))
    limit = 6.2831850051879883   # float32(tau), the property's maximum
    return dict(direction='ANGLE', angle=min(angle, limit), position=position, width=width)


def path_region(bm, painted, usable=lambda face: True, seeds=None):
    """Painted faces plus the faces that blend into them. With seeds (some of the painted faces), only those and
    the faces around them: the part of the region a change near them can alter."""
    region = set(painted if seeds is None else seeds)
    for index in (painted if seeds is None else seeds):
        face = bm.faces[index]
        normal = face.normal
        for vert in face.verts:
            for other in vert.link_faces:
                if other.index in region or other.index in painted or other.hide \
                        or other.material_index != face.material_index:
                    continue
                if normal.length > 0 and other.normal.length > 0 and normal.angle(other.normal) > BORDER_MAX_ANGLE:
                    continue
                if not usable(other):
                    continue
                region.add(other.index)
    return region


def face_components(bm, indices):
    """Groups of the given faces that touch at a corner (each becomes one patch)."""
    remaining, groups = set(indices), []
    while remaining:
        seed = remaining.pop()
        group, queue = [seed], [seed]
        while queue:
            for vert in bm.faces[queue.pop()].verts:
                for other in vert.link_faces:
                    if other.index in remaining:
                        remaining.discard(other.index)
                        group.append(other.index)
                        queue.append(other.index)
        groups.append(sorted(group))
    return groups


def clear_face_blend(face, stored, patch, factor, previous):
    """What Remove Blend does for one shared face: its earlier tile back, no patch."""
    restore_base_tile(face, stored)
    face[patch] = 0
    if factor is not None:
        for loop in face.loops:
            loop[factor].uv = (0.0, 0.0)
    if previous is not None:
        face[previous] = 0


def usable_face_test(obj, image):
    """Faces the Path brush can blend: visible, with an area, and a material that samples B's atlas."""
    slots = obj.material_slots
    verdicts = {}

    def material_ok(index):
        if index not in verdicts:
            material = slots[index].material if index < len(slots) else None
            ok = material is not None and material.use_nodes and material.node_tree is not None and any(
                node.type == 'TEX_IMAGE' and node.image == image for node in material.node_tree.nodes)
            verdicts[index] = ok
        return verdicts[index]

    def usable(face):
        return (not face.hide and len(face.verts) >= 3 and face.calc_area() > 1e-10
                and material_ok(face.material_index))
    return usable


def read_path_marks(bm, layer):
    """{face index: PAINTED or BORDER} for every face the Path brush has marked. (Other values in the layer
    belong to other tools: Blend This Seam marks its faces 3, and a path update leaves those alone.)"""
    if layer is None:
        return {}
    return {face.index: face[layer] for face in bm.faces if face[layer] in (PAINTED, BORDER)}


def update_path(context, obj, add=(), remove=(), refresh=False, session=None):
    """Paint faces (add), erase them (remove) or redo every path face with the current settings (refresh).

    One transaction: every blend is placed by a single Apply, which undoes itself if anything fails, and the path
    markers are only written after it worked. Faces that can't be blended (hidden, no area, or a material without
    B's atlas) are skipped. Returns (faces painted, faces blended at the border, faces skipped). Raises ValueError
    like Apply Blend does, leaving the mesh as it was.

    The work is in proportion to the faces that change, not to the mesh or to the path so far: only the faces
    around the change are looked at again, and the faces to blend are named to Apply directly, so the selection is
    neither read nor touched. session (a dict the Path Brush keeps while it runs) carries the path markers from
    one batch to the next, which saves reading them from every face each time; session['changed'] is set when
    the mesh was changed."""
    scene = context.scene
    image = scene.sprotile_blend_b.image
    if image is None:
        raise ValueError('Capture B first: the tile the path is made of')
    if scene.sprotile_blend_defaults.edge_style == 'OVERLAY':
        raise ValueError('The Path Brush draws Straight, Grass Edge or Snow Drift edges; change Style')
    mesh = obj.data
    bm = bmesh.from_edit_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    usable = usable_face_test(obj, image)
    layer = path_layer(mesh, bm, create=False)
    membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
    # The markers as they are (base) are only read here; what this call changes is collected in `delta` and
    # written, to the mesh and to the session, once the blends are in place.
    trusted = session is not None and session.get('bm') is bm and session.get('faces') == len(bm.faces)
    base = session['marks'] if trusted else read_path_marks(bm, layer)
    delta = {}

    def mark(index):
        return delta[index] if index in delta else base.get(index, 0)
    changed, skipped = set(), 0
    if not trusted:
        for index, value in base.items():
            # Remove Blend (or an Undo past the blend) took this painted face back to its tile: it is ground again.
            # (While the brush runs nothing else can do that between its batches.)
            if value == PAINTED and (membership is None or not bm.faces[index][membership]):
                delta[index] = 0
                changed.add(index)
    def border_usable(face):
        # Direct painting can take over a blend. Automatic borders keep regular blends and leave existing seams
        # alone when refreshing, while new strokes can still blend into a seam beside them.
        tool_mark = face[layer] if layer is not None else 0
        return (usable(face) and (membership is None or not face[membership]
                                  or mark(face.index) in (PAINTED, BORDER) or (tool_mark and not refresh)))

    total = len(bm.faces)
    for index in add:
        if not 0 <= index < total or mark(index) == PAINTED:
            continue
        if not usable(bm.faces[index]):
            skipped += 1
            continue
        delta[index] = PAINTED
        changed.add(index)
    for index in remove:
        if mark(index) == PAINTED:
            delta[index] = 0
            changed.add(index)
    if refresh:
        painted = {index for index in set(base) | set(delta) if mark(index) == PAINTED}
        region = path_region(bm, painted, border_usable)
        scope = set(base) | region          # every face that is, or was, part of a path
        changed |= scope
    elif changed:
        # Only faces that share a corner with a changed face can gain or lose their blend, and only the painted
        # faces around those decide it: nothing here depends on how long the path already is.
        scope = set(changed)
        for index in changed:
            for vert in bm.faces[index].verts:
                scope.update(other.index for other in vert.link_faces)
        seeds = {other.index for index in scope for vert in bm.faces[index].verts for other in vert.link_faces
                 if mark(other.index) == PAINTED}
        painted = {other.index for index in seeds for vert in bm.faces[index].verts for other in vert.link_faces
                   if mark(other.index) == PAINTED} | seeds
        region = path_region(bm, painted, border_usable, seeds)
    if not changed:
        return 0, 0, skipped
    border = sorted(index for index in region if index in scope and mark(index) != PAINTED)
    border_set = set(border)
    # BMVerts themselves, not their index: vertices made in this Edit Mode session can all read -1.
    near = {vert for index in border for vert in bm.faces[index].verts}
    painted_vertices = {vert for vert in near if any(mark(other.index) == PAINTED for other in vert.link_faces)}
    # Hidden faces keep what they have: Blender's own tools leave them alone, and so does a blend.
    hidden = {index for index in scope if bm.faces[index].hide}
    fill = sorted(index for index in changed if mark(index) == PAINTED and index not in hidden)
    gone = sorted(index for index in scope if base.get(index) and mark(index) != PAINTED and index not in region
                  and index not in hidden)
    targets = {index: [1.0 if loop.vert in painted_vertices else 0.0 for loop in bm.faces[index].loops]
               for index in border}
    feather = scene.sprotile_blend_defaults.width
    fill_set = set(fill)
    groups = face_components(bm, fill) + [[index] for index in border]

    def settings(indices, masks):
        if indices[0] in fill_set:
            return dict(direction='PAINTED', position=1.0)
        return border_settings(masks[indices[0]], targets[indices[0]], feather)

    if groups:
        key = mesh.as_pointer()

        def apply(method):
            return apply_shared_texture_blend(
                context, overrides=dict(tiles='FACE', path=False, alpha_shaping=False, direction='ANGLE'),
                keep_a=True, patch_groups={key: groups}, patch_settings=settings, method=method, fit='FILL',
                patch_name='Path', controls=scene.sprotile_blend_defaults, faces={key: fill + border},
                preserve_path_marks=True)
        try:
            apply('EXISTING')
        except ValueError as error:
            if 'Existing UVs' not in str(error):
                raise
            apply('UNFOLD')   # a face's own UVs are collapsed: lay B out from its shape instead
    bm = bmesh.from_edit_mesh(mesh)
    bm.faces.ensure_lookup_table()
    layer = path_layer(mesh, bm)
    if gone:
        stored = edit_blend_layers(mesh, bm)
        patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
        factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
        previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
        for index in gone:
            face = bm.faces[index]
            if patch is not None and face[patch]:
                clear_face_blend(face, stored, patch, factor, previous)
    for index in scope:
        if index in hidden and mark(index) != PAINTED:
            continue
        value = PAINTED if mark(index) == PAINTED else BORDER if index in border_set else 0
        face = bm.faces[index]
        if face[layer] != value and (value or face[layer] in (PAINTED, BORDER)):
            face[layer] = value
        if value:
            base[index] = value
        else:
            base.pop(index, None)
    if session is not None:
        session.update(bm=bm, faces=len(bm.faces), marks=base, changed=True)
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    tag_redraw_view3d()
    return len(fill), len(border), skipped


def _report_result(operator, painted, border, verb='Painted'):
    if painted or border:
        operator.report({'INFO'}, f'{verb} {painted} face{"s" if painted != 1 else ""}; '
                                  f'{border} face{"s" if border != 1 else ""} blend into the path')


class MESH_OT_sprotile_path_from_selection(bpy.types.Operator):
    """Paint the selected faces with tile B and blend the faces around them into it; Clear takes faces back out"""
    bl_idname = 'mesh.sprotile_path_selection'
    bl_label = 'Path from Selection'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[('ADD', 'Paint', 'Make the selected faces part of the path'),
                                ('REMOVE', 'Clear', 'Take the selected faces out of the path'),
                                ('REFRESH', 'Update', 'Redo every path face with the current Style, Feather and B')],
                         default='ADD', options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        if not is_editable_mesh(context.active_object):
            cls.poll_message_set('Enter Edit Mode on a mesh first')   # shown in the greyed-out button's tooltip
            return False
        return True

    def execute(self, context):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.index_update()   # faces made in this Edit Mode session can still read -1
        selected = [face.index for face in bm.faces if face.select and not face.hide]
        if self.action != 'REFRESH' and not selected:
            self.report({'WARNING'}, 'Select faces first')
            return {'CANCELLED'}
        try:
            painted, border, skipped = update_path(context, obj, add=selected if self.action == 'ADD' else (),
                                                   remove=selected if self.action == 'REMOVE' else (),
                                                   refresh=self.action == 'REFRESH')
        except (ValueError, RuntimeError) as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        if skipped:
            self.report({'WARNING'}, f'{skipped} selected face{"s" if skipped != 1 else ""} left out: no area, or a '
                        'material without the atlas B comes from')
        if self.action == 'REMOVE':
            self.report({'INFO'}, 'Path cleared from the selection')
        elif not (painted or border):
            self.report({'INFO'}, 'Nothing changed: those faces are already part of the path'
                        if self.action == 'ADD' else 'There is no path on this mesh yet')
        else:
            _report_result(self, painted, border, 'Updated' if self.action == 'REFRESH' else 'Painted')
        return {'FINISHED'}


def editable_hit(bvh, matrix, occluders, origin, direction):
    """The face of the edited mesh a view ray reaches first, or None when it misses or another visible object is
    in front (as the Paint Tiles brush checks)."""
    inverse = matrix.inverted_safe()
    local_direction = inverse.to_3x3() @ direction
    if local_direction.length_squared == 0:
        return None
    location, _normal, index, _distance = bvh.ray_cast(inverse @ origin, local_direction.normalized())
    if location is None:
        return None
    distance = ((matrix @ location) - origin).length
    for occluder in occluders:
        blocked = occluder.distance(origin, direction)
        if blocked is not None and blocked < distance - max(1e-6, distance * 1e-7):
            return None
    return index


class MESH_OT_sprotile_path_brush(bpy.types.Operator):
    """Drag over faces to paint them with tile B; Ctrl-drag erases. Each stroke is its own Undo step.
    Right-click, Enter or Esc finishes"""
    bl_idname = 'mesh.sprotile_path_brush'
    bl_label = 'Path Brush'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        if not is_editable_mesh(context.active_object):
            cls.poll_message_set('Enter Edit Mode on a mesh first')
            return False
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        if context.scene.sprotile_blend_b.image is None:
            self.report({'WARNING'}, 'Capture B first: the tile the path is made of')
            return {'CANCELLED'}
        if is_quad_view(context.area):
            self.report({'WARNING'}, 'The Path Brush does not work in Quad View; switch back to one view (Ctrl Alt Q)')
            return {'CANCELLED'}
        self.obj = context.active_object
        self.obj_name = self.obj.name
        self.area = context.area
        self.region = get_window_region(context.area)
        self.rv3d = get_region_3d(context.area)
        if self.region is None or self.rv3d is None:
            self.report({'WARNING'}, 'Use the Path Brush in a single 3D Viewport')
            return {'CANCELLED'}
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        self.over_ui = False
        self.stroke, self.pending, self.erasing, self.painting, self.total = set(), set(), False, False, 0
        # The faces the next dab would take (world-space corners), shown while the mouse hovers.
        self.hover, self.hover_erase = [], False
        # Undo: every finished stroke is pushed as a step of its own (unsaved: changes not pushed yet). Ctrl+Z
        # inside the brush only steps back through the strokes of this session (steps), and Ctrl+Shift+Z forward
        # again (undone); the brush never lets an Undo reach back before it started.
        self.unsaved, self.steps, self.undone = False, 0, 0
        self.last_apply = 0.0
        self.stop_requested = False
        self.bvh, self.bvh_faces, self.bm, self.cull, self.session = None, (), None, {}, {}
        self.build_bvh()
        # Other visible objects block the brush, as they do for Paint Tiles.
        self.occluders = _brush_scene_occluders(context, self.area, [SimpleNamespace(obj=self.obj)])
        self.handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_brush, (), 'WINDOW', 'POST_PIXEL')
        # Registered like Paint Tiles, so loading a file or disabling Sprotile mid-stroke cleans up.
        _brush_draw_handles.append(self.handle)
        _active_brush_operators.append(self)
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set('Path Brush: drag to paint B, Ctrl-drag to erase, [ and ] size, '
                                     'Ctrl+Z undoes a stroke; Right-click, Enter or Esc to finish')
        return {'RUNNING_MODAL'}

    def find_object(self, context):
        """The object being painted. An Undo can replace Blender's data under the brush, which leaves the old
        reference dead; the object is then found again by name."""
        try:
            self.obj.name
            return self.obj
        except AttributeError:
            return None
        except ReferenceError:
            pass
        obj = context.active_object
        if obj is None or obj.type != 'MESH' or obj.name != self.obj_name:
            obj = None
        self.obj, self.bm, self.bvh_faces = obj, None, ()
        self.session.clear()
        return obj

    def build_bvh(self):
        """The tree the brush casts its rays into: the visible faces only, so a hidden face never blocks it."""
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        self.bm = bm
        self.bvh, self.bvh_faces = visible_face_bvh(bm)

    def cleanup(self, context=None):
        handle = getattr(self, 'handle', None)
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except Exception:
                pass
            if handle in _brush_draw_handles:
                _brush_draw_handles.remove(handle)
            self.handle = None
        if self in _active_brush_operators:
            _active_brush_operators.remove(self)
        if getattr(self, 'area', None) is not None:
            try:
                self.area.header_text_set(None)
            except (ReferenceError, RuntimeError, AttributeError):
                pass
        self.occluders = []
        tag_redraw_view3d()

    def finish(self, context):
        self.cleanup(context)

    def cancel(self, context):
        self.cleanup(context)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception as error:   # never leave the viewport stuck in a modal state
            self.cleanup(context)
            self.report({'WARNING'}, f'Path Brush stopped: {error}')
            return self.result()

    def result(self):
        """FINISHED makes Blender push an Undo step, so it is returned only for changes no stroke step holds yet."""
        return {'FINISHED'} if self.unsaved else {'CANCELLED'}

    def push_stroke(self):
        """Give the stroke that just ended its own Undo step."""
        if not self.unsaved:
            return
        keep_undo_in_step()
        try:
            bpy.ops.ed.undo_push(message='Path Brush Stroke')
        except Exception:
            return   # the step is pushed when the brush finishes instead
        self.unsaved, self.steps, self.undone = False, self.steps + 1, 0

    def _modal(self, context, event):
        obj = self.find_object(context)
        if (self.stop_requested or obj is None or obj.mode != 'EDIT' or not area_is_alive(self.area)):
            self.cleanup(context)
            return self.result()
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'} \
                or event.type.startswith('NDOF'):
            return {'PASS_THROUGH'}
        if event.type in {'RIGHTMOUSE', 'ESC', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self.flush(context)
            self.cleanup(context)
            return self.result()
        if event.type == 'Z' and event.value == 'PRESS' and event.ctrl and not event.alt and not self.painting:
            # Blender's own Undo and Redo run, but only across the strokes of this brush session: those are Edit
            # Mode steps of this mesh. An Undo from before the brush started could replace the objects under it.
            redo = event.shift
            if (self.undone if redo else self.steps) > 0:
                self.steps += 1 if redo else -1
                self.undone += -1 if redo else 1
                self.session.clear()   # the mesh is about to change under the brush
                self.hover = []
                return {'PASS_THROUGH'}
            self.report({'INFO'}, 'Nothing more to redo' if redo else
                        'No stroke left to undo here; finish the brush (Enter) to undo earlier work')
            return {'RUNNING_MODAL'}
        if event.type in {'LEFT_BRACKET', 'RIGHT_BRACKET'} and event.value == 'PRESS':
            scene = context.scene
            scene.sprotile_brush_radius = max(2, scene.sprotile_brush_radius * (0.8 if event.type == 'LEFT_BRACKET' else 1.25))
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'MOUSEMOVE', 'LEFTMOUSE'}:
            self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
            # The sidebar and toolbar can overlap the view: the brush never paints through them.
            self.over_ui = (visible_overlap_region(self.area, event.mouse_x, event.mouse_y) is not None
                            or not (0 <= self.mouse[0] < self.region.width and 0 <= self.mouse[1] < self.region.height))
        if event.type == 'MOUSEMOVE':
            if self.painting and not self.over_ui:
                self.collect(context)
            self.update_hover(context, event.ctrl)
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if self.over_ui:
                    return {'PASS_THROUGH'}   # a click on the sidebar or a header still works
                self.painting, self.erasing, self.stroke = True, event.ctrl, set()
                self.hover = []
                self.collect(context)
            elif event.value == 'RELEASE' and self.painting:
                self.painting = False
                self.flush(context)
                self.stroke = set()
                self.push_stroke()
                self.update_hover(context, event.ctrl)
            elif event.value == 'RELEASE':
                return {'PASS_THROUGH'}   # the end of a click that began on the sidebar or a header: theirs
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def update_hover(self, context, erase=False):
        """Remember the faces a click here would take, for draw_brush to show. Nothing while a stroke runs (the
        stroke shows itself) or over the sidebar."""
        self.hover, self.hover_erase = [], bool(erase)
        if self.painting or self.over_ui:
            return
        try:
            faces = self.faces_under_brush(context)
            matrix = self.obj.matrix_world
            self.hover = [[matrix @ vert.co for vert in self.bm.faces[index].verts] for index in sorted(faces)]
        except Exception:
            self.hover = []   # a preview must never stop the brush

    def faces_under_brush(self, context):
        from bpy_extras.view3d_utils import location_3d_to_region_2d, region_2d_to_origin_3d, region_2d_to_vector_3d
        from mathutils import Vector
        region, rv3d = self.region, self.rv3d
        matrix = self.obj.matrix_world
        mouse = Vector(self.mouse)
        if not (0 <= mouse.x < region.width and 0 <= mouse.y < region.height):
            return set()
        bm = bmesh.from_edit_mesh(self.obj.data)
        if bm is not self.bm or not self.bvh_faces or not self.bvh_faces[0].is_valid:
            self.build_bvh()   # the edit mesh was replaced under the brush (an Undo): look at the new one
            bm = self.bm
        bm.faces.ensure_lookup_table()
        radius = context.scene.sprotile_brush_radius
        # Every ray goes through the brush circle, so only objects near it on screen can block one.
        occluders = occluders_near(self.occluders, region, rv3d, mouse.x, mouse.y, radius + 2.0, self.cull,
                                   location_3d_to_region_2d)
        faces = self.bvh_faces

        def hit(point2d):
            origin = region_2d_to_origin_3d(region, rv3d, point2d)
            direction = region_2d_to_vector_3d(region, rv3d, point2d)
            if origin is None or direction is None:
                return None
            found = editable_hit(self.bvh, matrix, occluders, origin, direction)
            if found is None or not 0 <= found < len(faces):
                return None
            return faces[found].index
        first = hit(mouse)
        if first is None or first >= len(bm.faces) or bm.faces[first].hide:
            return set()
        found, queue = {first}, [first]
        while queue and len(found) < STROKE_FACE_LIMIT:
            face = bm.faces[queue.pop()]
            for edge in face.edges:
                for other in edge.link_faces:
                    if other.index in found or other.hide:
                        continue
                    centre = location_3d_to_region_2d(region, rv3d, matrix @ other.calc_center_median())
                    if centre is None or (centre - mouse).length > radius:
                        continue
                    if hit(centre) != other.index:
                        continue   # behind something
                    found.add(other.index)
                    queue.append(other.index)
        return {index for index in found if not bm.faces[index].hide}

    def collect(self, context):
        new = self.faces_under_brush(context) - self.stroke
        if new:
            self.stroke |= new
            self.pending |= new
        # Paint as the brush moves, in batches no more often than APPLY_INTERVAL: each batch rewrites the faces
        # around it, which takes a moment on a large level mesh.
        if self.pending and time.monotonic() - self.last_apply >= APPLY_INTERVAL:
            self.flush(context)

    def flush(self, context):
        if self.pending:
            faces, self.pending = self.pending, set()
            self.apply(context, faces)
            self.last_apply = time.monotonic()

    def apply(self, context, faces):
        if self.erasing:
            painted, border, _skipped = update_path(context, self.obj, remove=faces, session=self.session)
        else:
            painted, border, _skipped = update_path(context, self.obj, add=faces, session=self.session)
        self.total += painted + border + (len(faces) if self.erasing else 0)
        # update_path says in the session whether it changed the mesh (erasing a lone face blends nothing new).
        self.unsaved = self.unsaved or bool(self.session.pop('changed', False))

    def draw_brush(self):
        try:
            import gpu
            from gpu_extras.batch import batch_for_shader
            context = bpy.context
            if (context.area != self.area or context.region is None or context.region.type != 'WINDOW'
                    or self.over_ui):
                return
            x, y = self.mouse
            radius = context.scene.sprotile_brush_radius
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.blend_set('ALPHA')
            shader.bind()
            if self.hover and not self.painting:
                from bpy_extras.view3d_utils import location_3d_to_region_2d
                triangles = []
                for corners in self.hover:
                    points = [location_3d_to_region_2d(context.region, self.rv3d, corner) for corner in corners]
                    if any(point is None for point in points):
                        continue
                    for second, third in zip(points[1:], points[2:]):
                        triangles += [points[0], second, third]
                if triangles:
                    shader.uniform_float('color', (1.0, .35, .25, .28) if self.hover_erase else (.55, .9, 1.0, .28))
                    batch_for_shader(shader, 'TRIS', {'pos': triangles}).draw(shader)
            colour = (1.0, .35, .25, .8) if self.erasing and self.painting else (.55, .9, 1.0, .8)
            shader.uniform_float('color', colour)
            batch_for_shader(shader, 'LINE_STRIP', {'pos': circle_points(x, y, radius)}).draw(shader)
            gpu.state.blend_set('NONE')
            if status_enabled(context):
                draw_status(path_status(context.scene, self.erasing and self.painting), context.region.width)
        except Exception:
            pass


def draw_path_brush_controls(layout, context):
    """The Path brush Quick start: the brush, the selection buttons and Update."""
    box = layout.box()
    row = box.row()
    row.scale_y = 1.4
    row.operator('mesh.sprotile_path_brush', text='Path Brush', icon='BRUSH_DATA')
    draw_blocker(box, action_blocker(context, 'path_brush'))
    row = box.row(align=True)
    row.operator('mesh.sprotile_path_selection', text='Paint Selected').action = 'ADD'
    row.operator('mesh.sprotile_path_selection', text='Clear Selected').action = 'REMOVE'
    box.operator('mesh.sprotile_path_selection', text='Update Path Edges', icon='FILE_REFRESH').action = 'REFRESH'
    box.prop(context.scene, 'sprotile_brush_radius', text='Brush Size')


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .draw import circle_points, tag_redraw_view3d
from .tiles import is_editable_mesh
from .picker import area_is_alive, get_region_3d, get_window_region, is_quad_view, visible_overlap_region
from .core import _active_brush_operators, _brush_draw_handles, keep_undo_in_step
from .hints import action_blocker, draw_blocker, draw_status, path_status, status_enabled
from .brush import _brush_scene_occluders, occluders_near, visible_face_bvh
from .blend_settings import BLEND_ORIGINAL_MATERIAL, PATH_PAINT_ATTRIBUTE, SHARED_FACTOR_UV, SHARED_PATCH_ID
from .blend_storage import edit_blend_layers, restore_base_tile
from .blend_shared import apply_shared_texture_blend
