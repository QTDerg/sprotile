"""Putting tiles on faces: Map Per Face, patch layouts (Unfold, Surface, Existing UVs, Rectangle) and the
map, turn and flip operators.
"""

import bpy
import bmesh
import math
import mathutils
from collections import deque, defaultdict
from bpy.props import IntProperty, EnumProperty


def paint_orientation_matrix(obj):
    # One UV layout cannot be world-upright on differently rotated instances.
    # Use shared local axes so hitting another instance never repaints a turn.
    return mathutils.Matrix.Identity(4) if obj.data.users > 1 else obj.matrix_world


def upright_face_uvs(face, matrix_world=None):
    """Choose tile corners from surface geometry, independent of the first loop.

    World Z is up on walls; horizontal surfaces use world X/Y. Whole quads
    still cover all four tile corners. Triangles use the best matching half.
    """
    matrix_world = matrix_world if matrix_world is not None else mathutils.Matrix.Identity(4)
    chart = patch_vertex_uvs([face], matrix_world, 'SURFACE')
    projected = [chart[loop.vert] for loop in face.loops]
    count = len(projected)
    order = sorted(range(count), key=lambda i: (projected[i], tuple(face.loops[i].vert.co)))
    base = [base_local_uv(i, count) for i in range(count)]
    candidates = []
    for rotation in ((0, 90, 180, 270) if count != 4 else (0,)):
        corners = [apply_tile_transform(*uv, rotation, False, False) for uv in base]
        for offset in range(count):
            values = corners[offset:] + corners[:offset]
            error = sum((a[0]-b[0])**2 + (a[1]-b[1])**2 for a,b in zip(projected, values))
            # Break symmetric ties in geometric order, never BMesh loop order.
            candidates.append((round(error, 10), tuple(values[i] for i in order), values))
    return min(candidates, key=lambda item: item[:2])[2]


def map_face_to_tile(face, uv_layer, col, row, tile_w_px, tile_h_px,
                     atlas_width, atlas_height,
                     rotation=0, flip_u=False, flip_v=False, inset_px=0.0,
                     matrix_world=None, local_uvs=None):
    """Map a face's loops to the calculated UV coordinates of the target tile."""
    if atlas_width <= 0 or atlas_height <= 0 or tile_w_px <= 0 or tile_h_px <= 0:
        return

    # The active tile is stored independently of the grid, so raising Tile Size
    # can leave it pointing past the last column or row.  Clamp here - the one
    # place every mapping path goes through - so UVs can never land outside the
    # atlas.
    col = max(0, min(max(1, int(atlas_width / tile_w_px)) - 1, col))
    row = max(0, min(max(1, int(atlas_height / tile_h_px)) - 1, row))

    u_start, v_start, span_u, span_v = tile_uv_bounds(
        col, row, tile_w_px, tile_h_px, atlas_width, atlas_height, inset_px,
    )

    num_verts = len(face.loops)
    if num_verts == 0:
        return

    local_uvs = local_uvs if local_uvs is not None else upright_face_uvs(face, matrix_world)
    for loop, (u_local, v_local) in zip(face.loops, local_uvs):
        u_local, v_local = apply_tile_transform(u_local, v_local, rotation, flip_u, flip_v)
        loop[uv_layer].uv = (
            u_start + u_local * span_u,
            v_start + v_local * span_v,
        )


def connected_face_patches(faces):
    """Selected edge-connected components; touching at one vertex is not a patch."""
    remaining = set(faces)
    for seed in faces:
        if seed not in remaining:
            continue
        remaining.remove(seed)
        patch = []
        pending = deque([seed])
        while pending:
            face = pending.popleft()
            patch.append(face)
            for edge in face.edges:
                for neighbour in edge.link_faces:
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        pending.append(neighbour)
        yield patch


class PatchChart(dict):
    """Normalized UVs plus their original width/height for optional proportional fitting."""

    def __init__(self, values, aspect_ratio=1.0):
        super().__init__(values)
        self.aspect_ratio = float(aspect_ratio)


def fit_patch_chart(chart):
    """Rotate to a compact bounding box, then use both dimensions of the tile."""
    if not chart or not all(math.isfinite(value) for uv in chart.values() for value in uv):
        raise ValueError("Unfold did not produce a valid UV chart")
    # box_fit_2d uses float32 vectors. Centre and normalize in Python first so
    # a valid chart is not lost to a large offset or a very small/large scale.
    origin = next(iter(chart.values()))
    chart = {loop: (uv[0] - origin[0], uv[1] - origin[1])
             for loop, uv in chart.items()}
    extent = max(abs(value) for uv in chart.values() for value in uv)
    if not math.isfinite(extent) or extent == 0.0:
        raise ValueError("Unfold produced a collapsed chart; check the selected faces")
    chart = {loop: (uv[0] / extent, uv[1] / extent) for loop, uv in chart.items()}
    points = [mathutils.Vector(uv) for uv in chart.values()]
    angle = mathutils.geometry.box_fit_2d(points)
    candidates = []
    for turn in (0.0, angle, -angle):
        c, s = math.cos(turn), math.sin(turn)
        rotated = {loop: (c * uv[0] - s * uv[1], s * uv[0] + c * uv[1])
                   for loop, uv in chart.items()}
        u0 = min(uv[0] for uv in rotated.values())
        v0 = min(uv[1] for uv in rotated.values())
        width = max(uv[0] for uv in rotated.values()) - u0
        height = max(uv[1] for uv in rotated.values()) - v0
        candidates.append((width * height, rotated, u0, v0, width, height))
    _, rotated, u0, v0, width, height = min(candidates, key=lambda item: item[0])
    if min(width, height) <= max(width, height) * 1e-6:
        raise ValueError("Unfold produced a collapsed chart; check the selected faces")
    return PatchChart({loop: ((uv[0] - u0) / width, (uv[1] - v0) / height)
                       for loop, uv in rotated.items()}, aspect_ratio=width / height)


def unfold_patch_loop_uvs(faces, matrix_world):
    """Unwrap an isolated copy, keeping the user's Edit Mode and BMesh intact.

    Only the resulting per-corner coordinates are returned. Marked seams are
    copied to the temporary mesh; no geometry or seams are added to the source.
    """
    faces = list(faces)
    selected = set(faces)
    edges = {edge for face in faces for edge in face.edges}
    if not faces:
        raise ValueError("Select faces to unfold")
    if any(sum(face in selected for face in edge.link_faces) > 2 for edge in edges):
        raise ValueError("Unfold needs a manifold patch; split the selection at junctions")
    if not any(edge.seam or sum(face in selected for face in edge.link_faces) == 1 for edge in edges):
        raise ValueError("Closed patches need a UV seam; mark a cut or select an open area")

    vertices = list(dict.fromkeys(vertex for face in faces for vertex in face.verts))
    indices = {vertex: i for i, vertex in enumerate(vertices)}
    # UVs do not depend on object translation. Applying the complete matrix
    # first rounds away small edges far from the world origin. Compute the
    # transformed local differences in Python precision, then normalize before
    # Blender's float32 area tests and unwrap solver see the coordinates.
    linear = matrix_world.to_3x3()
    origin = tuple(vertices[0].co)
    offsets = [tuple(float(vertex.co[i]) - origin[i] for i in range(3))
               for vertex in vertices]
    coordinates = [tuple(sum(float(linear[i][j]) * offset[j] for j in range(3))
                         for i in range(3)) for offset in offsets]
    if not all(math.isfinite(value) for point in coordinates for value in point):
        raise ValueError("Patch contains invalid coordinates or object scale")
    extent = max(abs(value) for point in coordinates for value in point)
    if extent == 0.0:
        raise ValueError("Unfold cannot map a zero-area face; repair the selected geometry")
    points = [mathutils.Vector(tuple(value / extent for value in point)) for point in coordinates]
    # Reject zero-area source faces before the solver can invent a chart.
    for face in faces:
        coordinates = [points[indices[vertex]] for vertex in face.verts]
        area = mathutils.Vector((0.0, 0.0, 0.0))
        for i in range(1, len(coordinates) - 1):
            area += (coordinates[i] - coordinates[0]).cross(coordinates[i + 1] - coordinates[0])
        if area.length_squared == 0.0:
            raise ValueError("Unfold cannot map a zero-area face; repair the selected geometry")

    # Even allocation/setup can fail (for example on a large selection). Own
    # every temporary datablock immediately, before the next allocation starts.
    scene = mesh = obj = None
    override = None
    try:
        scene = bpy.data.scenes.new("__Sprotile_Unfold")
        mesh = bpy.data.meshes.new("__Sprotile_Unfold")
        obj = bpy.data.objects.new("__Sprotile_Unfold", mesh)
        layer = scene.view_layers[0]
        scene.collection.objects.link(obj)
        layer.objects.active = obj
        obj.select_set(True, view_layer=layer)
        override = dict(scene=scene, view_layer=layer, object=obj, active_object=obj,
                        selected_objects=[obj], selected_editable_objects=[obj])
        mesh.from_pydata(points, [], [[indices[vertex] for vertex in face.verts] for face in faces])
        mesh.uv_layers.new(name="UVMap")
        seams = {tuple(sorted(indices[vertex] for vertex in edge.verts)) for edge in edges if edge.seam}
        for edge in mesh.edges:
            edge.use_seam = tuple(sorted(edge.vertices)) in seams
        with bpy.context.temp_override(**override):
            # Disable nested undo steps: only the final Sprotile operator owns
            # the user's undo entry, never these temporary scene operations.
            bpy.ops.object.mode_set('EXEC_DEFAULT', False, mode='EDIT')
            with bpy.context.temp_override(edit_object=obj):
                bpy.ops.mesh.select_all('EXEC_DEFAULT', False, action='SELECT')
                result = bpy.ops.uv.unwrap('EXEC_DEFAULT', False, method='ANGLE_BASED',
                                           fill_holes=True, correct_aspect=False, margin=0.0)
            if 'FINISHED' not in result:
                raise ValueError("Could not unfold the patch; mark a UV seam or select a smaller area")
            temp_bm = bmesh.from_edit_mesh(mesh)
            temp_bm.faces.ensure_lookup_table()
            temp_bm.faces.index_update()
            temp_bm.verts.index_update()
            uv_layer = temp_bm.loops.layers.uv.active
            uv_by_corner = {(face.index, loop.vert.index): tuple(loop[uv_layer].uv)
                            for face in temp_bm.faces for loop in face.loops}
            chart = {loop: uv_by_corner[(i, indices[loop.vert])]
                     for i, face in enumerate(faces) for loop in face.loops}
            bpy.ops.object.mode_set('EXEC_DEFAULT', False, mode='OBJECT')
    except RuntimeError as exc:
        raise ValueError("Could not unfold the patch; check its faces and UV seams") from exc
    finally:
        try:
            if obj is not None and obj.mode != 'OBJECT':
                with bpy.context.temp_override(**override):
                    bpy.ops.object.mode_set('EXEC_DEFAULT', False, mode='OBJECT')
        finally:
            try:
                if obj is not None:
                    bpy.data.objects.remove(obj, do_unlink=True)
            finally:
                try:
                    if mesh is not None:
                        bpy.data.meshes.remove(mesh)
                finally:
                    if scene is not None:
                        bpy.data.scenes.remove(scene)

    # Blender can report FINISHED while implicitly tearing a closed chart.
    # Only source edges explicitly marked as seams may have discontinuities.
    for edge in edges:
        adjacent = [face for face in edge.link_faces if face in selected]
        if edge.seam or len(adjacent) != 2:
            continue
        for vertex in edge.verts:
            uvs = [chart[next(loop for loop in face.loops if loop.vert == vertex)] for face in adjacent]
            if (mathutils.Vector(uvs[0]) - mathutils.Vector(uvs[1])).length > 1e-4:
                raise ValueError("This patch needs a UV seam; mark a cut or select a smaller open area")
    chart = orient_unfold_chart(fit_patch_chart(chart), faces, matrix_world)
    for face in faces:
        uv = [chart[loop] for loop in face.loops]
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(uv, uv[1:] + uv[:1])))
        if area < 1e-10:
            raise ValueError("Unfold collapsed a face; repair the patch or add a UV seam")
    return chart


def orient_unfold_chart(chart, faces, matrix_world):
    """Anchor the solver's free quarter-turn to the same upright surface frame.

    Rotating the completed chart preserves seams, coverage and distortion.
    A patch with no useful planar reference retains the native orientation.
    """
    try:
        reference = patch_vertex_uvs(faces,matrix_world,'SURFACE')
    except ValueError:
        return chart
    candidates = []
    for rotation in (0,90,180,270):
        rotated = {loop:apply_tile_transform(*uv,rotation,False,False) for loop,uv in chart.items()}
        error = math.fsum((uv[0]-reference[loop.vert][0])**2+(uv[1]-reference[loop.vert][1])**2
                          for loop,uv in rotated.items())
        candidates.append((round(error,8),rotation,rotated))
    candidates.sort(key=lambda item:item[:2])
    if candidates[1][0]-candidates[0][0] <= 1e-6*len(chart):
        return chart  # A symmetric/overlapping reference cannot establish up reliably.
    _,rotation,rotated = candidates[0]
    return PatchChart(rotated,aspect_ratio=1/chart.aspect_ratio if rotation%180 else chart.aspect_ratio)


def existing_patch_loop_uvs(faces, uv_layer, reader=None):
    """Normalize the artist's active UV chart without rotating or unwrapping it.

    Read coordinates per corner, rather than per vertex, so authored seams and
    separately placed UV islands survive.  Aspect is in UV units; the caller
    converts it to texture pixels when Keep Proportions needs the atlas size.
    """
    faces = list(faces)
    if not faces:
        raise ValueError("Select faces to fit their existing UVs")
    if uv_layer is None:
        raise ValueError("Existing UVs needs an active UV map; unwrap or project the patch first")
    read = reader or (lambda loop: loop[uv_layer].uv)
    chart = {loop: tuple(read(loop)) for face in faces for loop in face.loops}
    if not chart or not all(math.isfinite(value) for uv in chart.values() for value in uv):
        raise ValueError("Existing UVs contains invalid coordinates; repair the selected UVs")
    u0 = min(uv[0] for uv in chart.values())
    v0 = min(uv[1] for uv in chart.values())
    width = max(uv[0] for uv in chart.values()) - u0
    height = max(uv[1] for uv in chart.values()) - v0
    if width <= 0.0 or height <= 0.0:
        raise ValueError("Existing UVs is collapsed; unwrap or project the patch first")
    normalized = {loop: ((uv[0] - u0) / width, (uv[1] - v0) / height)
                  for loop, uv in chart.items()}
    for face in faces:
        uv = [normalized[loop] for loop in face.loops]
        # Either winding is valid, including deliberately mirrored islands.
        # A bow tie with cancelling lobes is ambiguous, just like a line.
        area = abs(sum(a[0] * b[1] - b[0] * a[1]
                       for a, b in zip(uv, uv[1:] + uv[:1])))
        if area == 0.0:
            raise ValueError("Existing UVs has a zero-area or self-crossing face; repair the selected UVs")
    result = PatchChart(normalized, aspect_ratio=width / height)
    result.source_bounds = (u0, v0, width, height)
    return result


def patch_loop_uvs(faces, matrix_world, projection='UNFOLD', view_rotation=None, uv_layer=None, reader=None):
    """One chart for the selected patch, retaining separate corners at seams."""
    faces = list(faces)
    if projection == 'UNFOLD':
        return unfold_patch_loop_uvs(faces, matrix_world)
    if projection == 'EXISTING':
        return existing_patch_loop_uvs(faces, uv_layer, reader)
    vertices = patch_vertex_uvs(faces, matrix_world, projection, view_rotation)
    return PatchChart({loop: vertices[loop.vert] for face in faces for loop in face.loops},
                      aspect_ratio=vertices.aspect_ratio)


def patch_vertex_uvs(faces, matrix_world, projection='SURFACE', view_rotation=None):
    """One orthographic chart for a patch, with identical UVs at shared vertices.

    SURFACE finds an area-weighted world-space plane, keeping walls upright.
    VIEW uses the viewport's right/up axes, even in a perspective viewport.
    Both fit the combined bounds to 0..1; this is a projection, not an unwrap.
    """
    faces = list(faces)
    vertices = list(dict.fromkeys(vertex for face in faces for vertex in face.verts))
    if not vertices:
        raise ValueError("Select faces to map as a patch")
    # Remove irrelevant translation before float32 matrix math can erase an
    # edge. Normalizing also keeps surface normal calculations stable across
    # very small and very large (including unapplied) object scales.
    linear = matrix_world.to_3x3()
    origin = tuple(vertices[0].co)
    offsets = {vertex: tuple(float(vertex.co[i]) - origin[i] for i in range(3))
               for vertex in vertices}
    coordinates = {vertex: tuple(sum(float(linear[i][j]) * offset[j] for j in range(3))
                                 for i in range(3)) for vertex, offset in offsets.items()}
    if not all(math.isfinite(value) for point in coordinates.values() for value in point):
        raise ValueError("Patch contains invalid coordinates or object scale")
    extent = max(abs(value) for point in coordinates.values() for value in point)
    if extent == 0.0:
        raise ValueError("Patch has no surface area")
    points = {vertex: mathutils.Vector(tuple(value / extent for value in point))
              for vertex, point in coordinates.items()}

    if projection == 'VIEW':
        if view_rotation is None:
            raise ValueError("View projection needs a 3D Viewport")
        axis_u = view_rotation @ mathutils.Vector((1.0, 0.0, 0.0))
        axis_v = view_rotation @ mathutils.Vector((0.0, 1.0, 0.0))
    else:
        normal = mathutils.Vector((0.0, 0.0, 0.0))
        largest = normal.copy()
        for face in faces:
            vertices = list(face.verts)
            origin = points[vertices[0]]
            area_normal = mathutils.Vector((0.0, 0.0, 0.0))
            for i in range(1, len(vertices) - 1):
                area_normal += (points[vertices[i]] - origin).cross(points[vertices[i + 1]] - origin)
            normal += area_normal
            if area_normal.length_squared > largest.length_squared:
                largest = area_normal
        if largest.length_squared == 0.0:
            raise ValueError("Patch has no surface area")
        if normal.length_squared < largest.length_squared * 1e-12:
            normal = largest.copy()
        normal.normalize()
        up = mathutils.Vector((0.0, 0.0, 1.0))
        # Ground-facing slopes keep the ground frame. The former .99 cutoff
        # switched a mere 9-degree ramp into the wall frame and quarter-turned tiles.
        if abs(normal.z) < max(abs(normal.x),abs(normal.y)):
            axis_v = (up - normal * normal.dot(up)).normalized()
            axis_u = axis_v.cross(normal).normalized()
        else:
            right = mathutils.Vector((1.0, 0.0, 0.0))
            axis_u = (right - normal * normal.dot(right)).normalized()
            axis_v = normal.cross(axis_u).normalized()

    origin = next(iter(points.values()))
    projected = {vertex: ((point - origin).dot(axis_u), (point - origin).dot(axis_v))
                 for vertex, point in points.items()}
    u_min = min(uv[0] for uv in projected.values())
    v_min = min(uv[1] for uv in projected.values())
    width = max(uv[0] for uv in projected.values()) - u_min
    height = max(uv[1] for uv in projected.values()) - v_min
    extent = max(width, height)
    # Blender vectors/quaternions use float32; an exactly edge-on view can
    # retain roughly 1e-7 of numerical thickness. Do not stretch that to a tile.
    if not math.isfinite(extent) or min(width, height) <= extent * 1e-6:
        raise ValueError("Patch projects to a line; use Surface projection or turn the view face-on")
    local_uvs = {vertex: ((uv[0] - u_min) / width, (uv[1] - v_min) / height)
                 for vertex, uv in projected.items()}
    # An edge-on diagonal can have nonzero bounds while still having no area.
    area = 0.0
    for face in faces:
        uv = [local_uvs[vertex] for vertex in face.verts]
        area += abs(sum(a[0] * b[1] - b[0] * a[1]
                        for a, b in zip(uv, uv[1:] + uv[:1])))
    if area <= 1e-8:
        raise ValueError("Patch has no projected area; turn the view face-on")
    return PatchChart(local_uvs, aspect_ratio=width / height)


def patch_triangles(faces):
    """Use Blender's mesh tessellation without changing source topology."""
    if all(len(face.loops) == 3 for face in faces):
        return [tuple(face.loops) for face in faces]
    temporary = bmesh.new()
    try:
        vertices = {v: temporary.verts.new(v.co) for v in dict.fromkeys(v for face in faces for v in face.verts)}
        lookup = {}
        for face in faces:
            copy = temporary.faces.new([vertices[v] for v in face.verts])
            original = {vertices[loop.vert]: loop for loop in face.loops}
            lookup[copy] = original
        temporary.normal_update()
        return [tuple(lookup[tri[0].face][loop.vert] for loop in tri)
                for tri in temporary.calc_loop_triangles()]
    finally:
        temporary.free()


def uv_triangle_area(triangle, coordinates):
    a, b, c = (coordinates[loop] for loop in triangle)
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def rectangle_patch_chart(faces, chart, marked_corners=None):
    """Map one disk boundary to a rectangle and solve a positive-weight interior.

    Four ordered boundary vertices become corners. Tessellation ears must be
    corners, otherwise their triangles would collapse onto a straight side.
    Unsupported topology/UV cuts cancel before touching the user's mesh.
    """
    faces = list(faces)
    if getattr(chart, 'rectangular', False):
        return chart
    selected = set(faces)
    vertices = list(dict.fromkeys(v for face in faces for v in face.verts))
    edges = {edge for face in faces for edge in face.edges}
    if not faces or len(vertices)-len(edges)+len(faces) != 1:
        raise ValueError("Fit Rectangle needs one connected patch without holes; select a smaller patch")
    if len(list(connected_face_patches(faces))) != 1:
        raise ValueError("Fit Rectangle needs one connected patch; map disconnected islands separately")
    outgoing, incoming, boundary_edges = {}, {}, set()
    for face in faces:
        for loop in face.loops:
            linked = [f for f in loop.edge.link_faces if f in selected]
            if len(linked) > 2:
                raise ValueError("Fit Rectangle needs a manifold patch")
            if len(linked) == 2:
                other = next(l for l in linked[0 if linked[1] == face else 1].loops if l.edge == loop.edge)
                if other.vert == loop.vert:
                    raise ValueError("Fit Rectangle needs consistent face winding; recalculate normals first")
                continue
            a, b = loop.vert, loop.link_loop_next.vert
            if a in outgoing or b in incoming:
                raise ValueError("Fit Rectangle needs a single boundary without touching branches")
            outgoing[a], incoming[b] = b, a
            boundary_edges.add(frozenset((a, b)))
    if len(outgoing) < 4 or set(outgoing) != set(incoming):
        raise ValueError("Fit Rectangle needs at least four boundary vertices")
    points = {}
    for face in faces:
        for loop in face.loops:
            uv = tuple(chart[loop])
            if not all(math.isfinite(x) for x in uv):
                raise ValueError("Fit Rectangle needs finite UV coordinates")
            if loop.vert in points and max(abs(a-b) for a,b in zip(points[loop.vert],uv)) > 1e-5:
                raise ValueError("Fit Rectangle needs one continuous UV chart; split at UV seams or use Surface")
            points[loop.vert] = uv
    low = tuple(min(uv[i] for uv in points.values()) for i in (0,1))
    span = tuple(max(uv[i] for uv in points.values())-low[i] for i in (0,1))
    if min(span) <= max(span)*1e-8:
        raise ValueError("Fit Rectangle needs a chart with width and height")
    points = {v: tuple((uv[i]-low[i])/span[i] for i in (0,1)) for v,uv in points.items()}
    start = min(outgoing, key=lambda v: (points[v], tuple(v.co)))
    boundary, visited, current = [], set(), start
    while current not in visited:
        boundary.append(current)
        visited.add(current)
        current = outgoing[current]
    if current != start or len(boundary) != len(outgoing):
        raise ValueError("Fit Rectangle needs one boundary; holes and separate islands cannot fill one tile")
    signed = sum(points[a][0]*points[b][1]-points[b][0]*points[a][1]
                 for a,b in zip(boundary,boundary[1:]+boundary[:1]))
    if abs(signed) <= 1e-10:
        raise ValueError("Fit Rectangle cannot use an overlapping or collapsed chart")
    orientation = 1 if signed > 0 else -1
    if orientation < 0:
        boundary.reverse()
    triangles = patch_triangles(faces)
    normalized = {loop: points[loop.vert] for face in faces for loop in face.loops}
    if any(uv_triangle_area(tri, normalized)*orientation <= 1e-12 for tri in triangles):
        raise ValueError("Fit Rectangle needs an unfolded chart without flipped or collapsed triangles")
    adjacency = {v:set() for v in vertices}
    required = set()
    for tri in triangles:
        a,b,c = (loop.vert for loop in tri)
        for v,w in ((a,b),(b,c),(c,a)):
            adjacency[v].add(w);adjacency[w].add(v)
        for v,w,z in ((a,b,c),(b,c,a),(c,a,b)):
            if frozenset((v,w)) in boundary_edges and frozenset((w,z)) in boundary_edges:
                required.add(w)
    if len(required) > 4:
        raise ValueError("Fit Rectangle needs more than four corners for this triangulation; add interior geometry or split the patch")
    corners = ((0.0,0.0),(1.0,0.0),(1.0,1.0),(0.0,1.0))
    count = len(boundary)
    def cost(vertex, corner):
        return sum((a-b)**2 for a,b in zip(points[vertex],corner))
    starts = sorted(range(count),key=lambda i:(cost(boundary[i],corners[0]),points[boundary[i]]))[:32]
    starts = sorted(set(starts) | {i for i,v in enumerate(boundary) if v in required})
    candidates = []
    for offset in (starts if marked_corners is None else ()):
        ring = boundary[offset:]+boundary[:offset]
        # Ordered dynamic programming: exactly four corners, including every ear.
        dp = {1:(cost(ring[0],corners[0]),(0,))}
        for i,vertex in enumerate(ring[1:],1):
            previous = dp
            dp = {} if vertex in required else dict(previous)
            for size,(score,indices) in previous.items():
                if size == 4:
                    continue
                candidate = (score+cost(vertex,corners[size]),indices+(i,))
                if size+1 not in dp or candidate < dp[size+1]:
                    dp[size+1] = candidate
        if 4 in dp:
            candidates.append((dp[4][0],offset,dp[4][1]))
    if marked_corners is not None:
        if len(marked_corners)!=4 or len(set(marked_corners))!=4 or any(v not in outgoing for v in marked_corners):
            raise ValueError('Marked corners must be four different vertices on this patch boundary')
        if not required.issubset(set(marked_corners)):
            raise ValueError('These corners would collapse boundary triangles; use Automatic or add interior geometry')
        offset=boundary.index(marked_corners[0]);indices=tuple((boundary.index(v)-offset)%count for v in marked_corners)
        if not (indices[0]==0<indices[1]<indices[2]<indices[3]):
            raise ValueError('Mark BL, BR, TR, TL in boundary order; crossing or mirrored corners cannot be fitted safely')
        candidates=[(0.,offset,indices)]
    if not candidates:
        raise ValueError("Fit Rectangle could not find four safe boundary corners")
    fixed = set(boundary)
    interior = [v for v in vertices if v not in fixed]
    indices = {v:i for i,v in enumerate(interior)}
    neighbours = [[indices[w] for w in adjacency[v] if w in indices] for v in interior]
    degrees = [len(adjacency[v]) for v in interior]

    def solve(values, axis):
        if not interior:
            return
        rhs = [sum(values[w][axis] for w in adjacency[v] if w in fixed) for v in interior]
        x = [points[v][axis] for v in interior]
        def multiply(vector):
            return [degrees[i]*value-sum(vector[j] for j in neighbours[i]) for i,value in enumerate(vector)]
        def dot(a,b):
            return math.fsum(x*y for x,y in zip(a,b))
        residual = [b-a for b,a in zip(rhs,multiply(x))]
        z = [r/d for r,d in zip(residual,degrees)]
        direction = list(z);rz = dot(residual,z)
        tolerance = 1e-20*max(1.0,dot(rhs,rhs))
        for _ in range(min(4096,4*len(interior)+64)):
            if dot(residual,residual) <= tolerance:
                break
            product = multiply(direction)
            denominator = dot(direction,product)
            if denominator <= 0:
                raise ValueError("Fit Rectangle could not solve the interior; split the patch")
            alpha = rz/denominator
            x = [a+alpha*b for a,b in zip(x,direction)]
            residual = [a-alpha*b for a,b in zip(residual,product)]
            z = [r/d for r,d in zip(residual,degrees)]
            new_rz = dot(residual,z)
            beta = new_rz/rz
            direction = [a+beta*b for a,b in zip(z,direction)]
            rz = new_rz
        else:
            raise ValueError("Fit Rectangle did not converge; select a smaller patch")
        for vertex,value in zip(interior,x):
            values[vertex][axis] = value

    for _score,offset,anchors in sorted(candidates):
        ring = boundary[offset:]+boundary[:offset]
        values = {v:[0.0,0.0] for v in vertices}
        valid = True
        for side in range(4):
            begin,end = anchors[side], (anchors[side+1] if side < 3 else count)
            segment = ring[begin:end+1] if side < 3 else ring[begin:]+[ring[0]]
            lengths = [math.hypot(points[b][0]-points[a][0],points[b][1]-points[a][1])
                       for a,b in zip(segment,segment[1:])]
            if not lengths or min(lengths) <= 1e-12:
                valid = False;break
            distance,total = 0.0,sum(lengths)
            a,b = corners[side],corners[(side+1)%4]
            for i,vertex in enumerate(segment[:-1]):
                t = distance/total
                values[vertex] = [a[j]+(b[j]-a[j])*t for j in (0,1)]
                distance += lengths[i]
        if not valid:
            continue
        solve(values,0);solve(values,1)
        result = PatchChart({loop:tuple(values[loop.vert]) for face in faces for loop in face.loops})
        areas = [uv_triangle_area(tri,result)*orientation for tri in triangles]
        if min(areas) <= 1e-12 or abs(sum(areas)-2.0) > 1e-6:
            continue
        result.rectangular = True
        result.triangles = triangles
        result.orientation = orientation
        return result
    raise ValueError("Fit Rectangle would collapse or flip triangles; add interior geometry or split the patch")


def patch_tile_uvs(faces, local_uvs, col, row, tile_w_px, tile_h_px,
                   atlas_width, atlas_height, rotation=0, flip_u=False,
                   flip_v=False, inset_px=0.0, projection='SURFACE', fit_mode='FILL',
                   keep_orientation=False):
    """Plan the actual float32 destination without touching a UV layer."""
    faces = list(faces)
    validate_tile_dimensions(tile_w_px, tile_h_px, atlas_width, atlas_height)
    if fit_mode == 'RECTANGLE':
        local_uvs = rectangle_patch_chart(faces, local_uvs,getattr(local_uvs,'rectangle_corners',None))
    if projection == 'EXISTING' and not keep_orientation:
        # Reusing a chart must not apply the same latched rotation or mirror
        # again on every tile swap. Edit its orientation in the UV Editor.
        # Blends opt out: they read the untouched original map and write
        # their own, and a captured A/B turn is a deliberate per-tile choice.
        rotation, flip_u, flip_v = 0, False, False
    col = max(0, min(max(1, int(atlas_width / tile_w_px)) - 1, col))
    row = max(0, min(max(1, int(atlas_height / tile_h_px)) - 1, row))
    u_start, v_start, span_u, span_v = tile_uv_bounds(
        col, row, tile_w_px, tile_h_px, atlas_width, atlas_height, inset_px)
    scale_u = scale_v = 1.0
    if fit_mode == 'KEEP_SHAPE':
        ratio = getattr(local_uvs, 'aspect_ratio', 1.0)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("Patch proportions must be finite and positive")
        if int(rotation) % 180 == 90:
            ratio = 1.0 / ratio
        target = (span_u * atlas_width) / (span_v * atlas_height)
        if ratio > target:
            scale_v = target / ratio
        else:
            scale_u = ratio / target
    result = {}
    for face in faces:
        for loop in face.loops:
            u, v = apply_tile_transform(*local_uvs[loop], rotation, flip_u, flip_v)
            u = (u - 0.5) * scale_u + 0.5
            v = (v - 0.5) * scale_v + 0.5
            result[loop] = tuple(mathutils.Vector((u_start + u * span_u, v_start + v * span_v)))
    if not all(math.isfinite(value) for uv in result.values() for value in uv):
        raise ValueError("Patch destination contains invalid UV coordinates")
    if fit_mode == 'RECTANGLE':
        orientation = local_uvs.orientation * (-1 if flip_u != flip_v else 1)
        if any(uv_triangle_area(tri,result)*orientation <= 0 for tri in local_uvs.triangles):
            raise ValueError("Fit Rectangle would collapse at this atlas precision; reduce inset or use a larger tile")
    if projection in {'UNFOLD', 'EXISTING'}:
        for face in faces:
            uv = [result[loop] for loop in face.loops]
            u0, v0 = uv[0]
            area = abs(sum((a[0] - u0) * (b[1] - v0) - (b[0] - u0) * (a[1] - v0)
                           for a, b in zip(uv, uv[1:] + uv[:1])))
            if area == 0.0:
                raise ValueError("Mapped UVs would collapse a face; reduce inset, use Fill Tile, or select a smaller area")
    return result


def map_patch_to_tile(faces, uv_layer, matrix_world, col, row,
                      tile_w_px, tile_h_px, atlas_width, atlas_height,
                      rotation=0, flip_u=False, flip_v=False, inset_px=0.0,
                      projection='SURFACE', view_rotation=None, local_uvs=None,
                      fit_mode='FILL'):
    """Fit one whole patch into a tile; the inset belongs to its outside bounds."""
    faces = list(faces)
    validate_tile_dimensions(tile_w_px, tile_h_px, atlas_width, atlas_height)
    if local_uvs is None:
        if projection == 'EXISTING':
            local_uvs = patch_loop_uvs(faces, matrix_world, projection, view_rotation, uv_layer=uv_layer)
            local_uvs.aspect_ratio *= atlas_width / atlas_height
        else:
            local_uvs = patch_loop_uvs(faces, matrix_world, projection, view_rotation)
    coordinates = patch_tile_uvs(faces, local_uvs, col, row, tile_w_px, tile_h_px,
                                  atlas_width, atlas_height, rotation, flip_u, flip_v,
                                  inset_px, projection, fit_mode)
    for face in faces:
        for loop in face.loops:
            loop[uv_layer].uv = coordinates[loop]


def validate_plain_material_uv(obj, bm, material):
    """Check that an edit will affect the UV map sampled by the atlas shader."""
    active = bm.loops.layers.uv.active
    if material.get(SHARED_MATERIAL_TAG):
        texture = shared_nodes(material).get('original')
    else:
        image = image_from_material(material)
        candidates = [node for node in material.node_tree.nodes
                      if node.type=='TEX_IMAGE' and node.image==image and node.outputs['Color'].is_linked] if material.node_tree else []
        texture = candidates[0] if len(candidates)==1 else None
    if texture is None or texture.type!='TEX_IMAGE':
        raise ValueError('Cannot identify the atlas UV input; check the source atlas material first')
    required = next((layer.name for layer in obj.data.uv_layers if layer.active_render), None)
    vector = texture.inputs['Vector']
    if vector.is_linked:
        source = vector.links[0].from_node
        if source.type=='UVMAP':required = source.uv_map or required
        elif not (source.type=='TEX_COORD' and vector.links[0].from_socket.name=='UV'):
            raise ValueError('This atlas uses a custom coordinate input; choose a directly mapped atlas material first')
    if active is None or active.name!=required:
        raise ValueError('Material reads UV map "'+str(required)+'"; activate that UV map first')


def plain_paint_binding(obj, bm, face):
    """Validate replacing a visible blend with ordinary atlas mapping."""
    active = bm.loops.layers.uv.active
    if active is not None and active.name in INTERNAL_BLEND_UVS:
        raise ValueError('Select the original UV map before ordinary painting; blend UV maps are internal')
    material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
    if material is None:
        return False, face.material_index
    if material.get('sprotile_blend_material'):
        history = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
        slot = history_slot(obj, face[history]) if history is not None else -1
        if not 0 <= slot < len(obj.material_slots):
            raise ValueError((history_conflict(obj, face[history]) if history is not None else None) or
                             'This separate blend has no valid original material; assign the source atlas material before painting')
        original = obj.material_slots[slot].material
        if (original is None or original.get('sprotile_blend_material')
                or obj.material_slots[slot].link != 'DATA'
                or image_from_material(original) != image_from_material(material)):
            raise ValueError('Restore the source atlas material before painting over this separate blend')
        validate_plain_material_uv(obj,bm,original)
        return True, slot
    if material.get(SHARED_MATERIAL_TAG):
        membership = bm.faces.layers.int.get(SHARED_PATCH_ID)
        factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
        enabled = (membership is not None and face[membership] > 0) or (
            factor is not None and any(loop[factor].uv.y != 0 for loop in face.loops))
        if enabled:validate_plain_material_uv(obj,bm,material)
        return bool(enabled), face.material_index
    return False, face.material_index


def clear_painted_blend(bm, face, binding):
    if not binding[0]:
        return
    face.material_index = binding[1]
    for name in (SHARED_PATCH_ID, BLEND_ORIGINAL_MATERIAL, PATH_PAINT_ATTRIBUTE):
        layer = bm.faces.layers.int.get(name)
        if layer is not None:face[layer] = 0
    factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
    if factor is not None:
        for loop in face.loops:loop[factor].uv = (0.0,0.0)


def paint_blend_snapshot(bm, face):
    metadata = {name:face[layer] for name in (SHARED_PATCH_ID, BLEND_ORIGINAL_MATERIAL, PATH_PAINT_ATTRIBUTE)
                for layer in (bm.faces.layers.int.get(name),) if layer is not None}
    factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
    return face.material_index, metadata, [tuple(loop[factor].uv) for loop in face.loops] if factor else None


def map_selected_patches_to_active(context):
    """Plan every chart first, so an invalid patch cancels without partial edits."""
    scene = context.scene
    projection = scene.sprotile_patch_projection
    view_rotation = None
    if projection == 'VIEW':
        rv3d = get_region_3d(context.area)
        if rv3d is None or is_quad_view(context.area):
            raise ValueError("View projection needs a single 3D Viewport; use Surface in Quad View")
        view_rotation = rv3d.view_rotation.copy()

    plans = []
    existing_groups = defaultdict(list)
    seen_meshes = set()
    bindings = {}
    tile_dimensions = get_tile_dims(scene)
    for obj in mapping_edit_mesh_objects(context):
        if obj.data.as_pointer() in seen_meshes:
            continue
        seen_meshes.add(obj.data.as_pointer())
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.index_update()
        by_atlas = defaultdict(list)
        images = {}
        slot_images = {}
        for face in bm.faces:
            if not face.select or face.hide:
                continue
            bindings[(obj.data.as_pointer(),face.index)] = plain_paint_binding(obj,bm,face)
            if face.material_index not in slot_images:
                image = atlas_for_material_index(obj, face.material_index)
                dimensions = image_size(image)
                if image is None or min(dimensions) <= 0:
                    raise ValueError("Each selected patch needs a material with a loaded atlas image")
                validate_tile_dimensions(*tile_dimensions, *dimensions)
                slot_images[face.material_index] = image
            image = slot_images[face.material_index]
            key = image.as_pointer()
            images[key] = image
            by_atlas[key].append(face)
        for key, faces in by_atlas.items():
            image = images[key]
            if projection == 'EXISTING':
                chart = patch_loop_uvs(faces, obj.matrix_world, projection,
                                      uv_layer=bm.loops.layers.uv.active,
                                      reader=existing_uv_reader(obj.data, bm, bm.loops.layers.uv.active))
                coordinates = [[chart[loop] for loop in face.loops] for face in faces]
                existing_groups[key].append((obj, bm, faces, image, coordinates, chart.source_bounds))
                continue
            for patch in connected_face_patches(faces):
                # Validate before adding a UV layer or changing any mesh.
                chart = patch_loop_uvs(patch, obj.matrix_world, projection, view_rotation)
                # Creating the first UV layer can relocate BMLoop customdata.
                # Cache plain values in face-loop order, never old loop keys.
                coordinates = [[chart[loop] for loop in face.loops] for face in patch]
                plans.append((obj, bm, patch, image, coordinates,
                              getattr(chart, 'aspect_ratio', 1.0)))

    # Existing UVs already supplies a common coordinate system. Retain one
    # layout per atlas across selected islands and separate objects, rather
    # than stretching each disconnected panel into a fresh copy of the tile.
    for entries in existing_groups.values():
        u0 = min(entry[5][0] for entry in entries)
        v0 = min(entry[5][1] for entry in entries)
        width = max((entry[5][0] - u0) + entry[5][2] for entry in entries)
        height = max((entry[5][1] - v0) + entry[5][3] for entry in entries)
        atlas_width, atlas_height = image_size(entries[0][3])
        aspect_ratio = (width / height) * (atlas_width / atlas_height)
        for obj, bm, faces, image, coordinates, bounds in entries:
            chart_u0, chart_v0, chart_width, chart_height = bounds
            coordinates = [[(((chart_u0 - u0) + uv[0] * chart_width) / width,
                             ((chart_v0 - v0) + uv[1] * chart_height) / height)
                            for uv in values] for values in coordinates]
            plans.append((obj, bm, faces, image, coordinates, aspect_ratio))

    tile_w_px, tile_h_px = get_tile_dims(scene)
    rotation, flip_u, flip_v = get_tile_transform(scene)
    # Validate the stored precision of every destination before allocating
    # even the first source UV layer or changing a previously planned mesh.
    destinations = []
    for obj, bm, patch, image, coordinates, aspect_ratio in plans:
        chart = PatchChart({loop: uv for face, values in zip(patch, coordinates)
                            for loop, uv in zip(face.loops, values)}, aspect_ratio=aspect_ratio)
        if scene.sprotile_patch_fit=='RECTANGLE' and scene.sprotile_rectangle_corner_mode=='MARKED':
            chart.rectangle_corners=read_rectangle_corners(obj.data,bm,patch)
        target_uvs = patch_tile_uvs(patch, chart, scene.sprotile_active_col, scene.sprotile_active_row,
                       tile_w_px, tile_h_px, *image_size(image), rotation, flip_u, flip_v,
                       get_uv_inset(scene), projection, scene.sprotile_patch_fit)
        destinations.append([(face.index, [target_uvs[loop] for loop in face.loops]) for face in patch])
    changed = {}
    for (obj, bm, patch, image, coordinates, aspect_ratio), destination in zip(plans, destinations):
        uv_layer = bm.loops.layers.uv.verify()
        # First UV allocation may relocate loop wrappers. Retain only indices
        # and validated numeric coordinates across it, then use fresh loops.
        bm.faces.ensure_lookup_table()
        for index, values in destination:
            clear_painted_blend(bm,bm.faces[index],bindings[(obj.data.as_pointer(),index)])
            for loop, uv in zip(bm.faces[index].loops, values):
                loop[uv_layer].uv = uv
        _recent_tiles.remember(scene, image, scene.sprotile_active_col, scene.sprotile_active_row)
        changed[obj.data.as_pointer()] = obj.data
    for mesh in changed.values():
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    if plans:
        tag_redraw_view3d()
    return bool(plans)


def map_selected_faces_to_active(context, mapping_mode=None):
    """Map the selected faces of every mesh in Edit Mode to the active tile.

    Faces are grouped by material slot so a selection spanning two atlases is
    mapped against the correct one for each face.
    """
    scene = context.scene
    mode = mapping_mode or scene.sprotile_mapping_mode
    if mode == 'PATCH':
        return map_selected_patches_to_active(context)
    col = scene.sprotile_active_col
    row = scene.sprotile_active_row
    tile_w_px, tile_h_px = get_tile_dims(scene)
    rotation, flip_u, flip_v = get_tile_transform(scene)
    inset_px = get_uv_inset(scene)

    plans = []
    seen_meshes = set()
    collapsed = 0
    for obj in mapping_edit_mesh_objects(context):
        me = obj.data
        if me.as_pointer() in seen_meshes:
            continue
        seen_meshes.add(me.as_pointer())
        bm = bmesh.from_edit_mesh(me)
        selected = [f for f in bm.faces if f.select and not f.hide]
        if not selected:
            continue

        by_material = defaultdict(list)
        bindings = {}
        for face in selected:
            bindings[face] = plain_paint_binding(obj,bm,face)
            by_material[face.material_index].append(face)

        orientation = paint_orientation_matrix(obj)
        for material_index, faces in by_material.items():
            image = atlas_for_material_index(obj, material_index)
            if image is None or min(image_size(image)) <= 0:
                raise ValueError("Each selected face needs a material with a loaded atlas image")
            atlas_width, atlas_height = image_size(image)
            validate_tile_dimensions(tile_w_px, tile_h_px, atlas_width, atlas_height)
            # A zero-area face has no upright direction. Skip it as the brush
            # does, rather than cancelling the whole selection over one sliver.
            usable, coordinates = [], []
            for face in faces:
                try:
                    coordinates.append(upright_face_uvs(face, orientation))
                except ValueError:
                    collapsed += 1
                    continue
                usable.append(face)
            if usable:
                plans.append((me, bm, usable, image, atlas_width, atlas_height, coordinates,
                              [bindings[f] for f in usable]))
    if not plans and collapsed:
        raise ValueError("The selected faces have no area to map")

    changed = {}
    for me, bm, faces, image, atlas_width, atlas_height, coordinates, bindings in plans:
        uv_layer = bm.loops.layers.uv.verify()
        for face, local_uvs, binding in zip(faces, coordinates, bindings):
            clear_painted_blend(bm,face,binding)
            map_face_to_tile(face, uv_layer, col, row, tile_w_px, tile_h_px,
                             atlas_width, atlas_height, rotation, flip_u, flip_v,
                             inset_px=inset_px, local_uvs=local_uvs)
        _recent_tiles.remember(scene, image, col, row)
        changed[me.as_pointer()] = me
    for me in changed.values():
        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
    if plans:
        tag_redraw_view3d()
    return bool(plans)


# ---------------------------------------------------------------------------
# Sprotile Map Selected Operator (Bound to Keymap)
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_map_selected(bpy.types.Operator):
    """Map the current tile onto the selected faces: Per Face, or with the 4 key the Click and 4 Key Map mode"""
    bl_idname = "mesh.sprotile_map_selected"
    bl_label = "Map Current Tile"
    bl_options = {'REGISTER', 'UNDO'}

    # SKIP_SAVE: the 4 key sets no mode. Without it, one click on Map Per Face
    # became the remembered value and the 4 key ignored the sidebar's Patch.
    mapping_mode: EnumProperty(
        name="Mapping",
        items=[('DEFAULT', "Use Sidebar Mode", "Use the current selection mapping mode"),
               ('PER_FACE', "Per Face", "Map a separate tile onto every selected face"),
               ('PATCH', "Patch", "Unfold or project each connected selection across one tile")],
        default='DEFAULT',
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        mode = context.scene.sprotile_mapping_mode if self.mapping_mode == 'DEFAULT' else self.mapping_mode
        try:
            mapped = map_selected_faces_to_active(context, mode)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not mapped:
            self.report({'WARNING'}, "Ensure a mesh is in Edit Mode, faces are selected, and material has a texture.")
            return {'CANCELLED'}
        self.report({'INFO'}, "Mapped connected patches to active tile" if mode == 'PATCH'
                    else "Mapped selected faces to active tile")
        return {'FINISHED'}


class MESH_OT_sprotile_map_patch(bpy.types.Operator):
    """Fit an existing UV layout, or unfold/project selected connected faces (experimental)"""
    bl_idname = "mesh.sprotile_map_patch"
    bl_label = "Map as Patch"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        try:
            mapped = map_selected_faces_to_active(context, 'PATCH')
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not mapped:
            self.report({'WARNING'}, "Select faces with an atlas material in Edit Mode")
            return {'CANCELLED'}
        self.report({'INFO'}, "Mapped connected patches to active tile")
        return {'FINISHED'}


class MESH_OT_sprotile_picker_map(bpy.types.Operator):
    """Map selected faces to the tile clicked in the atlas picker"""
    bl_idname = "mesh.sprotile_picker_map"
    bl_label = "Map Clicked Tile"
    # Each picker mapping is its own undo step. REGISTER keeps F9 / Adjust Last
    # Operation from redoing an older step and discarding this one; with no
    # visible properties, no redo panel opens over the picker's corner.
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return is_editable_mesh(context.active_object)

    def execute(self, context):
        mode = context.scene.sprotile_mapping_mode
        try:
            mapped = map_selected_faces_to_active(context, mode)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not mapped:
            self.report({'WARNING'}, "Select faces in Edit Mode on a mesh with an atlas material")
            return {'CANCELLED'}
        self.report({'INFO'}, "Mapped connected patches to active tile" if mode == 'PATCH'
                    else "Mapped selected faces to active tile")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Orientation of the tile prepared for the next paint or mapping operation.
# ---------------------------------------------------------------------------

class MESH_OT_sprotile_rotate(bpy.types.Operator):
    """Turn the tile for the next paint or mapping operation in 90 degree steps"""
    bl_idname = "mesh.sprotile_rotate"
    bl_label = "Turn Current Tile"
    bl_options = {'REGISTER'}

    steps: IntProperty(
        name="Steps",
        description="Number of 90 degree steps; negative rotates the other way",
        default=1,
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        cycle_rotation(context.scene, self.steps)
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_flip(bpy.types.Operator):
    """Flip the current tile for the next paint or mapping"""
    bl_idname = "mesh.sprotile_flip"
    bl_label = "Flip Current Tile"
    bl_options = {'REGISTER'}

    axis: EnumProperty(
        name="Axis",
        items=[('U', "U", "Mirror horizontally"), ('V', "V", "Mirror vertically")],
        default='U',
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        scene = context.scene
        if self.axis == 'U':
            scene.sprotile_flip_u = not scene.sprotile_flip_u
        else:
            scene.sprotile_flip_v = not scene.sprotile_flip_v
        tag_redraw_view3d()
        return {'FINISHED'}


class MESH_OT_sprotile_reset_transform(bpy.types.Operator):
    """Clear the current tile's turn and both flips"""
    bl_idname = "mesh.sprotile_reset_transform"
    bl_label = "Reset Turn and Flip"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        scene.sprotile_rotation = '0'
        scene.sprotile_flip_u = False
        scene.sprotile_flip_v = False
        tag_redraw_view3d()
        return {'FINISHED'}


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .draw import tag_redraw_view3d
from .tiles import (
    _recent_tiles, apply_tile_transform, atlas_for_material_index, base_local_uv, cycle_rotation,
    get_tile_dims, get_tile_transform, get_uv_inset, image_from_material, image_size, is_editable_mesh,
    mapping_edit_mesh_objects, tile_uv_bounds, validate_tile_dimensions,
)
from .picker import get_region_3d, is_quad_view
from .blend_settings import (
    BLEND_ORIGINAL_MATERIAL, INTERNAL_BLEND_UVS, SHARED_EDGE_UV, SHARED_FACTOR_UV, SHARED_MATERIAL_TAG,
    PATH_PAINT_ATTRIBUTE, SHARED_PATCH_ID, read_rectangle_corners,
)
from .blend_shader import shared_nodes
from .blend_storage import existing_uv_reader, history_conflict, history_slot
