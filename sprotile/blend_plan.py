"""Planning a blend: captured tiles, patch layouts (Stretch or Per Face), Separate Material blends and
the snapshots that undo a failed Apply.
"""

import bpy
import bmesh
import mathutils


def capture_blend_tile(scene, slot, image):
    width, height = image_size(image)
    if image is None or min(width, height) <= 0:
        raise ValueError("Select a mesh with an atlas image, pick a tile, then capture A or B")
    tile_w, tile_h = get_tile_dims(scene)
    validate_tile_dimensions(tile_w, tile_h, width, height)
    record = getattr(scene, 'sprotile_blend_' + slot.lower())
    record.image = image
    record.col = max(0, min(width // tile_w - 1, scene.sprotile_active_col))
    record.row = max(0, min(height // tile_h - 1, scene.sprotile_active_row))
    record.width_px, record.height_px = tile_w, tile_h
    record.atlas_width, record.atlas_height = width, height
    record.rotation, record.flip_u, record.flip_v = get_tile_transform(scene)
    record.inset_px = get_uv_inset(scene)
    record.is_sampled = False
    record.sample_uv = (1,0,0,0,1,0)
    invalidate_clipboard_slot(scene, slot.lower())


def validated_blend_tile(record):
    image = record.image
    width, height = image_size(image)
    if image is None or min(width, height) <= 0:
        raise ValueError("Pick a loaded atlas and capture both texture A and texture B")
    if (width, height) != (record.atlas_width, record.atlas_height):
        raise ValueError("A captured atlas changed size; capture its blend tile again")
    if record.is_sampled:
        sample_uv_bounds(record.sample_uv)
        return {name: (tuple(getattr(record,name)) if name == 'sample_uv' else getattr(record,name)) for name in BLEND_TILE_FIELDS}
    validate_tile_dimensions(record.width_px, record.height_px, width, height)
    if (record.col + 1) * record.width_px > width or (record.row + 1) * record.height_px > height:
        raise ValueError("A captured blend tile is outside the atlas; capture it again")
    return {name: getattr(record, name) for name in BLEND_TILE_FIELDS}


def active_blend_material(context):
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    if is_editable_mesh(obj):
        bm = bmesh.from_edit_mesh(obj.data)
        face = bm.faces.active
        if face is not None and face.is_valid and face.select and not face.hide:
            if 0 <= face.material_index < len(obj.material_slots):
                material = obj.material_slots[face.material_index].material
                return material if material and material.get('sprotile_blend_material') else None
    material = obj.active_material
    return material if material and material.get('sprotile_blend_material') else None


def build_blend_material(tile_a, tile_b, settings, use_tint):
    """Standard Blender nodes; a saved blend renders without this add-on."""
    material = bpy.data.materials.new("Sprotile Blend: " + tile_a['image'].name + " + " + tile_b['image'].name)
    try:
        if not MATERIALS_ALWAYS_USE_NODES:
            material.use_nodes = True
        material.use_backface_culling = True
        material['sprotile_blend_material'] = True
        material['sprotile_blend_tint_used'] = use_tint
        nodes, links = material.node_tree.nodes, material.node_tree.links
        nodes.clear()

        def node(kind, role, location):
            result = nodes.new(kind)
            result['sprotile_blend_role'] = role
            result.label = role.replace('_', ' ').title()
            result.location = location
            return result

        textures = []
        for index, (tile, role) in enumerate(((tile_a, 'texture_a'), (tile_b, 'texture_b'))):
            uv = node('ShaderNodeUVMap', role + '_uv', (-900, 420 - index * 230))
            uv.uv_map = '' if index == 0 else BLEND_B_UV   # '' samples the render map, which holds tile A
            texture = node('ShaderNodeTexImage', role, (-660, 420 - index * 230))
            texture.image = tile['image']
            texture.interpolation = 'Closest'
            texture.extension = 'EXTEND'
            links.new(uv.outputs[0], texture.inputs[0])
            textures.append(texture)
        mask = node('ShaderNodeAttribute', 'mask_uv', (-900, -160))
        mask.attribute_type = 'GEOMETRY'
        mask.attribute_name = BLEND_MASK_ATTRIBUTE
        axis = node('ShaderNodeSeparateXYZ', 'mask_axis', (-660, -160))
        links.new(mask_socket(mask), axis.inputs[0])
        gradient = node('ShaderNodeMapRange', 'gradient', (-430, -140))
        gradient.data_type = 'FLOAT'
        mix = node('ShaderNodeMixRGB', 'texture_mix', (-120, 320))
        mix.blend_type = 'MIX'
        links.new(gradient.outputs[0], mix.inputs[0])
        links.new(textures[0].outputs[0], mix.inputs[1])
        links.new(textures[1].outputs[0], mix.inputs[2])
        colour = mix.outputs[0]
        if use_tint:
            tint = node('ShaderNodeVertexColor', 'tint', (-120, -20))
            tint.layer_name = 'Tint'
            multiply = node('ShaderNodeMixRGB', 'tint_multiply', (130, 320))
            multiply.blend_type = 'MULTIPLY'
            multiply.inputs[0].default_value = 1.0
            links.new(colour, multiply.inputs[1])
            links.new(tint.outputs[0], multiply.inputs[2])
            double = node('ShaderNodeMixRGB', 'ps1_double', (370, 320))
            double.blend_type = 'ADD'
            double.inputs[0].default_value = 1.0
            links.new(multiply.outputs[0], double.inputs[1])
            links.new(multiply.outputs[0], double.inputs[2])
            colour = double.outputs[0]
        emission = node('ShaderNodeEmission', 'unlit', (620, 320))
        links.new(colour, emission.inputs[0])
        output = node('ShaderNodeOutputMaterial', 'output', (840, 320))
        links.new(emission.outputs[0], output.inputs[0])
        nodes.active = textures[0]
        controls = material.sprotile_blend_controls
        for name in BLEND_CONTROL_FIELDS:
            setattr(controls, name, settings[name])
        update_blend_graph(controls)
        return material
    except Exception:
        bpy.data.materials.remove(material, do_unlink=True)
        raise


def _blend_target_chart(faces, chart, tile, method, fit):
    if tile.get('is_sampled',False):
        # The sample already includes the source tile's fit, rotation and inset.
        mask = patch_tile_uvs(faces,chart,0,0,1,1,1,1,projection=method)
        result = {loop:tuple(mathutils.Vector(affine_uv(tile['sample_uv'],uv))) for loop,uv in mask.items()}
        for face in faces:
            a = result[face.loops[0]]
            area = abs(sum((result[l][0]-a[0])*(result[l.link_loop_next][1]-a[1])-
                           (result[l.link_loop_next][0]-a[0])*(result[l][1]-a[1]) for l in face.loops))
            if area == 0:
                raise ValueError('Copied blend would collapse a face at this atlas precision')
        return result
    return patch_tile_uvs(faces, chart, tile['col'], tile['row'], tile['width_px'], tile['height_px'],
                          tile['atlas_width'], tile['atlas_height'], tile['rotation'],
                          tile['flip_u'], tile['flip_v'], tile['inset_px'], method, fit,
                          keep_orientation=True)


def blend_mesh_version(mesh, bm=None):
    """Recognise generated data after Join drops the mesh's ownership marker.

    Names alone are never proof: require the complete UV set, typed face
    metadata and a tagged generated material actually referencing these maps.
    This is read-only; successful application writes the marker transactionally.
    """
    version = mesh.get(BLEND_MESH_TAG, 0)
    if version >= 2:
        return version

    def has_layer(name, domain, kind):
        if bm is None:
            attr = mesh.attributes.get(name)
            return attr is not None and (attr.domain, attr.data_type) == (domain, kind)
        layers = bm.loops.layers if domain == 'CORNER' else bm.faces.layers
        collection = {'FLOAT2': 'uv', 'FLOAT_VECTOR': 'float_vector', 'INT': 'int', 'FLOAT': 'float'}[kind]
        return getattr(layers, collection).get(name) is not None

    if not has_layer(BLEND_B_UV, 'CORNER', 'FLOAT2'):
        return version
    legacy_mask = not has_layer(BLEND_MASK_ATTRIBUTE, 'CORNER', 'FLOAT_VECTOR')
    if legacy_mask and not has_layer(LEGACY_MASK_UV, 'CORNER', 'FLOAT2'):
        return version
    def uv_nodes_match(material, role_key, required):
        if material_nodes(material) is None:
            return False
        nodes = {node.get(role_key):node for node in material.node_tree.nodes if node.get(role_key)}
        return all(role in nodes and nodes[role].type == 'UVMAP' and nodes[role].uv_map == name
                   for role,name in required.items())
    shared = (has_layer(SHARED_FACTOR_UV, 'CORNER', 'FLOAT2') and has_layer(SHARED_PATCH_ID, 'FACE', 'INT')
              and has_layer(SHARED_PATCH_FLAGS, 'FACE', 'INT')
              and all(has_layer(SHARED_PATCH_FLOATS[key], 'FACE', 'FLOAT') for key in ('position','width')))
    for material in mesh.materials:
        if material and shared and material.get(SHARED_MATERIAL_TAG) and uv_nodes_match(
                material, 'sprotile_shared_role',
                {'uv_0': LEGACY_A_UV if legacy_mask else '', 'uv_1': BLEND_B_UV, 'factor': SHARED_FACTOR_UV}):
            if (has_layer(SHARED_EDGE_UV, 'CORNER', 'FLOAT2')
                    and all(has_layer(SHARED_PATCH_FLOATS[key], 'FACE', 'FLOAT') for key in ('edge_height','edge_frequency'))
                    and uv_nodes_match(material,'sprotile_shared_role',{'edge_uv':SHARED_EDGE_UV})):return 3
            return 2
        if (material and not version and has_layer(BLEND_ORIGINAL_MATERIAL, 'FACE', 'INT')
                and material.get('sprotile_blend_material') and uv_nodes_match(material, 'sprotile_blend_role',
                    {'texture_a_uv':'', 'texture_b_uv':BLEND_B_UV})
                and is_mask_node(blend_nodes(material).get('mask_uv'))):
            version = 1
    return version


def per_face_coordinates(face_points):
    """Each face's own share of the patch layout, 0..1 across its box, for Tiles: Per Face.

    A face's box in the layout becomes the whole tile, as Map Per Face fills a quad. A triangle that is half of a
    square cell gets half the tile, so a triangulated grid still shows one whole tile per cell.
    """
    result = []
    for points in face_points:
        us, vs = [p[0] for p in points], [p[1] for p in points]
        u0, v0 = min(us), min(vs)
        du, dv = max(us) - u0, max(vs) - v0
        if not (du > 1e-9 and dv > 1e-9):      # also false for NaN
            raise ValueError('A face is a line in the blend layout, so it has no tile to fill; '
                             'use Tiles: Stretch or remove the sliver face')
        result.append([((u - u0) / du, (v - v0) / dv) for u, v in points])
    return result


def tile_domain(faces, mask, per_face):
    """Where each corner samples A and B before the tile's own fit: the patch Mask, or each face's share of it."""
    if not per_face:
        return {loop: tuple(corner_uv(loop, mask)) for face in faces for loop in face.loops}
    corners = [list(face.loops) for face in faces]
    coordinates = per_face_coordinates([[tuple(corner_uv(loop, mask)) for loop in loops] for loops in corners])
    return {loop: uv for loops, values in zip(corners, coordinates) for loop, uv in zip(loops, values)}


def patch_tiles_per_face(obj, bm, faces):
    """Whether a placed patch repeats A and B per face (shared: face flag; separate: its material)."""
    face = faces[0]
    material = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
    if material is not None and material.get('sprotile_blend_material'):
        return material.sprotile_blend_controls.tiles == 'FACE'
    flags = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
    return bool(flags is not None and face[flags] & TILE_FACE_FLAG)


def prepare_blend_plans(context, tile_a, tile_b, per_face=False, split_faces=False, method=None, fit=None,
                        patch_groups=None, faces=None):
    """All geometry, atlas and destination checks precede source mutations.

    faces ({mesh pointer: [face index, ...]}) names the faces to blend instead of the selection, which is then
    neither read nor changed (meshes it does not name are left alone; hidden faces are skipped). split_faces makes
    every chosen face its own patch; patch_groups ({mesh pointer: [[face index, ...], ...]}) says exactly which of
    them form each patch instead; method and fit override the scene's Blend Options (the paint tools use them so
    their result does not depend on what the panel was last set to)."""
    explicit = faces
    scene = context.scene
    marked = fit is None and scene.sprotile_rectangle_corner_mode == 'MARKED'
    method = method or scene.sprotile_patch_projection
    fit = fit or scene.sprotile_patch_fit
    view_rotation = None
    if method == 'VIEW':
        region = get_region_3d(context.area)
        if region is None or is_quad_view(context.area):
            raise ValueError("View blending needs a single 3D Viewport; choose Surface or Unfold")
        view_rotation = region.view_rotation.copy()
    raw, existing, seen, versions = [], [], set(), {}
    upgrade_file_blends()
    for obj in edit_mesh_objects(context):
        mesh = obj.data
        if mesh.as_pointer() in seen:
            continue
        seen.add(mesh.as_pointer())
        if mesh.library is not None:
            raise ValueError("Make linked library mesh data editable before blending")
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        if explicit is not None:
            total = len(bm.faces)
            wanted = dict.fromkeys(explicit.get(mesh.as_pointer(), ()))
            if any(not 0 <= index < total for index in wanted):
                raise ValueError('Internal: a face to blend is not in the mesh')
            faces = [face for face in (bm.faces[index] for index in wanted) if not face.hide]
        else:
            faces = [face for face in bm.faces if face.select and not face.hide]
        if not faces:
            continue
        blend_version = blend_mesh_version(mesh, bm)
        versions[mesh.as_pointer()] = blend_version
        if bm.loops.layers.uv.get(SHARED_EDGE_UV) is not None and blend_version<3:
            raise ValueError('Reserved grass-edge UV map already exists; rename that user map first')
        if not blend_version and (bm.loops.layers.uv.get(BLEND_B_UV) is not None
                                  or mesh.attributes.get(BLEND_MASK_ATTRIBUTE) is not None
                                  or mesh.attributes.get(BLEND_BASE_ATTRIBUTE) is not None):
            raise ValueError("Reserved SprotileBlend names already exist; rename those user maps or attributes first")
        check_reserved_attributes(mesh)
        if (not blend_version
                and mesh.attributes.get(BLEND_ORIGINAL_MATERIAL) is not None):
            raise ValueError("Reserved blend material-history attribute already exists; rename that user attribute first")
        # Separate mode also clears shared membership/factors when converting.
        # Reject user-owned names in either mode before any of those writes.
        if blend_version < 2:
            shared_names = (SHARED_PATCH_ID, SHARED_PATCH_FLAGS) + tuple(SHARED_PATCH_FLOATS.values())
            if (bm.loops.layers.uv.get(SHARED_FACTOR_UV) is not None
                    or any(mesh.attributes.get(name) is not None for name in shared_names)):
                raise ValueError("Reserved shared-blend data already exists; rename those user maps or attributes first")
        if (mesh.attributes.get(BLEND_ORIGINAL_MATERIAL) is not None
                and bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL) is None):
            raise ValueError("The blend material-history attribute has an incompatible type")
        # Named faces (the paint tools) never add a material slot, so only their own slots need to be sound;
        # an Apply from the selection checks the whole mesh, as it always has.
        slot_limit = max(1, len(mesh.materials))
        if any(face.material_index >= slot_limit for face in (faces if explicit is not None else bm.faces)):
            raise ValueError("Mesh has an invalid material slot index; repair its slots first")
        use_tint = bool(scene.sprotile_blend_use_tint and (
            mesh.color_attributes.get('Tint') is not None
            or bm.loops.layers.color.get('Tint') or bm.loops.layers.float_color.get('Tint')))
        groups = None
        if patch_groups is not None and mesh.as_pointer() in patch_groups:
            chosen = {face.index for face in faces}
            groups = [[bm.faces[index] for index in group] for group in patch_groups[mesh.as_pointer()] if group]
            if any(face.index not in chosen for group in groups for face in group) or \
                    sum(len(group) for group in groups) != len(chosen):
                raise ValueError('Internal: the patch groups must cover exactly the faces to blend')
        elif split_faces:
            groups = [[face] for face in faces]
        if method == 'EXISTING':
            source = bm.loops.layers.uv.active
            if explicit is not None:
                # The paint tools lay B on each face's own tile, which is in the render map whatever map is active.
                source = bm.loops.layers.uv.get(render_uv_name(mesh) or '') or source
            if source is not None and source.name in INTERNAL_BLEND_UVS:
                raise ValueError('Existing UVs reads the active UV map; make the original atlas map active, '
                                 'not a SprotileBlend map')
            reader = existing_uv_reader(mesh, bm, source)
            if groups is not None:
                # Each group keeps its own tiles' layout, so B lies on it exactly as A does (turns and flips too).
                for group in groups:
                    raw.append((obj, bm, group, patch_loop_uvs(group, obj.matrix_world, method, uv_layer=source,
                                                               reader=reader), use_tint))
                continue
            chart = patch_loop_uvs(faces, obj.matrix_world, method, uv_layer=source, reader=reader)
            existing.append((obj, bm, faces, chart, use_tint))
        else:
            for patch in groups if groups is not None else connected_face_patches(faces):
                chart = patch_loop_uvs(patch, obj.matrix_world, method, view_rotation)
                raw.append((obj, bm, patch, chart, use_tint))
    if existing:
        u0 = min(item[3].source_bounds[0] for item in existing)
        v0 = min(item[3].source_bounds[1] for item in existing)
        width = max(item[3].source_bounds[0] - u0 + item[3].source_bounds[2] for item in existing)
        height = max(item[3].source_bounds[1] - v0 + item[3].source_bounds[3] for item in existing)
        aspect = width / height * tile_a['atlas_width'] / tile_a['atlas_height']
        for obj, bm, faces, chart, use_tint in existing:
            x, y, w, h = chart.source_bounds
            chart = PatchChart({loop: ((x - u0 + uv[0] * w) / width,
                                       (y - v0 + uv[1] * h) / height)
                                for loop, uv in chart.items()}, aspect_ratio=aspect)
            raw.append((obj, bm, faces, chart, use_tint))
    plans = {}
    for obj, bm, faces, chart, use_tint in raw:
        if fit == 'RECTANGLE':
            corners=read_rectangle_corners(obj.data,bm,faces) if marked else None
            chart = rectangle_patch_chart(faces, chart,corners)
        mask = patch_tile_uvs(faces, chart, 0, 0, 1, 1, 1, 1, projection=method)
        if per_face:
            # The tiles fill each face's box in the Mask (float32 values, exactly as stored), so reading the patch
            # back later finds the same layout. Fit shapes the patch layout; every face then fills its tile.
            corners = [list(face.loops) for face in faces]
            local = per_face_coordinates([[mask[loop] for loop in loops] for loops in corners])
            local = PatchChart({loop: uv for loops, values in zip(corners, local) for loop, uv in zip(loops, values)})
            a = _blend_target_chart(faces, local, tile_a, method, 'FILL')
            b = _blend_target_chart(faces, local, tile_b, method, 'FILL')
        else:
            a = _blend_target_chart(faces, chart, tile_a, method, fit)
            b = _blend_target_chart(faces, chart, tile_b, method, fit)
        key = obj.data.as_pointer()
        plan = plans.setdefault(key, dict(obj=obj, bm=bm, faces=[], patches=[], use_tint=use_tint,
                                         blend_version=versions[key],
                                         recovered=not obj.data.get(BLEND_MESH_TAG)))
        plan['patches'].append([face.index for face in faces])
        for face in faces:
            plan['faces'].append((face.index, [[values[loop] for loop in face.loops] for values in (a, b, mask)]))
    return list(plans.values())


def _blend_snapshot(plan):
    mesh, bm = plan['obj'].data, plan['bm']
    bm.faces.ensure_lookup_table()
    layers = edit_blend_layers(mesh, bm)._asdict()
    previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
    patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
    metadata = {name: bm.faces.layers.float.get(name) for name in SHARED_PATCH_FLOATS.values()}
    metadata[SHARED_PATCH_FLAGS] = bm.faces.layers.int.get(SHARED_PATCH_FLAGS)
    metadata[PATH_PAINT_ATTRIBUTE] = bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE)
    return dict(materials=tuple(mesh.materials), active_material=plan['obj'].active_material_index,
                active_uv=bm.loops.layers.uv.active.name if bm.loops.layers.uv.active else None,
                render_uv=next((layer.name for layer in mesh.uv_layers if layer.active_render), None),
                tag=mesh.get(BLEND_MESH_TAG), existing_layers={name for name, layer in layers.items() if layer},
                history_existed=previous is not None,
                patch_existed=patch is not None,
                patch_count=len(mesh.sprotile_blend_patches),
                patch_ids={index: bm.faces[index][patch] if patch else 0 for index, _ in plan['faces']},
                metadata_layers={name for name, layer in metadata.items() if layer is not None},
                metadata={index: {name: bm.faces[index][layer] for name, layer in metadata.items() if layer is not None}
                          for index, _ in plan['faces']},
                faces=[(index, face.material_index, face[previous] if previous else 0,
                        {name: [tuple(corner_uv(loop, layer)) for loop in face.loops]
                         for name, layer in layers.items() if layer})
                       for index, _ in plan['faces'] for face in (bm.faces[index],)])


def _blend_restore_uv_choice(mesh, snapshot):
    if snapshot['active_uv'] is not None:
        for index, layer in enumerate(mesh.uv_layers):
            if layer.name == snapshot['active_uv']:
                mesh.uv_layers.active_index = index
                break
    if snapshot['render_uv'] is not None:
        layer = mesh.uv_layers.get(snapshot['render_uv'])
        if layer is not None:
            layer.active_render = True


def _blend_restore_plan(plan, snapshot):
    mesh, bm = plan['obj'].data, plan['bm']
    previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
    bm.faces.ensure_lookup_table()
    layers = edit_blend_layers(mesh, bm)._asdict()
    for index, material_index, history, values in snapshot['faces']:
        face = bm.faces[index]
        face.material_index = material_index
        if previous is not None:
            face[previous] = history
        for name, coordinates in values.items():
            layer = layers[name]
            for loop, uv in zip(face.loops, coordinates):
                set_corner_uv(loop, layer, uv)
    remove_created_layers(bm, plan.get('created', ()))
    if previous is not None and not snapshot['history_existed']:
        bm.faces.layers.int.remove(previous)
    patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
    if patch is not None:
        bm.faces.ensure_lookup_table()
        for index, value in snapshot['patch_ids'].items():
            bm.faces[index][patch] = value
        if not snapshot['patch_existed']:
            bm.faces.layers.int.remove(patch)
    while len(mesh.sprotile_blend_patches) > snapshot['patch_count']:
        mesh.sprotile_blend_patches.remove(len(mesh.sprotile_blend_patches) - 1)
    for index, (parts, name) in snapshot.get('updated_records', {}).items():
        record = mesh.sprotile_blend_patches[index]
        record.parts, record.name = parts, name
    int_metadata = (SHARED_PATCH_FLAGS, PATH_PAINT_ATTRIBUTE)
    for name in tuple(SHARED_PATCH_FLOATS.values()) + int_metadata:
        collection = bm.faces.layers.int if name in int_metadata else bm.faces.layers.float
        layer = collection.get(name)
        if layer is None:
            continue
        if name not in snapshot['metadata_layers']:
            collection.remove(layer)
        else:
            bm.faces.ensure_lookup_table()
            for index, values in snapshot['metadata'].items():
                bm.faces[index][layer] = values[name]
    while len(mesh.materials) > len(snapshot['materials']):
        mesh.materials.pop(index=len(mesh.materials) - 1)
    if snapshot['tag'] is None:
        if BLEND_MESH_TAG in mesh:
            del mesh[BLEND_MESH_TAG]
    else:
        mesh[BLEND_MESH_TAG] = snapshot['tag']
    plan['obj'].active_material_index = snapshot['active_material']
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    _blend_restore_uv_choice(mesh, snapshot)


def apply_separate_texture_blend(context):
    settings=validated_blend_settings(active_blend_settings(context))
    if settings['direction'] == 'PAINTED':
        raise ValueError('Direction Painted Only needs Shared Material: the painted alpha lives in the shared atlas material')
    tile_a = validated_blend_tile(context.scene.sprotile_blend_a)
    tile_b = validated_blend_tile(context.scene.sprotile_blend_b)
    plans = prepare_blend_plans(context, tile_a, tile_b, per_face=settings['tiles'] == 'FACE')
    if not plans:
        return False
    for plan in plans:
        uvs = plan['bm'].loops.layers.uv
        missing = sum(uvs.get(name) is None for name in blend_uv_maps_needed(False, False)) + (not len(uvs))
        if len(uvs) + missing > 8:
            raise ValueError("Separate Material needs a blend UV map for texture B; Blender's eight-map limit would be exceeded")
    snapshots = [_blend_snapshot(plan) for plan in plans]
    materials, touched = {}, []
    try:
        for use_tint in {plan['use_tint'] for plan in plans}:
            materials[use_tint] = build_blend_material(tile_a, tile_b, settings, use_tint)
        for plan, snapshot in zip(plans, snapshots):
            touched.append((plan, snapshot))
            obj, bm = plan['obj'], plan['bm']
            mesh = obj.data
            if not mesh.materials:
                mesh.materials.append(None)  # Keep unselected, previously untextured faces untextured.
            material = materials[plan['use_tint']]
            mesh.materials.append(material)
            slot = len(mesh.materials) - 1
            plan['created'] = ensure_edit_blend_layers(mesh, bm, shared=False, edge=False)
            previous = bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL) or bm.faces.layers.int.new(BLEND_ORIGINAL_MATERIAL)
            stored = edit_blend_layers(mesh, bm)
            layers = (stored.a, stored.b, stored.mask)
            patch = bm.faces.layers.int.get(SHARED_PATCH_ID)
            path_mark = bm.faces.layers.int.get(PATH_PAINT_ATTRIBUTE)
            factor = bm.loops.layers.uv.get(SHARED_FACTOR_UV)
            # First face-domain custom data can relocate BMFace wrappers.
            # Plans retain indices and plain UV values, never stale wrappers.
            bm.faces.ensure_lookup_table()
            for index, coordinates in plan['faces']:
                face = bm.faces[index]
                old = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
                if not face_is_blended(face, patch, previous):
                    remember_base_tile(face, stored)
                if plan['recovered'] or not face[previous] or old is None or not old.get('sprotile_blend_material'):
                    face[previous] = encode_material_history(old, face.material_index, mesh)
                for layer, values in zip(layers, coordinates):
                    for loop, uv in zip(face.loops, values):
                        set_corner_uv(loop, layer, uv)
                if patch is not None:
                    face[patch] = 0
                if path_mark is not None:
                    face[path_mark] = 0
                if factor is not None:
                    for loop in face.loops:
                        loop[factor].uv = (0.0, 0.0)
                face.material_index = slot
            mesh[BLEND_MESH_TAG] = max(1, plan['blend_version'])
            obj.active_material_index = slot
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
            _blend_restore_uv_choice(mesh, snapshot)
    except Exception:
        for plan, snapshot in reversed(touched):
            _blend_restore_plan(plan, snapshot)
        for material in materials.values():
            if material.users == 0:
                bpy.data.materials.remove(material)
        raise
    tag_redraw_view3d()
    return True


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import MATERIALS_ALWAYS_USE_NODES, material_nodes
from .draw import tag_redraw_view3d
from .tiles import (
    edit_mesh_objects, get_tile_dims, get_tile_transform, get_uv_inset, image_size, is_editable_mesh,
    validate_tile_dimensions,
)
from .mapping import PatchChart, connected_face_patches, patch_loop_uvs, patch_tile_uvs, rectangle_patch_chart
from .picker import get_region_3d, is_quad_view
from .blend_settings import (
    BLEND_B_UV, BLEND_BASE_ATTRIBUTE, BLEND_CONTROL_FIELDS, BLEND_MASK_ATTRIBUTE, BLEND_MESH_TAG,
    BLEND_ORIGINAL_MATERIAL, BLEND_TILE_FIELDS, INTERNAL_BLEND_UVS, LEGACY_A_UV, LEGACY_MASK_UV,
    SHARED_EDGE_UV, SHARED_FACTOR_UV,
    PATH_PAINT_ATTRIBUTE, SHARED_MATERIAL_TAG, SHARED_PATCH_FLAGS, SHARED_PATCH_FLOATS, SHARED_PATCH_ID, TILE_FACE_FLAG,
    blend_nodes, read_rectangle_corners, update_blend_graph, validated_blend_settings,
)
from .blend_storage import (
    blend_uv_maps_needed, check_reserved_attributes, corner_uv, edit_blend_layers, ensure_edit_blend_layers,
    existing_uv_reader, face_is_blended, is_mask_node, mask_socket, remember_base_tile, remove_created_layers,
    render_uv_name, set_corner_uv, upgrade_file_blends, encode_material_history,
)
from .blend_recipes import active_blend_settings, affine_uv, sample_uv_bounds
from .blend_ops import invalidate_clipboard_slot
