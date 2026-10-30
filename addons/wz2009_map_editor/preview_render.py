"""Offline orthographic preview projection using Blender's existing CPU Cycles.

The image depicts the supplied visual geometry. It does not establish physical
meter calibration, collision, playable boundaries, or navigability.
"""
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile


def _size(size):
    if not isinstance(size, (list, tuple)) or len(size) != 2 or any(
            type(value) is not int or not 1 <= value <= 32768 for value in size):
        raise ValueError('size: two integer pixel dimensions in 1..32768 required')
    return tuple(size)


def square_bounds(bounds, size=(1024, 1024)):
    """Pad bounds about their center to exactly match the pixel viewport aspect."""
    width, height = _size(size)
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in bounds):
        raise ValueError('bounds: four finite coordinates required')
    x0, y0, x1, y1 = (float(value) for value in bounds)
    dx, dy = x1-x0, y1-y0
    if not math.isfinite(dx) or not math.isfinite(dy) or dx <= 0 or dy <= 0:
        raise ValueError('bounds: positive finite extents required')
    aspect = width / height
    if dx/dy < aspect:
        padding = (dy*aspect-dx)/2
        x0 -= padding
        x1 += padding
    else:
        padding = (dx/aspect-dy)/2
        y0 -= padding
        y1 += padding
    result = [x0, y0, x1, y1]
    if not all(math.isfinite(value) for value in result):
        raise ValueError('bounds: padded extent overflow')
    return result


def _selected_objects(scene):
    return [obj for obj in scene.objects if getattr(obj, 'type', '') in {'MESH', 'CURVE', 'EMPTY'}
            and (obj.get('wz09_preview_only') or obj.get('wz09_custom_geometry') or obj.get('wz09_type'))]


def _object_id(obj):
    if obj.get('wz09_preview_only'):
        value = obj.get('wz09_preview_id')
    elif obj.get('wz09_type'):
        value = obj.get('wz09_id')
    else:
        value = obj.get('wz09_geometry_id')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Preview/authored objects need persistent preview, object, or geometry IDs')
    return value


def _plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Preview state contains a nonfinite value')
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if hasattr(value, 'get') and any(value.get(key) for key in ('wz09_preview_only', 'wz09_type', 'wz09_custom_geometry')):
        return {'object_id': _object_id(value)}
    if hasattr(value, '__iter__'):
        return [_plain(item) for item in value]
    return {'type': type(value).__name__, 'name': str(getattr(value, 'name_full', ''))}


def _put(digest, tag, value):
    payload = json.dumps([tag, _plain(value)], sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest.update(struct.pack('<Q', len(payload)))
    digest.update(payload)


def _cache_key(data):
    # RNA wrappers may be recreated between attribute reads. The datablock's
    # pointer stays stable for the lifetime of this non-mutating hash operation.
    return data.as_pointer() if hasattr(data, 'as_pointer') else id(data)


def _rna_state(owner):
    """Rendering-relevant scalar/RNA properties, excluding UI layout and pointers."""
    ignored = {'rna_type', 'id_data', 'name', 'label', 'location', 'dimensions', 'width', 'height',
               'select', 'show_options', 'show_preview', 'show_texture', 'hide', 'parent', 'color',
               'use_custom_color', 'inputs', 'outputs', 'internal_links', 'node_tree', 'image'}
    result = {}
    properties = getattr(getattr(owner, 'bl_rna', None), 'properties', ())
    for prop in properties:
        key = prop.identifier
        if key in ignored or getattr(prop, 'is_readonly', False) or getattr(prop, 'type', '') == 'COLLECTION':
            continue
        value = getattr(owner, key, None)
        result[key] = _plain(value)
    # Useful also for small offline fakes without Blender RNA.
    for key in ('operation', 'blend_type', 'data_type', 'interpolation', 'extension', 'projection'):
        if hasattr(owner, key):
            result[key] = _plain(getattr(owner, key))
    return result


def _image_digest(image):
    digest = hashlib.sha256()
    path = str(getattr(image, 'filepath', ''))
    _put(digest, 'image', {'path': path, 'size': list(getattr(image, 'size', ())),
                          'source': getattr(image, 'source', ''),
                          'colorspace': getattr(getattr(image, 'colorspace_settings', None), 'name', '')})
    packed_files = list(getattr(image, 'packed_files', ()))
    packed = getattr(image, 'packed_file', None)
    if packed_files:
        for item in sorted(packed_files, key=lambda value: str(getattr(value, 'filepath', ''))):
            _put(digest, 'packed_path', str(getattr(item, 'filepath', '')))
            digest.update(bytes(item.packed_file.data))
    elif packed is not None:
        digest.update(bytes(packed.data))
    elif path:
        if path.startswith('//'):
            import bpy
            path = bpy.path.abspath(path, library=getattr(image, 'library', None))
        source = Path(path).expanduser()
        tiles = getattr(image, 'tiles', ()) if '<UDIM>' in path else ()
        paths = [Path(path.replace('<UDIM>', str(tile.number))) for tile in tiles] or [source]
        for source in sorted(paths):
            _put(digest, 'image_file', str(source))
            if source.is_file():
                with source.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024*1024), b''):
                        digest.update(block)
            else:
                _put(digest, 'missing', True)
    if (not path and packed is None and not packed_files) or getattr(image, 'is_dirty', False):
        pixels = getattr(image, 'pixels', ())
        for start in range(0, len(pixels), 8192):
            digest.update(array('f', pixels[start:start+8192]).tobytes())
    return digest.hexdigest()


def _material_digest(material, cache, image_cache, tree_stack=None):
    if material is None:
        return None
    key = _cache_key(material)
    if key in cache:
        return cache[key]
    tree_stack = set() if tree_stack is None else tree_stack
    if key in tree_stack:
        raise ValueError('Recursive material node group is unsupported')
    tree_stack.add(key)
    digest = hashlib.sha256()
    for name in ('diffuse_color', 'metallic', 'roughness', 'use_nodes', 'surface_render_method',
                 'use_backface_culling'):
        if hasattr(material, name):
            _put(digest, name, getattr(material, name))
    tree = getattr(material, 'node_tree', None)
    if tree is None and hasattr(material, 'nodes'):
        tree = material  # ShaderNodeTree used by a group node.
    if tree is not None:
        for node in sorted(tree.nodes, key=lambda item: item.name):
            _put(digest, 'node', [node.name, node.bl_idname, _rna_state(node)])
            for direction in ('inputs', 'outputs'):
                for socket in getattr(node, direction, ()):
                    if hasattr(socket, 'default_value'):
                        _put(digest, 'socket', [direction, socket.identifier, socket.default_value])
            ramp = getattr(node, 'color_ramp', None)
            if ramp is not None:
                _put(digest, 'ramp', [getattr(ramp, 'color_mode', ''), getattr(ramp, 'hue_interpolation', ''),
                                     getattr(ramp, 'interpolation', ''),
                                     [[element.position, list(element.color)] for element in ramp.elements]])
            image_user = getattr(node, 'image_user', None)
            if image_user is not None:
                _put(digest, 'image_user', _rna_state(image_user))
            image = getattr(node, 'image', None)
            if image is not None:
                image_key = _cache_key(image)
                if image_key not in image_cache:
                    image_cache[image_key] = _image_digest(image)
                _put(digest, 'image', image_cache[image_key])
            group = getattr(node, 'node_tree', None)
            if group is not None:
                _put(digest, 'group', _material_digest(group, cache, image_cache, tree_stack))
        _put(digest, 'links', sorted([link.from_node.name, link.from_socket.identifier,
                                    link.to_node.name, link.to_socket.identifier] for link in tree.links))
    result = digest.hexdigest()
    tree_stack.remove(key)
    cache[key] = result
    return result


def _put_array(digest, tag, values):
    """Stream a typed, length-delimited array in canonical little-endian order."""
    if values.typecode in ('f', 'd') and any(not math.isfinite(value) for value in values):
        raise ValueError('Preview geometry contains a nonfinite value')
    _put(digest, tag, [values.typecode, values.itemsize, len(values)])
    if struct.pack('=I', 1) != struct.pack('<I', 1):
        values.byteswap()
    digest.update(memoryview(values).cast('B'))


def _collection_array(collection, attribute, typecode, width=1, default=None):
    """Use RNA bulk reads, with an equivalent pure-list/unsupported-RNA fallback.

    Blender coordinates, UVs, normals and colors are float32 values. Keeping that
    native buffer type lets foreach_get stream directly without Python objects.
    """
    count = len(collection)*width
    values = array(typecode, [0])*count
    if count and hasattr(collection, 'foreach_get'):
        try:
            collection.foreach_get(attribute, values)
            return values
        except (TypeError, AttributeError, RuntimeError, ValueError, BufferError):
            # Some Blender versions cannot expose a converted RNA property via
            # foreach_get (e.g. byte color attributes). Retain the full fallback.
            pass
    values = array(typecode)
    try:
        if width == 1:
            values.extend(getattr(item, attribute, default) for item in collection)
        else:
            for item in collection:
                components = getattr(item, attribute)
                if len(components) != width:
                    raise ValueError('Preview geometry vector has an unexpected dimension')
                values.extend(components)
    except (TypeError, OverflowError) as exc:
        raise ValueError(f'Preview geometry property {attribute} cannot be packed') from exc
    return values


def _mesh_digest(data):
    digest = hashlib.sha256()
    if hasattr(data, 'vertices'):
        _put_array(digest, 'vertex_co', _collection_array(data.vertices, 'co', 'f', 3))
        # Deform weights are nested sparse data, without a collection bulk API.
        # Record the vertex index explicitly so moving a weight also changes SHA.
        for index, vertex in enumerate(data.vertices):
            weights = getattr(vertex, 'groups', ())
            if weights:
                _put(digest, 'vertex_weights', [index, [[weight.group, weight.weight] for weight in weights]])
        if hasattr(data, 'loops'):
            starts = _collection_array(data.polygons, 'loop_start', 'i')
            totals = _collection_array(data.polygons, 'loop_total', 'i')
            indices = _collection_array(data.loops, 'vertex_index', 'i')
        else:
            # Pure offline fakes and older mesh adapters expose polygon vertices.
            starts, totals, indices = array('i'), array('i'), array('i')
            for polygon in data.polygons:
                starts.append(len(indices))
                totals.append(len(polygon.vertices))
                indices.extend(polygon.vertices)
        _put_array(digest, 'polygon_loop_start', starts)
        _put_array(digest, 'polygon_loop_total', totals)
        _put_array(digest, 'loop_vertex_index', indices)
        _put_array(digest, 'polygon_material', _collection_array(data.polygons, 'material_index', 'i'))
        _put_array(digest, 'polygon_smooth', _collection_array(data.polygons, 'use_smooth', 'b', default=False))
        for layer in getattr(data, 'uv_layers', ()):
            _put(digest, 'uv_layer', [layer.name, bool(getattr(layer, 'active_render', False))])
            _put_array(digest, 'uv', _collection_array(layer.data, 'uv', 'f', 2))
        _put_array(digest, 'normal', _collection_array(getattr(data, 'corner_normals', ()), 'vector', 'f', 3))
        for layer in getattr(data, 'color_attributes', ()):
            _put(digest, 'color_layer', [layer.name, layer.domain, layer.data_type])
            _put_array(digest, 'color', _collection_array(layer.data, 'color', 'f', 4))
        shape_keys = getattr(data, 'shape_keys', None)
        if shape_keys is not None:
            for block in shape_keys.key_blocks:
                _put(digest, 'shape_key', [block.name, block.value, block.mute, block.interpolation])
                _put_array(digest, 'shape_point', _collection_array(block.data, 'co', 'f', 3))
    else:
        _put(digest, 'curve', _rna_state(data))
        for spline in getattr(data, 'splines', ()):
            _put(digest, 'spline', _rna_state(spline))
            for point in spline.points:
                _put(digest, 'point', [list(point.co), point.radius, point.tilt])
            for point in spline.bezier_points:
                _put(digest, 'bezier', [list(point.co), list(point.handle_left), list(point.handle_right),
                                      point.handle_left_type, point.handle_right_type, point.radius, point.tilt])
    return digest.hexdigest()


def preview_fingerprint(scene):
    """Hash IDs, world matrices, unique geometry, UVs, material graphs and textures.

    Each shared geometry/material/image datablock is streamed once. Camera,
    original lights, selection and display names do not affect this projection.
    No preview source file is silently treated as a collider or meter survey.
    """
    objects = _selected_objects(scene)
    ordered = sorted(((_object_id(obj), obj) for obj in objects), key=lambda item: item[0])
    ids = [uid for uid, obj in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate preview/authored IDs; duplicate maps through the WZ2009 panel')
    digest = hashlib.sha256()
    _put(digest, 'projection_version', 2)
    _put(digest, 'frame', getattr(scene, 'frame_current', 1))
    geometry_cache, material_cache, image_cache = {}, {}, {}
    for uid, obj in ordered:
        _put(digest, 'object', [uid, obj.type, [list(row) for row in obj.matrix_world],
                              bool(getattr(obj, 'hide_render', False))])
        for modifier in getattr(obj, 'modifiers', ()):
            _put(digest, 'modifier', [modifier.type, _rna_state(modifier)])
        for constraint in getattr(obj, 'constraints', ()):
            _put(digest, 'constraint', [constraint.type, _rna_state(constraint)])
        _put(digest, 'vertex_groups', [[group.name, group.index] for group in getattr(obj, 'vertex_groups', ())])
        data = getattr(obj, 'data', None)
        if data is None:
            continue
        data_key = _cache_key(data)
        if data_key not in geometry_cache:
            geometry_cache[data_key] = _mesh_digest(data)
        _put(digest, 'geometry', geometry_cache[data_key])
        slots = getattr(obj, 'material_slots', None)
        materials = [slot.material for slot in slots] if slots is not None else getattr(data, 'materials', ())
        _put(digest, 'materials', [_material_digest(material, material_cache, image_cache)
                                   for material in materials])
    return digest.hexdigest()


def _height_projection(objects):
    nav = [float(obj.matrix_world.translation.z) for obj in objects if obj.get('wz09_type') == 'nav_node']
    if nav:
        low, high = min(nav), max(nav)
        basis = 'authored_nav_point_heights'
    else:
        heights = [float(obj.matrix_world.translation.z) for obj in objects]
        low, high = min(heights), max(heights)
        basis = 'authored_object_origins_without_nav'
    if not all(math.isfinite(value) for value in (low, high)):
        raise ValueError('Preview camera height range is nonfinite')
    return {'camera_z_m': high+40.0, 'clip_start_m': 0.05,
            'clip_end_m': max(80.0, high-low+80.0), 'height_basis': basis,
            'reference_z_range_m': [low, high], 'vertical_guard_m': 40.0,
            'view': 'orthographic_xy_top_down', 'camera_forward': [0, 0, -1],
            'image_up': [0, 1, 0], 'sensor_fit': 'HORIZONTAL',
            'limitation': 'Geometry above the camera or outside the guarded height interval is clipped; '
                          'this projection is visual preview evidence only.'}


def render_orthographic(ctx, path, bounds, size=(1024, 1024), root=None):
    """Render once in a transient scene and atomically publish a new editor PNG.

    Original cameras/lights and all original scene render settings are untouched.
    Shared authored objects are linked temporarily, then unlinked with the scene.
    The camera looks down world -Z with world +Y at the image's upper edge.
    """
    if root is None:
        raise ValueError('Explicit editor repository root required')
    from .scene_io import safe_output_path
    from .tactical_map import _write_new
    import bpy
    root = Path(root).expanduser().resolve()
    path = safe_output_path(path, root=root)
    if path.suffix.lower() != '.png':
        raise ValueError('Orthographic preview output must use .png')
    if path.exists():
        raise FileExistsError(f'Refusing to overwrite preview: {path}; choose a new version')
    width, height = _size(size)
    actual = square_bounds(bounds, size)
    objects = _selected_objects(ctx.scene)
    if not any(obj.type in {'MESH', 'CURVE'} for obj in objects):
        raise ValueError('No authored preview or custom mesh/curve geometry available')
    source_fingerprint = preview_fingerprint(ctx.scene)
    projection = _height_projection(objects)
    path.parent.mkdir(parents=True, exist_ok=True)
    if safe_output_path(path, root=root) != path:
        raise ValueError('Preview output parent changed during setup')
    temporary_scene = world = camera_data = sun_data = camera = sun = None
    original_scene = ctx.scene
    original_window = getattr(ctx, 'window', None)
    try:
        temporary_scene = bpy.data.scenes.new('WZ09_ORTHOGRAPHIC_PREVIEW_TEMP')
        temporary_scene.frame_set(ctx.scene.frame_current)
        for obj in objects:
            temporary_scene.collection.objects.link(obj)
        world = bpy.data.worlds.new('WZ09_PREVIEW_WORLD_TEMP')
        world.use_nodes = True
        world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.32, 0.38, 0.42, 1)
        world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.7
        temporary_scene.world = world
        camera_data = bpy.data.cameras.new('WZ09_PREVIEW_CAMERA_TEMP')
        camera_data.type = 'ORTHO'
        camera_data.sensor_fit = 'HORIZONTAL'
        camera_data.ortho_scale = actual[2]-actual[0]
        camera_data.clip_start = projection['clip_start_m']
        camera_data.clip_end = projection['clip_end_m']
        camera = bpy.data.objects.new('WZ09_PREVIEW_CAMERA_TEMP', camera_data)
        temporary_scene.collection.objects.link(camera)
        camera.location = ((actual[0]+actual[2])/2, (actual[1]+actual[3])/2, projection['camera_z_m'])
        camera.rotation_euler = (0, 0, 0)
        temporary_scene.camera = camera
        sun_data = bpy.data.lights.new('WZ09_PREVIEW_SUN_TEMP', 'SUN')
        sun_data.energy = 2.0
        sun_data.angle = 0.15
        sun = bpy.data.objects.new('WZ09_PREVIEW_SUN_TEMP', sun_data)
        sun.rotation_euler = (0.35, -0.25, 0.2)
        temporary_scene.collection.objects.link(sun)
        temporary_scene.render.engine = 'CYCLES'
        temporary_scene.render.threads_mode = 'FIXED'
        temporary_scene.render.threads = min(4, os.cpu_count() or 1)
        temporary_scene.cycles.device = 'CPU'
        temporary_scene.cycles.samples = 8
        temporary_scene.cycles.seed = 0
        temporary_scene.cycles.use_denoising = False
        temporary_scene.cycles.max_bounces = 2
        temporary_scene.cycles.diffuse_bounces = 1
        temporary_scene.cycles.glossy_bounces = 1
        temporary_scene.cycles.transmission_bounces = 0
        temporary_scene.cycles.volume_bounces = 0
        temporary_scene.render.film_transparent = False
        temporary_scene.render.resolution_x = width
        temporary_scene.render.resolution_y = height
        temporary_scene.render.resolution_percentage = 100
        temporary_scene.render.pixel_aspect_x = 1
        temporary_scene.render.pixel_aspect_y = 1
        frame = camera_data.view_frame(scene=temporary_scene)
        frame_width = max(point.x for point in frame)-min(point.x for point in frame)
        frame_height = max(point.y for point in frame)-min(point.y for point in frame)
        if not (math.isclose(frame_width, actual[2]-actual[0], rel_tol=1e-6, abs_tol=1e-6)
                and math.isclose(frame_height, actual[3]-actual[1], rel_tol=1e-6, abs_tol=1e-6)):
            raise ValueError('Blender orthographic camera frame disagrees with preview bounds')
        temporary_scene.render.image_settings.file_format = 'PNG'
        temporary_scene.render.image_settings.color_mode = 'RGBA'
        temporary_scene.render.image_settings.color_depth = '8'
        temporary_scene.render.use_file_extension = True
        with tempfile.TemporaryDirectory(prefix='.wz09-preview-', dir=path.parent) as folder:
            staged = Path(folder)/'projection.png'
            temporary_scene.render.filepath = str(staged)
            result = bpy.ops.render.render(write_still=True, scene=temporary_scene.name)
            if result != {'FINISHED'} or not staged.is_file():
                raise ValueError('Blender CPU Cycles preview render did not finish')
            payload = staged.read_bytes()
            if len(payload) < 24 or payload[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('Blender preview render produced invalid PNG data')
            if struct.unpack('>2I', payload[16:24]) != (width, height):
                raise ValueError('Blender preview dimensions differ from projection metadata')
            _write_new(path, payload)
        return {'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(payload).hexdigest(),
                'width_px': width, 'height_px': height, 'bounds_m': actual,
                'kind': 'orthographic_preview', 'physical_scale_verified': False,
                'source_fingerprint': source_fingerprint, 'projection': projection}
    finally:
        if original_window is not None and original_window.scene is temporary_scene:
            original_window.scene = original_scene
        for obj in (camera, sun):
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
        if temporary_scene is not None:
            bpy.data.scenes.remove(temporary_scene, do_unlink=True)
        if camera_data is not None:
            bpy.data.cameras.remove(camera_data)
        if sun_data is not None:
            bpy.data.lights.remove(sun_data)
        if world is not None:
            bpy.data.worlds.remove(world)

# Shared native mesh selector policy; exact implementation moved from the landscape builder.

def _mesh_layer_selection(mesh,*,require_render=False):
    """Read selector names; indices do not survive subset/join layer ordering."""
    colors,uvs=mesh.color_attributes,mesh.uv_layers
    active=colors.active_color;render=colors.render_color_index
    uv_render=[layer.name for layer in uvs if layer.active_render]
    if len(uv_render)>1:raise ValueError('Native mesh has ambiguous render UV selection')
    selection=dict(color_active=active.name if active else None,
        color_render=colors[render].name if 0<=render<len(colors) else None,
        uv_active=uvs.active.name if uvs.active else None,uv_render=uv_render[0] if uv_render else None)
    if require_render and selection['color_render'] is None and any(
            node.bl_idname=='ShaderNodeVertexColor' and not node.mute and not node.layer_name
            and any(socket.is_linked for socket in node.outputs)
            for material in mesh.materials if material and material.use_nodes and material.node_tree
            for node in material.node_tree.nodes):
        raise ValueError('Original native source lacks render color selection for linked unnamed VertexColor: '+json.dumps(selection,sort_keys=True))
    return selection


def _apply_mesh_layer_selection(mesh,selection):
    """Apply existing authored selectors without inventing an attribute name."""
    colors={layer.name:i for i,layer in enumerate(mesh.color_attributes)}
    uvs={layer.name:i for i,layer in enumerate(mesh.uv_layers)}
    for key in ('color_active','color_render','uv_active','uv_render'):
        name=selection[key]
        if name is not None and name not in (colors if key.startswith('color_') else uvs):
            raise ValueError('Copied native mesh lost selected layer: '+key+'='+name)
    if colors:
        if selection['color_active'] is not None:mesh.color_attributes.active_color_index=colors[selection['color_active']]
        if selection['color_render'] is not None:mesh.color_attributes.render_color_index=colors[selection['color_render']]
    if selection['uv_active'] is not None:mesh.uv_layers.active_index=uvs[selection['uv_active']]
    if selection['uv_render'] is not None:
        for layer in mesh.uv_layers:layer.active_render=layer.name==selection['uv_render']
    actual=_mesh_layer_selection(mesh)
    if any(name is not None and actual[key]!=name for key,name in selection.items()):raise ValueError('Native named layer selection readback differs')


def _derived_mesh_layer_selection(mesh):
    """Resolve private preview lookup only; never recover/write author choices."""
    raw=_mesh_layer_selection(mesh);resolved=dict(raw)
    basis='named_selections_preserved_none_is_no_authored_preference'
    needs_color=any(node.bl_idname=='ShaderNodeVertexColor' and not node.mute and not node.layer_name
        and any(socket.is_linked for socket in node.outputs)
        for material in mesh.materials if material and material.use_nodes and material.node_tree
        for node in material.node_tree.nodes)
    if raw['color_render'] is None and needs_color:
        supported=[layer for layer in mesh.color_attributes if layer.domain in ('POINT','CORNER') and layer.data_type in ('FLOAT_COLOR','BYTE_COLOR')]
        if len(supported)!=1 or len(mesh.color_attributes)!=1:
            raise ValueError('Missing unnamed VertexColor render selection is ambiguous or unsupported; exactly one supported color layer required')
        resolved['color_render']=supported[0].name
        if resolved['color_active'] is None:resolved['color_active']=supported[0].name
        basis='single_COLOR_0_material_modulation'
    return dict(source_raw_selection=raw,resolved_selection=resolved,basis=basis,
        private_derived_preview_only=True,original_author_selection_recovered=False)
