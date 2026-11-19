"""Where a blend lives on a mesh (Sprotile 5.13).

Only what the Unreal master reads is a UV map:
- tile A goes into the render UV map, the map the atlas material already samples (Unreal's UV0);
- tile B, Factor and, for grass or snow, Edge stay UV maps.
What only Sprotile reads is a corner attribute, which FBX export leaves out:
- `sprotile_blend_mask`, the patch layout (x, y) that every tool reads a placed blend back from;
- `sprotile_blend_base`, each blended face's previous tile (x, y), which Remove Blend puts back.
  Overlay cells store their source grid in base Z and mask Z, as low and high 16-bit halves.

Sprotile 5.12 and earlier kept A and the Mask in UV maps of their own (SprotileBlend_A, SprotileBlend_Mask).
upgrade_file_blends() moves such meshes and their materials to this layout when a file loads and before every
blend operator runs. The upgrade is one way: Sprotile 5.12 cannot edit the blends of an upgraded file.
"""

import re

import bpy
import bmesh
from bmesh.types import BMLoopUV
from collections import namedtuple
from mathutils import Vector

BlendLayers = namedtuple('BlendLayers', ('a', 'b', 'mask', 'base', 'factor', 'edge'))


# ---------------------------------------------------------------------------
# Reading and writing a corner, whichever kind of layer holds it
# ---------------------------------------------------------------------------

def corner_uv(corner, layer):
    """The (u, v) a corner holds in a UV map or in a Sprotile vector attribute, in Edit or Object Mode."""
    item = corner[layer]
    if type(item) is BMLoopUV:
        return item.uv
    if type(item) is Vector:
        return item.xy
    if hasattr(item, 'uv'):
        return item.uv
    return item.vector.xy


def set_corner_uv(corner, layer, value):
    item = corner[layer]
    if type(item) is BMLoopUV:
        item.uv = value
    elif type(item) is Vector:
        corner[layer] = value if len(value) == 3 else (*value, 0.0)
    elif hasattr(item, 'uv'):
        item.uv = value
    else:
        item.vector = value if len(value) == 3 else (*value, 0.0)


# ---------------------------------------------------------------------------
# Finding the layers
# ---------------------------------------------------------------------------

def render_uv_name(mesh):
    """The render UV map's name (the first map when none is marked), or None for a mesh without UV maps.

    In Edit Mode mesh.uv_layers reads the edit mesh, so this also sees maps added during the session."""
    layers = mesh.uv_layers
    if not len(layers):
        return None
    return next((layer.name for layer in layers if layer.active_render), layers[0].name)


def _vector_attribute(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != 'CORNER' or attribute.data_type != 'FLOAT_VECTOR':
        return None
    return attribute


def edit_blend_layers(mesh, bm):
    """The blend layers of an Edit Mode mesh; missing ones are None. Never creates data."""
    uvs, vectors = bm.loops.layers.uv, bm.loops.layers.float_vector
    name = render_uv_name(mesh)
    return BlendLayers(uvs.get(name) if name else None, uvs.get(BLEND_B_UV),
                       vectors.get(BLEND_MASK_ATTRIBUTE), vectors.get(BLEND_BASE_ATTRIBUTE),
                       uvs.get(SHARED_FACTOR_UV), uvs.get(SHARED_EDGE_UV))


def object_blend_layers(mesh):
    """The same for an Object Mode mesh: UV maps and attributes, read with corner_uv(_MeshCorner, layer)."""
    uvs = mesh.uv_layers
    name = render_uv_name(mesh)
    return BlendLayers(uvs.get(name) if name else None, uvs.get(BLEND_B_UV),
                       _vector_attribute(mesh, BLEND_MASK_ATTRIBUTE), _vector_attribute(mesh, BLEND_BASE_ATTRIBUTE),
                       uvs.get(SHARED_FACTOR_UV), uvs.get(SHARED_EDGE_UV))


def check_reserved_attributes(mesh):
    """Refuse a user attribute that has one of Sprotile's corner-attribute names but another type."""
    for name in (BLEND_MASK_ATTRIBUTE, BLEND_BASE_ATTRIBUTE):
        attribute = mesh.attributes.get(name)
        if attribute is not None and (attribute.domain != 'CORNER' or attribute.data_type != 'FLOAT_VECTOR'):
            raise ValueError('Reserved blend attribute "' + name + '" already exists with another type; '
                             'rename that user attribute first')


def blend_uv_maps_needed(shared, edge):
    """The UV maps a blend adds, besides the render map that holds A."""
    return (BLEND_B_UV,) + ((SHARED_FACTOR_UV,) if shared else ()) + ((SHARED_EDGE_UV,) if shared and edge else ())


def ensure_edit_blend_layers(mesh, bm, shared, edge):
    """Create what an Apply writes and return (kind, name) for each layer created, for rollback."""
    created = []
    uvs, vectors = bm.loops.layers.uv, bm.loops.layers.float_vector
    if not len(uvs):
        uvs.new('UVMap')   # An untextured mesh (Separate Material) gets a render map to hold A.
        created.append(('uv', 'UVMap'))
    for name in blend_uv_maps_needed(shared, edge):
        if uvs.get(name) is None:
            uvs.new(name)
            created.append(('uv', name))
    for name in (BLEND_MASK_ATTRIBUTE, BLEND_BASE_ATTRIBUTE):
        if vectors.get(name) is None:
            vectors.new(name)
            created.append(('vector', name))
    return created


def remove_created_layers(bm, created):
    for kind, name in reversed(created):
        collection = bm.loops.layers.uv if kind == 'uv' else bm.loops.layers.float_vector
        layer = collection.get(name)
        if layer is not None:
            collection.remove(layer)


def face_is_blended(face, patch, previous):
    """A shared patch face or a Separate Material face: its render map holds A, its base attribute its tile."""
    return bool((patch is not None and face[patch] > 0) or (previous is not None and face[previous] > 0))


# A Separate Material face remembers the material it had before its blend (BLEND_ORIGINAL_MATERIAL). Up to 5.15.0
# that was only the slot number + 1, which points at the wrong material once slots are reordered. Since 5.15.1 the
# value is (material key << 8) | (slot + 1): the key is a number stored on the material (HISTORY_KEY_PROPERTY), so
# the material is found again in whatever slot it now sits, even after a Join. Values below 256 are the old form.
#
# Material.copy() and Shift+D copy custom properties, so a copy carries its original's key (5.15.1 then restored the
# copy). Since 5.15.2 each mesh that records a history also keeps HISTORY_OWNERS: {key: the material itself}, an ID
# pointer a material copy can't inherit. That record decides which of several materials with one key is meant, and a
# copy that gets a history of its own is given a fresh key first.
HISTORY_KEY_PROPERTY = 'sprotile_history_key'
HISTORY_OWNERS = 'sprotile_history_owners'
HISTORY_SLOT_BITS = 8
HISTORY_SLOT_MASK = (1 << HISTORY_SLOT_BITS) - 1
HISTORY_KEY_LIMIT = 1 << 22


def material_history_key(material, create=True):
    """The material's history key (1 .. HISTORY_KEY_LIMIT - 1), made on first use; 0 if it can't have one."""
    if material is None:
        return 0
    key = material.get(HISTORY_KEY_PROPERTY)
    if isinstance(key, int) and 0 < key < HISTORY_KEY_LIMIT:
        return key
    if not create or material.library is not None:
        return 0
    return _new_history_key(material)


def _new_history_key(material):
    import random
    used = {m.get(HISTORY_KEY_PROPERTY) for m in bpy.data.materials}
    key = random.randrange(1, HISTORY_KEY_LIMIT)
    while key in used:
        key = random.randrange(1, HISTORY_KEY_LIMIT)
    try:
        material[HISTORY_KEY_PROPERTY] = key
    except (TypeError, AttributeError, RuntimeError):
        return 0
    return key


def _recorded_owner(mesh, key):
    owners = mesh.get(HISTORY_OWNERS) if mesh is not None else None
    if owners is None or not hasattr(owners, 'get'):
        return None
    owner = owners.get(str(key))
    return owner if isinstance(owner, bpy.types.Material) else None


def history_candidates(key):
    """Every material that carries this history key (more than one only when a material was copied before 5.15.2)."""
    return [m for m in bpy.data.materials if material_history_key(m, False) == key]


def record_history_owner(mesh, key, material):
    try:
        owners = mesh.get(HISTORY_OWNERS)
        if owners is None or not hasattr(owners, 'get'):
            mesh[HISTORY_OWNERS] = {}
            owners = mesh[HISTORY_OWNERS]
        if owners.get(str(key)) != material:
            owners[str(key)] = material
        return True
    except (TypeError, AttributeError, RuntimeError):
        return False   # a linked mesh: the key alone still works while it is unique


def history_owner(key, mesh=None, cache=None, chosen=None):
    """The material a history key stands for: the one a mesh recorded (this mesh first, then any mesh, which covers
    joined faces), else the only material with that key. None when no material has the key, and None when several
    have it and nothing recorded says which (a file saved by 5.15.1 after its material was copied): a name is not
    proof, so nothing is guessed. `chosen` is the user's answer to that: when it is one of the candidates it is
    used, and recorded on the mesh so the question is settled for good."""
    if cache is not None and key in cache:
        return cache[key]
    owner = _recorded_owner(mesh, key)
    if owner is None or material_history_key(owner, False) != key:
        owner = None
        for other in bpy.data.meshes:
            candidate = _recorded_owner(other, key)
            if candidate is not None and material_history_key(candidate, False) == key:
                owner = candidate
                break
    if owner is None:
        candidates = history_candidates(key)
        if len(candidates) == 1:
            owner = candidates[0]
        elif chosen is not None and chosen in candidates:
            owner = chosen
            if mesh is not None:
                record_history_owner(mesh, key, owner)
    if cache is not None and owner is not None:
        cache[key] = owner
    return owner


def history_conflict(obj, value):
    """For a face whose original material cannot be told: the sentence that says why and what to do, else None."""
    value = int(value)
    if value <= HISTORY_SLOT_MASK:
        return None
    key = value >> HISTORY_SLOT_BITS
    if history_owner(key, obj.data, chosen=obj.active_material) is not None:
        return None
    names = sorted(m.name for m in history_candidates(key))
    if len(names) < 2:
        return None
    return ("Materials " + ", ".join(f"'{name}'" for name in names) + " both claim to be the original of these faces "
            "(one is a copy made before Sprotile 5.15.2). Click the right one in the object's material list "
            "(add it to the list if it is not there), then do this again")


def encode_material_history(material, slot, mesh=None):
    """The BLEND_ORIGINAL_MATERIAL value for a face whose material before the blend sat in `slot`.

    With `mesh`, the material is recorded as the owner of its key there; a copy that shares its original's key gets
    a fresh key of its own first, so the two can never be confused."""
    key = material_history_key(material)
    if not key:
        return slot + 1
    if mesh is not None:
        owner = history_owner(key, mesh)
        if (owner is not None and owner != material) or (owner is None and len(history_candidates(key)) > 1):
            # Another material owns this key, or nobody can say who does: this blend gets a key of its own.
            key = _new_history_key(material) or key
        record_history_owner(mesh, key, material)
    return (key << HISTORY_SLOT_BITS) | (slot + 1 if slot + 1 <= HISTORY_SLOT_MASK else 0)


def history_slot(obj, value, cache=None):
    """The slot that now holds the material a face had before its blend, or -1. Old values (< 256) are slots."""
    value = int(value)
    if value <= 0:
        return -1
    slots = obj.material_slots
    if value <= HISTORY_SLOT_MASK:
        return value - 1 if value - 1 < len(slots) else -1
    key, hint = value >> HISTORY_SLOT_BITS, (value & HISTORY_SLOT_MASK) - 1
    owner = history_owner(key, obj.data, cache, chosen=obj.active_material)
    if owner is None:
        return -1
    if 0 <= hint < len(slots) and slots[hint].material == owner:
        return hint
    return next((index for index, slot in enumerate(slots) if slot.material == owner), -1)


def history_material(value, mesh=None, cache=None):
    """The material a history value names, wherever it is (None for old values or a deleted material)."""
    value = int(value)
    if value <= HISTORY_SLOT_MASK:
        return None
    return history_owner(value >> HISTORY_SLOT_BITS, mesh, cache)


def remember_base_tile(face, layers):
    """Keep the tile a face shows before its first blend, so Remove Blend can put it back."""
    for loop in face.loops:
        loop[layers.base] = (*loop[layers.a].uv, 0.0)


def restore_base_tile(face, layers):
    if layers.base is None or layers.a is None:
        return
    for loop in face.loops:
        loop[layers.a].uv = loop[layers.base].xy


def mask_socket(node):
    """The patch-coordinate output of a Separate Material's mask node (an Attribute node since 5.13)."""
    return node.outputs['Vector'] if node.bl_idname == 'ShaderNodeAttribute' else node.outputs[0]


def is_mask_node(node):
    return (node is not None and node.bl_idname == 'ShaderNodeAttribute' and node.attribute_type == 'GEOMETRY'
            and node.attribute_name == BLEND_MASK_ATTRIBUTE)


def is_render_uv_node(node):
    """A UV Map node that samples the render map, which holds tile A."""
    return node is not None and node.type == 'UVMAP' and node.uv_map == ''


# ---------------------------------------------------------------------------
# Upgrading 5.12 files
# ---------------------------------------------------------------------------

def mesh_is_legacy(mesh):
    """A mesh with 5.12 blends: SprotileBlend_A/_Mask maps plus Sprotile's blend records (a user's own map that
    happens to have one of those names is left alone)."""
    names = mesh.uv_layers.keys()
    if LEGACY_A_UV not in names and LEGACY_MASK_UV not in names:
        return False
    # A current mesh already stores both the patch mask and the original tile.
    # Running the migration again would replace them with a user's namesake UV map.
    if all(_vector_attribute(mesh, name) is not None for name in (BLEND_MASK_ATTRIBUTE, BLEND_BASE_ATTRIBUTE)):
        return False
    return bool(mesh.get(BLEND_MESH_TAG) or mesh.attributes.get(SHARED_PATCH_ID) is not None
                or mesh.attributes.get(BLEND_ORIGINAL_MATERIAL) is not None)


def _upgrade_edit_mesh(mesh):
    bm = bmesh.from_edit_mesh(mesh)
    uvs, vectors = bm.loops.layers.uv, bm.loops.layers.float_vector
    render = render_uv_name(mesh)
    if render in (LEGACY_A_UV, LEGACY_MASK_UV, BLEND_B_UV, SHARED_FACTOR_UV, SHARED_EDGE_UV):
        render = next((layer.name for layer in mesh.uv_layers if layer.name not in
                       (LEGACY_A_UV, LEGACY_MASK_UV, BLEND_B_UV, SHARED_FACTOR_UV, SHARED_EDGE_UV)), None)
        if render is None:
            uvs.new('UVMap')
            render = 'UVMap'
        mesh.uv_layers[render].active_render = True
    for name in (BLEND_MASK_ATTRIBUTE, BLEND_BASE_ATTRIBUTE):
        if vectors.get(name) is None:
            vectors.new(name)
    # Look every layer up again after adding some (see the 4.x lesson in SPROTILE_STATE).
    a, mask, base = uvs.get(LEGACY_A_UV), uvs.get(LEGACY_MASK_UV), uvs.get(render)
    mask_out, base_out = vectors.get(BLEND_MASK_ATTRIBUTE), vectors.get(BLEND_BASE_ATTRIBUTE)
    patch, previous = bm.faces.layers.int.get(SHARED_PATCH_ID), bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
    for face in bm.faces:
        blended = face_is_blended(face, patch, previous)
        for loop in face.loops:
            if mask is not None:
                loop[mask_out] = (*loop[mask].uv, 0.0)
            loop[base_out] = (*loop[base].uv, 0.0)
            if blended and a is not None:
                loop[base].uv = loop[a].uv
    for name in (LEGACY_A_UV, LEGACY_MASK_UV):
        layer = uvs.get(name)
        if layer is not None:
            uvs.remove(layer)
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return render


def _upgrade_object_mesh(mesh):
    names = mesh.uv_layers.keys()
    reserved = (LEGACY_A_UV, LEGACY_MASK_UV, BLEND_B_UV, SHARED_FACTOR_UV, SHARED_EDGE_UV)
    render = render_uv_name(mesh)
    if render in reserved:
        render = next((name for name in names if name not in reserved), None)
        if render is None:
            mesh.uv_layers.new(name='UVMap', do_init=False)
            render = 'UVMap'
        mesh.uv_layers[render].active_render = True
    count = len(mesh.loops)

    def read(name):
        values = [0.0] * (2 * count)
        if name in mesh.uv_layers:
            mesh.uv_layers[name].data.foreach_get('uv', values)
        return values
    a, mask, base = read(LEGACY_A_UV), read(LEGACY_MASK_UV), read(render)
    blended = [False] * len(mesh.polygons)
    for name in (SHARED_PATCH_ID, BLEND_ORIGINAL_MATERIAL):
        attribute = mesh.attributes.get(name)
        if attribute is not None and attribute.domain == 'FACE' and attribute.data_type == 'INT':
            values = [0] * len(mesh.polygons)
            attribute.data.foreach_get('value', values)
            blended = [b or v > 0 for b, v in zip(blended, values)]
    shown = list(base)
    if LEGACY_A_UV in names:
        for polygon in mesh.polygons:
            if blended[polygon.index]:
                for index in polygon.loop_indices:
                    shown[2*index:2*index+2] = a[2*index:2*index+2]
    for name in (BLEND_MASK_ATTRIBUTE, BLEND_BASE_ATTRIBUTE):
        if mesh.attributes.get(name) is None:
            mesh.attributes.new(name, 'FLOAT_VECTOR', 'CORNER')
    # Blender 4.x reallocates attribute storage when one is added: look everything up again.
    expand = lambda flat: [value for i in range(count) for value in (flat[2*i], flat[2*i+1], 0.0)]
    mesh.attributes[BLEND_MASK_ATTRIBUTE].data.foreach_set('vector', expand(mask))
    mesh.attributes[BLEND_BASE_ATTRIBUTE].data.foreach_set('vector', expand(base))
    mesh.uv_layers[render].data.foreach_set('uv', shown)
    active = mesh.uv_layers.active.name if mesh.uv_layers.active else render
    for name in (LEGACY_A_UV, LEGACY_MASK_UV):
        if name in mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[name])
    mesh.uv_layers[render].active_render = True
    mesh.uv_layers.active = mesh.uv_layers[active if active in mesh.uv_layers else render]
    mesh.update()
    return render


def upgrade_blend_material(material):
    """Point a 5.12 blend material at the render map (A) and the mask attribute. Returns True if it changed."""
    if material is None or material.library is not None or material.node_tree is None:
        return False
    changed = False
    if material.get(SHARED_MATERIAL_TAG):
        for node in material.node_tree.nodes:
            if node.get('sprotile_shared_role') == 'uv_0' and node.type == 'UVMAP' and node.uv_map == LEGACY_A_UV:
                node.uv_map = ''
                changed = True
    if material.get('sprotile_blend_material'):
        nodes, links = material.node_tree.nodes, material.node_tree.links
        for node in list(nodes):
            role = node.get('sprotile_blend_role')
            if role == 'texture_a_uv' and node.type == 'UVMAP' and node.uv_map == LEGACY_A_UV:
                node.uv_map = ''
                changed = True
            elif role == 'mask_uv' and node.type == 'UVMAP' and node.uv_map == LEGACY_MASK_UV:
                targets = [link.to_socket for link in node.outputs[0].links]
                replacement = nodes.new('ShaderNodeAttribute')
                replacement.attribute_type = 'GEOMETRY'
                replacement.attribute_name = BLEND_MASK_ATTRIBUTE
                replacement['sprotile_blend_role'] = 'mask_uv'
                replacement.label = node.label
                replacement.location = node.location
                nodes.remove(node)
                for socket in targets:
                    links.new(replacement.outputs['Vector'], socket)
                changed = True
    return changed


def upgrade_file_blends():
    """Move every 5.12 blend in this file to the 5.13 layout; returns the number of meshes upgraded."""
    upgraded = 0
    for mesh in bpy.data.meshes:
        if mesh.library is not None or not mesh_is_legacy(mesh):
            continue
        if mesh.is_editmode:
            _upgrade_edit_mesh(mesh)
        else:
            _upgrade_object_mesh(mesh)
        upgraded += 1
    for material in bpy.data.materials:
        upgrade_blend_material(material)
    return upgraded


def check_atlas_reads_render_map(mesh, original):
    """Tile A is written into the render UV map, so the atlas node a shared blend extends must sample that map."""
    render = render_uv_name(mesh)
    vector = original.inputs['Vector']
    if not vector.is_linked:
        return
    link = vector.links[0]
    source = link.from_node
    if source.type == 'UVMAP' and source.uv_map in ('', render):
        return
    if source.type == 'TEX_COORD' and link.from_socket.name == 'UV':
        return
    name = source.uv_map if source.type == 'UVMAP' else 'a custom coordinate input'
    raise ValueError('Blends write texture A into the render UV map ("' + str(render) + '"), but this atlas reads '
                     + (('UV map "' + name + '"') if source.type == 'UVMAP' else name)
                     + '; make that map the render map, or use Separate Material')


def existing_uv_reader(mesh, bm, uv_layer):
    """How Existing UVs reads a corner of the active map. A blended face's render map holds tile A, so for
    those faces read the layout the face had before its blend (the base attribute), as Sprotile 5.12 did."""
    if uv_layer is None:
        return None
    base = bm.loops.layers.float_vector.get(BLEND_BASE_ATTRIBUTE)
    if base is None or uv_layer.name != render_uv_name(mesh):
        return lambda loop: loop[uv_layer].uv
    patch, previous = bm.faces.layers.int.get(SHARED_PATCH_ID), bm.faces.layers.int.get(BLEND_ORIGINAL_MATERIAL)
    def read(loop):
        return loop[base].xy if face_is_blended(loop.face, patch, previous) else loop[uv_layer].uv
    return read


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .blend_settings import (
    BLEND_B_UV, BLEND_BASE_ATTRIBUTE, BLEND_MASK_ATTRIBUTE, BLEND_MESH_TAG, BLEND_ORIGINAL_MATERIAL, LEGACY_A_UV, LEGACY_MASK_UV,
    SHARED_EDGE_UV, SHARED_FACTOR_UV, SHARED_MATERIAL_TAG, SHARED_PATCH_ID,
)
