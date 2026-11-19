"""The shared blend shader: path and edge maths, the path, grass and snow node builders, shared
material upgrades and the overlay bank.
"""

import math
import struct


def is_path(controls):
    return (bool(getattr(controls, 'path', False)) and controls.edge_style != 'OVERLAY'
            and getattr(controls, 'direction', '') != 'PAINTED')


def path_shape(path_width, width):
    """(outer half-width, fade start k) of a path: the band is path_width wide, the fade width wide in total."""
    half, feather = max(path_width, 0.) * .5, max(width, 0.)
    outer = max(half + feather * .5, 1e-6)
    return outer, max(-1., min(.999, (half - feather * .5) / outer))


def path_mode(smooth, inner):
    return (2.0 if smooth else 1.0) + PATH_FRACTION_MIN + PATH_FRACTION_SCALE * (inner + 1.)


def decode_path_mode(mode):
    """The fade start k of a path's Factor V, or None for any other mode."""
    fraction = mode - math.floor(mode)
    if mode < 2.5 and .02 < fraction < .98:
        return (fraction - PATH_FRACTION_MIN) / PATH_FRACTION_SCALE - 1.
    return None


def shader_factor(x, mode, tangent=0., amount=0., snow=False):
    """What the shared shader does with Factor (x, mode) and Edge (tangent, amount): 0 shows A, 1 shows B."""
    inner = decode_path_mode(mode)
    if inner is not None:
        x = (1 - abs(x)) / max(1 - inner, 1e-4)
    if amount:
        x -= abs(amount) * (snow_profile(tangent) if snow else grass_profile(tangent))
    x = max(0., min(1., x))
    return x*x*(3 - 2*x) if mode > 1.5 else x


def shared_factor_mapper(controls):
    """Prepare direction/angle once, then map every corner using plain numbers."""
    x, y, offset, reverse = blend_axis(controls)
    position, width = controls.position, max(controls.width, 1e-6)
    if controls.direction == 'PAINTED':
        # One amount everywhere: Position 0 is all A, 1 all B. Painted alpha adds to it in the shader.
        mode = 2.0 if controls.smooth else 1.0
        return lambda uv: (position, mode)
    if is_path(controls):
        outer, inner = path_shape(controls.path_width, controls.width)
        mode = path_mode(controls.smooth, inner)
        def coordinate(uv):
            return (uv[0]*x + uv[1]*y + offset - position)/outer, mode
        return coordinate
    mode = 2.0 if controls.smooth else 1.0
    def coordinate(uv):
        value = (uv[0]*x + uv[1]*y + offset - position)/width + .5
        return (1.0-value if reverse else value), mode
    return coordinate


def shared_factor_coordinate(uv, controls):
    return shared_factor_mapper(controls)(uv)


def grass_profile(value):
    triangle=1-abs(2*(value%1)-1)
    return triangle**3*(.8+.2*math.cos(value*2.399963))


def snow_profile(value):
    """Smooth unequal lobes, symmetric so aligned patch endpoints agree."""
    return .5+.35*math.cos(value*math.tau)+.15*math.cos(value*math.pi)


def snow_profile_math(math_node,tangent):
    wave=math_node('snow_phase','MULTIPLY',tangent,math.tau)
    wave=math_node('snow_wave','COSINE',wave)
    wave=math_node('snow_primary','MULTIPLY_ADD',wave,.35,.5)
    broad=math_node('snow_broad_phase','MULTIPLY',tangent,math.pi)
    broad=math_node('snow_broad_wave','COSINE',broad)
    broad=math_node('snow_broad','MULTIPLY',broad,.15)
    return math_node('snow_profile','ADD',wave,broad)


def shared_edge_mapper(controls):
    x,y,_offset,reverse=blend_axis(controls)
    if reverse:x,y=-x,-y
    frequency=controls.edge_frequency
    # Painted Only has no transition line for tufts to sit on, so it gets none (snow's profile is not 0 at 0).
    amount=controls.edge_height/max(controls.width,1e-6) if controls.edge_style in SHAPED_EDGES and controls.direction!='PAINTED' else 0.
    if controls.edge_style=='SNOW':amount=-amount  # The same edge UV map encodes both styles.
    return lambda uv: ((-y*(uv[0]-.5)+x*(uv[1]-.5))*frequency,amount)


def edge_node(material,key,kind,role):
    node=next((n for n in material.node_tree.nodes if n.get(key)==role),None)
    if node is None:
        node=material.node_tree.nodes.new(kind);node[key]=role;node.label='Sprotile '+role.replace('_',' ').title()
        node.location=(-900+(len(material.node_tree.nodes)%5)*190,-1700-(len(material.node_tree.nodes)//5)*140)
    return node


def edge_profile_nodes(material,key,tangent,amount,base,snow=False):
    links=material.node_tree.links
    def math_node(role,operation,a,b=None,c=None):
        node=edge_node(material,key,'ShaderNodeMath',role);node.operation=operation
        for i,value in enumerate((a,b,c)):
            if value is None:continue
            if isinstance(value,(int,float)):node.inputs[i].default_value=value
            else:links.new(value,node.inputs[i])
        return node.outputs[0]
    if snow:
        profile=snow_profile_math(math_node,tangent)
        return math_node('snow_result','SUBTRACT',base,math_node('snow_amount','MULTIPLY',profile,amount))
    phase=math_node('edge_phase','MULTIPLY',tangent,2.)
    triangle=math_node('edge_triangle','PINGPONG',phase,1.)
    tip=math_node('edge_tip','POWER',triangle,3.)
    variation=math_node('edge_variation_phase','MULTIPLY',tangent,2.399963)
    variation=math_node('edge_variation_wave','COSINE',variation)
    variation=math_node('edge_variation','MULTIPLY_ADD',variation,.2,.8)
    profile=math_node('edge_profile','MULTIPLY',tip,variation)
    displacement=math_node('edge_amount','MULTIPLY',profile,amount)
    return math_node('edge_result','SUBTRACT',base,displacement)


def validate_grass_profile(material,key,tangent,amount,base,snow=False):
    roles={n.get(key):n for n in material.node_tree.nodes if n.get(key)}
    def check(role,operation,*inputs):
        node=roles.get(role)
        if node is None or node.bl_idname!='ShaderNodeMath' or node.operation!=operation or node.mute or node.use_clamp:
            raise ValueError('The grass-edge shader was edited; restore its generated nodes before copying or applying')
        for i,value in enumerate(inputs):
            socket=node.inputs[i]
            if isinstance(value,(int,float)):
                if socket.is_linked or socket.default_value!=struct.unpack('f',struct.pack('f',value))[0]:raise ValueError('Grass-edge shader parameters were edited')
            elif len(socket.links)!=1 or socket.links[0].from_socket!=value:raise ValueError('Grass-edge shader connections were edited')
        return node.outputs[0]
    if snow:
        profile=snow_profile_math(check,tangent)
        return check('snow_result','SUBTRACT',base,check('snow_amount','MULTIPLY',profile,amount))
    phase=check('edge_phase','MULTIPLY',tangent,2.)
    tip=check('edge_tip','POWER',check('edge_triangle','PINGPONG',phase,1.),3.)
    variation=check('edge_variation_phase','MULTIPLY',tangent,2.399963)
    variation=check('edge_variation_wave','COSINE',variation)
    variation=check('edge_variation','MULTIPLY_ADD',variation,.2,.8)
    profile=check('edge_profile','MULTIPLY',tip,variation)
    return check('edge_result','SUBTRACT',base,check('edge_amount','MULTIPLY',profile,amount))


def shared_snow_nodes(material,validate=False):
    """Extend old grass graphs without changing their nodes or positive-strength result."""
    key='sprotile_shared_role';roles=shared_nodes(material);links=material.node_tree.links
    def math_node(role,operation,*values):
        node=roles.get(role) if validate else edge_node(material,key,'ShaderNodeMath',role)
        if validate:
            if node is None or node.bl_idname!='ShaderNodeMath' or node.operation!=operation or node.mute or node.use_clamp:
                raise ValueError('Snow-edge shader nodes were edited; restore them before copying or applying')
        else:node.operation=operation
        for i,value in enumerate(values):
            socket=node.inputs[i]
            if validate:
                if isinstance(value,(int,float)):
                    if socket.is_linked or socket.default_value!=struct.unpack('f',struct.pack('f',value))[0]:raise ValueError('Snow-edge parameters were edited')
                elif len(socket.links)!=1 or socket.links[0].from_socket!=value:raise ValueError('Snow-edge connections were edited')
            elif isinstance(value,(int,float)):socket.default_value=value
            else:links.new(value,socket)
        return node.outputs[0]
    tangent,amount=roles['edge_axes'].outputs['X'],roles['edge_axes'].outputs['Y']
    profile=snow_profile_math(math_node,tangent)
    negative=math_node('snow_negative','MULTIPLY',amount,-1.)
    strength=math_node('snow_strength','MAXIMUM',negative,0.)
    combined=math_node('snow_correction_profile','ADD',roles['edge_profile'].outputs[0],profile)
    correction=math_node('snow_correction','MULTIPLY',combined,strength)
    return math_node('snow_result','SUBTRACT',roles['edge_result'].outputs[0],correction)


def validate_shared_grass(material,require_snow=False):
    roles=shared_nodes(material)
    if not {'edge_uv','edge_axes','factor_axes','gradient_linear','gradient_smoothstep'}.issubset(roles):raise ValueError('Grass-edge shader support is missing')
    uv,axes=roles['edge_uv'],roles['edge_axes']
    if uv.type!='UVMAP' or uv.uv_map!=SHARED_EDGE_UV or uv.mute or axes.mute or axes.bl_idname!='ShaderNodeSeparateXYZ' or len(axes.inputs[0].links)!=1 or axes.inputs[0].links[0].from_socket!=uv.outputs[0]:
        raise ValueError('Grass-edge UV connections were edited')
    output=validate_grass_profile(material,'sprotile_shared_role',axes.outputs['X'],axes.outputs['Y'],shared_factor_base(roles))
    if 'snow_result' in roles:output=shared_snow_nodes(material,validate=True)
    elif require_snow:raise ValueError('Snow-edge shader support is missing; reapply this patch')
    if any(len(roles[name].inputs['Value'].links)!=1 or roles[name].inputs['Value'].links[0].from_socket!=output for name in ('gradient_linear','gradient_smoothstep')):
        raise ValueError('Grass-edge gradient connections were edited')


def restore_edge_support(journal):
    material=journal['material'];nodes=material.node_tree.nodes;links=material.node_tree.links
    for node in list(nodes):
        if node.as_pointer() not in journal['nodes']:nodes.remove(node)
    for source,destination in journal['links']:links.new(source,destination)
    nodes.active=journal['active']


def ensure_shared_edge_support(material,snow=False):
    roles=shared_nodes(material);links=material.node_tree.links
    if 'edge_result' in roles:
        required={'edge_uv','edge_axes','edge_phase','edge_triangle','edge_tip','edge_variation_phase','edge_variation_wave','edge_variation','edge_profile','edge_amount'}
        if not required.issubset(roles) or roles['edge_uv'].uv_map!=SHARED_EDGE_UV:
            raise ValueError('Grass edge shader support was edited; repair the material before applying')
        validate_shared_grass(material)
        if not snow or 'snow_result' in roles:return None
    destinations=[roles[name].inputs['Value'] for name in ('gradient_linear','gradient_smoothstep')]
    source=roles['edge_result'].outputs[0] if 'edge_result' in roles else shared_factor_base(roles)
    if any(len(socket.links)!=1 or socket.links[0].from_socket!=source for socket in destinations):
        raise ValueError('The shared gradient was edited; restore it before adding a grass edge')
    journal=dict(material=material,nodes={n.as_pointer() for n in material.node_tree.nodes},links=[(source,d) for d in destinations],active=material.node_tree.nodes.active)
    try:
        result=source
        if 'edge_result' not in roles:
            uv=edge_node(material,'sprotile_shared_role','ShaderNodeUVMap','edge_uv');uv.uv_map=SHARED_EDGE_UV
            axes=edge_node(material,'sprotile_shared_role','ShaderNodeSeparateXYZ','edge_axes');links.new(uv.outputs[0],axes.inputs[0])
            result=edge_profile_nodes(material,'sprotile_shared_role',axes.outputs['X'],axes.outputs['Y'],source)
        if snow:result=shared_snow_nodes(material)
        for destination in destinations:links.new(result,destination)
        material.node_tree.nodes.active=journal['active']
        return journal
    except Exception:restore_edge_support(journal);raise


def shared_factor_base(roles):
    """What the gradients (or the tuft chain) read: the painted-alpha stage when it exists, else the path band, else Factor U."""
    if 'alpha_result' in roles:
        return roles['alpha_result'].outputs[0]
    return roles['path_result'].outputs[0] if 'path_result' in roles else roles['factor_axes'].outputs['X']


def shared_path_nodes(material, validate=False):
    """Build (or check) the nodes that turn a path's Factor U into a band; other modes pass through unchanged."""
    key = 'sprotile_shared_role'; roles = shared_nodes(material); links = material.node_tree.links
    f32 = lambda value: struct.unpack('f', struct.pack('f', value))[0]
    def math_node(role, operation, *values):
        node = roles.get(role) if validate else edge_node(material, key, 'ShaderNodeMath', role)
        if validate:
            if node is None or node.bl_idname != 'ShaderNodeMath' or node.operation != operation or node.mute or node.use_clamp:
                raise ValueError('Path shader nodes were edited; restore them before copying or applying')
        else:
            node.operation = operation; node.use_clamp = False
        for i, value in enumerate(values):
            socket = node.inputs[i]
            if validate:
                if isinstance(value, (int, float)):
                    if socket.is_linked or socket.default_value != f32(value):
                        raise ValueError('Path shader values were edited')
                elif len(socket.links) != 1 or socket.links[0].from_socket != value:
                    raise ValueError('Path shader connections were edited')
            elif isinstance(value, (int, float)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]
    axes = roles['factor_axes']
    x, y = axes.outputs['X'], axes.outputs['Y']
    fraction = math_node('path_fraction', 'MODULO', y, 1.)
    on = math_node('path_on', 'MULTIPLY',
                   math_node('path_has_fraction', 'MULTIPLY', math_node('path_fraction_low', 'GREATER_THAN', fraction, .02),
                             math_node('path_fraction_high', 'LESS_THAN', fraction, .98)),
                   math_node('path_below_overlays', 'LESS_THAN', y, 2.5))
    inner = math_node('path_inner', 'MULTIPLY_ADD', fraction, 1 / PATH_FRACTION_SCALE,
                      -(PATH_FRACTION_MIN / PATH_FRACTION_SCALE + 1))
    span = math_node('path_span', 'MAXIMUM', math_node('path_span_raw', 'SUBTRACT', 1., inner), 1e-4)
    band = math_node('path_band', 'DIVIDE', math_node('path_remaining', 'SUBTRACT', 1., math_node('path_distance', 'ABSOLUTE', x)), span)
    gated = math_node('path_gated', 'MULTIPLY', math_node('path_delta', 'SUBTRACT', band, x), on)
    return math_node('path_result', 'ADD', x, gated)


def path_consumers(roles):
    """Inputs that read the factor base: the alpha stage, else the tuft chain's base, else the two gradients."""
    if 'alpha_result' in roles:
        return [roles['alpha_result'].inputs[0]]
    if 'edge_result' in roles:
        return [roles['edge_result'].inputs[0]]
    return [roles[name].inputs['Value'] for name in ('gradient_linear', 'gradient_smoothstep')]


def validate_shared_path(material):
    roles = shared_nodes(material)
    if 'path_result' not in roles or 'factor_axes' not in roles:
        raise ValueError('Path shader support is missing; choose New Blend, tick Path and Apply')
    output = shared_path_nodes(material, validate=True)
    if any(len(socket.links) != 1 or socket.links[0].from_socket != output for socket in path_consumers(roles)):
        raise ValueError('Path shader connections were edited; restore them before copying or applying')


def ensure_shared_path_support(material):
    """Insert the path nodes between Factor U and its readers. Return a rollback journal, or None if present."""
    roles = shared_nodes(material)
    if 'path_result' in roles:
        validate_shared_path(material)
        return None
    if 'edge_result' in roles:
        validate_shared_grass(material)
    source = roles['factor_axes'].outputs['X']
    destinations = path_consumers(roles)
    if any(len(socket.links) != 1 or socket.links[0].from_socket != source for socket in destinations):
        raise ValueError('The shared gradient was edited; restore it before adding a path')
    journal = dict(material=material, nodes={n.as_pointer() for n in material.node_tree.nodes},
                   links=[(source, d) for d in destinations], active=material.node_tree.nodes.active)
    try:
        result = shared_path_nodes(material)
        for destination in destinations:
            material.node_tree.links.new(result, destination)
        material.node_tree.nodes.active = journal['active']
        return journal
    except Exception:
        restore_edge_support(journal)
        raise


def shared_nodes(material):
    if material_nodes(material) is None:
        return {}
    return {node.get('sprotile_shared_role'): node for node in material.node_tree.nodes
            if node.get('sprotile_shared_role')}


ATLAS_ROLES = ('original', 'texture_a', 'texture_b')


def shared_atlas_nodes(material):
    """The atlas image nodes of a shared blend material: the original, A and B samplers, plus untagged image nodes that
    read one of their images. Overlay and other tagged nodes hold other images on purpose and are left out."""
    if material is None or not material.get(SHARED_MATERIAL_TAG) or material_nodes(material) is None:
        return []
    nodes = [node for node in material.node_tree.nodes if node.type == 'TEX_IMAGE']
    roles = [node for node in nodes if node.get('sprotile_shared_role') in ATLAS_ROLES]
    known = {node.image for node in roles if node.image is not None}
    return roles + [node for node in nodes if not node.get('sprotile_shared_role') and node.image in known]


def shared_atlas_mismatch(material):
    """Names of the images the atlas nodes of a shared blend material read when they are not all one image; else empty."""
    images = {node.image for node in shared_atlas_nodes(material) if node.image is not None}
    return {image.name for image in images} if len(images) > 1 else set()


def shared_atlas_node(material, image):
    if material is None or material.library is not None or material_nodes(material) is None:
        raise ValueError("Shared blending needs an editable atlas material on every selected face")
    if material.get(SHARED_MATERIAL_TAG):
        roles = shared_nodes(material)
        required = {'original', 'uv_0', 'uv_1', 'texture_a', 'texture_b', 'factor', 'factor_axes',
                    'gradient_linear', 'gradient_smoothstep', 'smooth', 'curve', 'tiles', 'enabled', 'result'}
        if not required.issubset(roles):
            raise ValueError("The shared blend graph was changed; repair it or use Separate Material")
        names = shared_atlas_mismatch(material)
        if any(roles[key].image != image for key in ('original', 'texture_a', 'texture_b')):
            sampled = sorted(names - {image.name if image else ''}) or sorted(names)
            raise ValueError(f"Material '{material.name}' samples atlas '{', '.join(sampled)}' but the blend uses "
                             f"'{image.name if image else 'none'}'; click Use One Atlas or use Separate Material")
        return roles['original']
    candidates = [node for node in material.node_tree.nodes
                  if node.type == 'TEX_IMAGE' and node.image == image and node.outputs['Color'].is_linked]
    if len(candidates) != 1:
        raise ValueError("Shared blending needs one connected atlas image node; use Separate Material for complex/grouped shaders")
    # Avoid changing an image used only for a normal/displacement branch.
    if image_from_material(material) != image:
        raise ValueError("The selected material's visible atlas does not match A/B")
    return candidates[0]


def upgrade_shared_material(material, original):
    """Insert before existing Tint/lighting. Return a rollback journal, never clear user nodes."""
    if material.get(SHARED_MATERIAL_TAG):
        return None
    nodes, links = material.node_tree.nodes, material.node_tree.links
    journal = dict(material=material, nodes=set(node.as_pointer() for node in nodes),
                   original=original, destinations=[link.to_socket for link in original.outputs['Color'].links],
                   role=original.get('sprotile_shared_role'), active=nodes.active)
    try:
        original['sprotile_shared_role'] = 'original'
        origin = original.location.copy()

        def node(kind, role, x, y):
            item = nodes.new(kind)
            item['sprotile_shared_role'] = role
            item.label = 'Sprotile ' + role.replace('_', ' ').title()
            item.location = (origin.x + x, origin.y + y)
            return item

        samples = []
        for index, name in enumerate(('', BLEND_B_UV)):   # '' samples the render map, which holds tile A
            uv = node('ShaderNodeUVMap', 'uv_' + str(index), -600, -300 - index * 240)
            uv.uv_map = name
            tex = node('ShaderNodeTexImage', 'texture_a' if index == 0 else 'texture_b', -380, -300 - index * 240)
            tex.image = original.image
            tex.interpolation = original.interpolation
            tex.extension = original.extension
            links.new(uv.outputs[0], tex.inputs[0])
            samples.append(tex)
        uv = node('ShaderNodeUVMap', 'factor', -900, -850)
        uv.uv_map = SHARED_FACTOR_UV
        axis = node('ShaderNodeSeparateXYZ', 'factor_axes', -680, -850)
        links.new(uv.outputs[0], axis.inputs[0])
        gradients = []
        for index, interpolation in enumerate(('LINEAR', 'SMOOTHSTEP')):
            gradient = node('ShaderNodeMapRange', 'gradient_' + interpolation.lower(), -420, -850 - index * 260)
            gradient.data_type = 'FLOAT'
            gradient.interpolation_type = interpolation
            gradient.clamp = True
            links.new(axis.outputs['X'], gradient.inputs['Value'])
            gradients.append(gradient)
        smooth = node('ShaderNodeMath', 'smooth', -420, -1420)
        smooth.operation = 'GREATER_THAN'; smooth.inputs[1].default_value = 1.5
        links.new(axis.outputs['Y'], smooth.inputs[0])
        factor = node('ShaderNodeMixRGB', 'curve', -120, -850)
        links.new(smooth.outputs[0], factor.inputs[0])
        links.new(gradients[0].outputs[0], factor.inputs[1])
        links.new(gradients[1].outputs[0], factor.inputs[2])
        mix = node('ShaderNodeMixRGB', 'tiles', -100, -300)
        links.new(factor.outputs[0], mix.inputs[0])
        links.new(samples[0].outputs['Color'], mix.inputs[1])
        links.new(samples[1].outputs['Color'], mix.inputs[2])
        enabled = node('ShaderNodeMath', 'enabled', -120, -1150)
        enabled.operation = 'GREATER_THAN'; enabled.inputs[1].default_value = 0.5
        links.new(axis.outputs['Y'], enabled.inputs[0])
        result = node('ShaderNodeMixRGB', 'result', 150, -100)
        links.new(enabled.outputs[0], result.inputs[0])
        links.new(original.outputs['Color'], result.inputs[1])
        links.new(mix.outputs[0], result.inputs[2])
        for socket in journal['destinations']:
            links.new(result.outputs[0], socket)
        material[SHARED_MATERIAL_TAG] = 1
        nodes.active = journal['active']
        return journal
    except Exception:
        restore_shared_material(journal)
        raise


def restore_shared_material(journal):
    material = journal['material']
    nodes, links = material.node_tree.nodes, material.node_tree.links
    for node in list(nodes):
        if node.as_pointer() not in journal['nodes']:
            nodes.remove(node)
    original = journal['original']
    for socket in journal['destinations']:
        links.new(original.outputs['Color'], socket)
    if journal['role'] is None:
        if 'sprotile_shared_role' in original:
            del original['sprotile_shared_role']
    else:
        original['sprotile_shared_role'] = journal['role']
    if SHARED_MATERIAL_TAG in material:
        del material[SHARED_MATERIAL_TAG]
    nodes.active = journal['active']


def linked_output(socket):
    return socket.links[0].from_socket if socket.is_linked else None


def setup_overlay_sampler(node, reference, image, vector, links):
    node.image = image
    node.projection = 'FLAT'
    node.interpolation = reference.interpolation
    node.extension = reference.extension
    links.new(vector, node.inputs['Vector'])


def check_overlay_samplers(color, alpha, vector):
    for node in (color, alpha):
        if (node.type != 'TEX_IMAGE' or node.mute or node.image is None or node.projection != 'FLAT'
                or linked_output(node.inputs['Vector']) != vector):
            raise ValueError('Overlay sampling was edited. Restore the generated UV and image connections.')
    if (alpha.image != color.image or alpha.interpolation != color.interpolation
            or alpha.extension != color.extension):
        raise ValueError('The overlay colour and alpha must sample the same image and UVs.')


def update_separate_overlay(material, roles, opacity):
    """Read RGBA together so the mix applies the source alpha exactly once."""
    mix, color, uv = (roles.get(role) for role in ('texture_mix', 'texture_b', 'texture_b_uv'))
    if None in (mix, color, uv):
        return
    nodes, links = material.node_tree.nodes, material.node_tree.links
    active = nodes.active
    try:
        alpha = edge_node(material, 'sprotile_blend_role', 'ShaderNodeTexImage', 'overlay_alpha')
        setup_overlay_sampler(alpha, color, color.image, uv.outputs['UV'], links)
        amount = edge_node(material, 'sprotile_blend_role', 'ShaderNodeMath', 'overlay_amount')
        amount.operation = 'MULTIPLY'
        amount.inputs[1].default_value = opacity / 100
        links.new(alpha.outputs['Alpha'], amount.inputs[0])
        links.new(amount.outputs[0], mix.inputs[0])
        links.new(alpha.outputs['Color'], mix.inputs[2])
    finally:
        nodes.active = active
    material.update_tag()


def check_separate_overlay(material, opacity):
    roles = blend_nodes(material)
    required = {'texture_a', 'texture_b', 'texture_b_uv', 'overlay_alpha', 'overlay_amount', 'texture_mix'}
    if not required.issubset(roles):
        raise ValueError('Overlay nodes are missing. Reapply the overlay before copying it.')
    color, alpha, amount, mix = (roles[r] for r in ('texture_b', 'overlay_alpha', 'overlay_amount', 'texture_mix'))
    check_overlay_samplers(color, alpha, roles['texture_b_uv'].outputs[0])
    if (amount.type != 'MATH' or amount.operation != 'MULTIPLY' or amount.mute or amount.use_clamp
            or linked_output(amount.inputs[0]) != alpha.outputs['Alpha']
            or amount.inputs[1].is_linked or abs(amount.inputs[1].default_value - opacity / 100) > 2e-6
            or mix.type != 'MIX_RGB' or mix.mute or mix.blend_type != 'MIX'
            or linked_output(mix.inputs[0]) != amount.outputs[0]
            or linked_output(mix.inputs[1]) != roles['texture_a'].outputs['Color']
            or linked_output(mix.inputs[2]) != alpha.outputs['Color']):
        raise ValueError('The overlay shader differs from its settings. Reapply it before copying.')


def shared_overlay_bank(material):
    """Trace the live selectors back to the ordinary blend. Codes are local to each material."""
    roles = shared_nodes(material)
    types = {'result':'MIX_RGB', 'tiles':'MIX_RGB', 'texture_a':'TEX_IMAGE', 'texture_b':'TEX_IMAGE',
             'uv_0':'UVMAP', 'uv_1':'UVMAP', 'factor':'UVMAP', 'factor_axes':'SEPXYZ'}
    if any(name not in roles or roles[name].type != kind or roles[name].mute for name,kind in types.items()):
        raise ValueError('The shared blend shader is incomplete. Restore its nodes before using an overlay.')
    if (roles['factor'].uv_map != SHARED_FACTOR_UV or not is_render_uv_node(roles['uv_0'])
            or roles['uv_1'].uv_map != BLEND_B_UV
            or linked_output(roles['factor_axes'].inputs[0]) != roles['factor'].outputs[0]
            or linked_output(roles['texture_a'].inputs['Vector']) != roles['uv_0'].outputs[0]):
        raise ValueError('The shared overlay UV connections were edited. Restore the generated inputs.')
    sources = {}
    output = linked_output(roles['result'].inputs[2])
    while output != roles['tiles'].outputs[0]:
        switch = output.node if output else None
        code = switch.get('sprotile_overlay_code') if switch else None
        if not isinstance(code, int) or code < FIRST_OVERLAY_CODE or code in sources:
            raise ValueError('The shared overlay selector was edited. Restore the generated shader connections.')
        prefix = f'overlay_{code}_'
        required = {prefix + role for role in ('alpha','amount','mix','select','switch')}
        if not required.issubset(roles):
            raise ValueError('Shared overlay nodes are missing.')
        alpha, amount, mix, select = (roles[prefix+r] for r in ('alpha','amount','mix','select'))
        if (alpha.type != 'TEX_IMAGE' or alpha.mute or alpha.image is None or alpha.projection != 'FLAT'
                or linked_output(alpha.inputs['Vector']) != roles['uv_1'].outputs[0]):
            raise ValueError('Overlay source or UV connection is missing.')
        axes = roles['factor_axes']
        if (switch != roles[prefix+'switch'] or switch.type != 'MIX_RGB' or switch.blend_type != 'MIX'
                or mix.type != 'MIX_RGB' or mix.blend_type != 'MIX'
                or amount.type != 'MATH' or amount.operation != 'MULTIPLY'
                or select.type != 'MATH' or select.operation != 'COMPARE'
                or any(node.mute or node.use_clamp for node in (mix, switch, select, amount))
                or output != switch.outputs[0]
                or linked_output(select.inputs[0]) != axes.outputs['Y']
                or select.inputs[1].is_linked or select.inputs[1].default_value != code
                or select.inputs[2].is_linked or select.inputs[2].default_value != .25
                or linked_output(amount.inputs[0]) != alpha.outputs['Alpha']
                or linked_output(amount.inputs[1]) != axes.outputs['X']
                or linked_output(mix.inputs[0]) != amount.outputs[0]
                or linked_output(mix.inputs[1]) != roles['texture_a'].outputs['Color']
                or linked_output(mix.inputs[2]) != alpha.outputs['Color']
                or linked_output(switch.inputs[0]) != select.outputs[0]
                or linked_output(switch.inputs[2]) != mix.outputs[0]):
            raise ValueError('The shared overlay shader was edited. Restore its generated nodes.')
        sources[code] = alpha
        output = linked_output(switch.inputs[1])
    if sum('sprotile_overlay_code' in n for n in material.node_tree.nodes) != len(sources):
        raise ValueError('Disconnected overlay selectors found. Restore their connections before applying.')
    return sources


def ensure_shared_overlay(material, image):
    sources = shared_overlay_bank(material)
    for code, sampler in sources.items():
        if sampler.image == image:
            return code, None
    code = max(sources, default=FIRST_OVERLAY_CODE-1) + 1
    if code - FIRST_OVERLAY_CODE >= OVERLAY_INDEX_LIMIT:
        raise ValueError(f'This material already holds {OVERLAY_INDEX_LIMIT} overlay images, the most a face can '
                         'name; use a copy of the material or Separate Material')
    roles = shared_nodes(material)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    target = roles['result'].inputs[2]
    previous = linked_output(target)
    journal = dict(material=material, nodes={n.as_pointer() for n in nodes},
                   links=[(previous, target)], active=nodes.active)
    try:
        frame = nodes.new('NodeFrame')
        frame.label = 'Sprotile Overlay | ' + image.name
        frame.location = (roles['tiles'].location.x, roles['tiles'].location.y - 2200 - 550*len(sources))
        def node(kind, role, x, y):
            item = nodes.new(kind)
            item['sprotile_shared_role'] = f'overlay_{code}_{role}'
            item.parent = frame
            item.label = role.title()
            item.location = (x, y)
            return item
        uv = roles['uv_1'].outputs[0]
        alpha = node('ShaderNodeTexImage', 'alpha', 0, 0)
        alpha.label = 'Overlay RGBA'
        setup_overlay_sampler(alpha, roles['texture_b'], image, uv, links)
        amount = node('ShaderNodeMath', 'amount', 280, -180)
        amount.operation = 'MULTIPLY'
        links.new(alpha.outputs['Alpha'], amount.inputs[0])
        links.new(roles['factor_axes'].outputs['X'], amount.inputs[1])
        mix = node('ShaderNodeMixRGB', 'mix', 480, 0)
        links.new(amount.outputs[0], mix.inputs[0])
        links.new(roles['texture_a'].outputs['Color'], mix.inputs[1])
        links.new(alpha.outputs['Color'], mix.inputs[2])
        select = node('ShaderNodeMath', 'select', 480, -220)
        select.operation = 'COMPARE'
        links.new(roles['factor_axes'].outputs['Y'], select.inputs[0])
        select.inputs[1].default_value = code
        select.inputs[2].default_value = .25
        switch = node('ShaderNodeMixRGB', 'switch', 690, 0)
        switch['sprotile_overlay_code'] = code
        links.new(select.outputs[0], switch.inputs[0])
        links.new(previous, switch.inputs[1])
        links.new(mix.outputs[0], switch.inputs[2])
        links.new(switch.outputs[0], target)
        nodes.active = journal['active']
        return code, journal
    except Exception:
        restore_edge_support(journal)
        raise


# Names from other Sprotile modules, used when functions run. Imported after this module's own
# definitions so that modules which use each other can load (see __init__.py).
from .core import material_nodes
from .tiles import image_from_material
from .blend_settings import (
    BLEND_B_UV, FIRST_OVERLAY_CODE, OVERLAY_INDEX_LIMIT, PATH_FRACTION_MIN, PATH_FRACTION_SCALE,
    SHAPED_EDGES, SHARED_EDGE_UV, SHARED_FACTOR_UV, SHARED_MATERIAL_TAG, blend_axis, blend_nodes,
)
from .blend_storage import is_render_uv_node
