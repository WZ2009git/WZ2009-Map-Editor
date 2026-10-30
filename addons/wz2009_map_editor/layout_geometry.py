"""Design-only layout and editable connector helpers; never native collision.

All dimensions here are explicit editor design metres, not IW4 source scales.
The pure plans have no Blender dependency. Blender helpers create new data only.
"""
import json
import math
import uuid


REGIONS = (
    ('North Ridge', ('mp_derail', 'mp_estate', 'mp_subbase', 'mp_brecourt'), (0, 2)),
    ('West Transit', ('mp_terminal', 'mp_boneyard'), (-2, 0)),
    ('Central City', ('mp_highrise', 'mp_nightshift', 'mp_invasion', 'mp_checkpoint'), (0, 0)),
    ('East Badlands', ('mp_quarry', 'mp_rust', 'mp_afghan'), (2, 0)),
    ('South Valley', ('mp_rundown', 'mp_favela', 'mp_underpass'), (0, -2)),
)
GEOMETRY_KINDS = ('road', 'bridge', 'corridor', 'tunnel', 'building')


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name}: positive finite design metres required')
    return float(value)


def _point(value, name='position'):
    if len(value) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise ValueError(f'{name}: three finite coordinates required')
    return tuple(float(x) for x in value)


def design_layout(spacing_m, region_gap_m, origin=(0, 0, 0)):
    """Return sixteen unmeasured design proxies; never import native geometry."""
    spacing = _positive(spacing_m, 'spacing_m')
    gap = _positive(region_gap_m, 'region_gap_m')
    if gap < spacing:
        raise ValueError('region_gap_m must be at least spacing_m')
    ox, oy, oz = _point(origin, 'origin')
    records = []
    for region, maps, center in REGIONS:
        for index, source in enumerate(maps):
            x = ox + center[0] * gap + ((index % 2) - .5) * spacing
            y = oy + center[1] * gap + (.5 - (index // 2)) * spacing
            records.append({'source_map': source, 'region': region, 'position_m': [x, y, oz],
                'provenance': {'region': region, 'design_hypothesis': True,
                    'layout_source': 'docs/WORLD_LAYOUT.md', 'layout_units': 'editor_design_meters',
                    'layout_spacing_m': spacing, 'layout_region_gap_m': gap,
                    'physical_scale_verified': False}})
    return records


def geometry_plan(kind, length_m, width_m, height_m, thickness_m=.25):
    """Closed visual box components, UVs and explicit placeholder metadata.

    Local X follows the connector, Y is width, Z is up. The deck top is Z=0.
    Separate overlapping components are deliberate editable placeholders, not
    a watertight combined collision mesh or proof of a traversable passage.
    """
    if kind not in GEOMETRY_KINDS:
        raise ValueError('Unsupported custom geometry kind')
    length = _positive(length_m, 'length_m')
    width = _positive(width_m, 'width_m')
    height = _positive(height_m, 'height_m')
    thick = _positive(thickness_m, 'thickness_m')
    if thick * 2 >= min(length, width) or thick >= height:
        raise ValueError('Thickness must leave positive interior width, length and height')
    vertices, faces, uvs = [], [], []

    def box(x0, x1, y0, y1, z0, z1):
        base = len(vertices)
        vertices.extend([(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                         (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)])
        for face in ((0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)):
            faces.append(tuple(base + i for i in face))
            uvs.append(((0,0),(1,0),(1,1),(0,1)))

    x, y = length / 2, width / 2
    box(-x, x, -y, y, -thick, 0)
    if kind in ('corridor', 'tunnel', 'building'):
        box(-x, x, -y, -y+thick, 0, height)
        box(-x, x, y-thick, y, 0, height)
    if kind in ('tunnel', 'building'):
        box(-x, x, -y, y, height, height+thick)
    if kind == 'building':
        box(-x, -x+thick, -y+thick, y-thick, 0, height)
        box(x-thick, x, -y+thick, y-thick, 0, height)
    if kind == 'bridge':
        rail = min(height, 1.2)
        box(-x, x, -y, -y+thick, 0, rail)
        box(-x, x, y-thick, y, 0, rail)
        for sx in (-x*.75, x*.75):
            box(sx-thick/2, sx+thick/2, -y, y, -height, -thick)
    return {'kind': kind, 'vertices': vertices, 'faces': faces, 'face_uvs': uvs,
            'dimensions_m': [length, width, height], 'thickness_m': thick,
            'status': 'visual_placeholder', 'collision_validated': False,
            'navigation_validated': False}


def _api(value):
    if value is not None:
        return value
    import bpy
    return bpy


def _same(a, b):
    return a is b or (hasattr(a, 'as_pointer') and hasattr(b, 'as_pointer') and a.as_pointer() == b.as_pointer())


def _collection(scene, api, name):
    if not scene.get('wz09_scene_id'):
        scene['wz09_scene_id'] = str(uuid.uuid4())
    scene_id = str(scene['wz09_scene_id'])
    for collection in scene.collection.children:
        if collection.get('wz09_scene_id') == scene_id and collection.get('wz09_collection_kind') == name:
            return collection
    collection = api.data.collections.new(name)
    collection['wz09_scene_id'] = scene_id
    collection['wz09_collection_kind'] = name
    scene.collection.children.link(collection)
    return collection


def create_design_scene(ctx, spacing_m, region_gap_m, *, origin=(0,0,0), bpy_module=None):
    """Create a separate proxy scene; the caller may switch the window to it."""
    plan = design_layout(spacing_m, region_gap_m, origin)
    api = _api(bpy_module)
    scene = api.data.scenes.new('KARVORSK Design Hypothesis')
    created = []
    try:
        scene.unit_settings.system = 'METRIC'
        scene.unit_settings.scale_length = 1.0
        scene['wz09_design_hypothesis'] = True
        collection = _collection(scene, api, 'WZ09_MAPS')
        for item in plan:
            obj = api.data.objects.new(item['source_map'], None)
            created.append(obj)
            obj.empty_display_type = 'CUBE'
            obj.empty_display_size = float(spacing_m) * .15
            obj.location = item['position_m']
            obj['wz09_type'] = 'map_instance'
            obj['wz09_id'] = str(uuid.uuid4())
            obj['wz09_source_map'] = item['source_map']
            obj['wz09_source_scale'] = 0.0
            obj['wz09_map_provenance_json'] = json.dumps(item['provenance'], sort_keys=True)
            collection.objects.link(obj)
        return scene
    except Exception:
        for obj in reversed(created):
            api.data.objects.remove(obj, do_unlink=True)
        api.data.scenes.remove(scene)
        raise


def _used_ids(scene):
    used = {str(scene['wz09_geometry_id'])} if scene.get('wz09_geometry_id') else set()
    for obj in scene.objects:
        for field in ('wz09_id', 'wz09_geometry_id', 'wz09_preview_id'):
            if obj.get(field):
                used.add(str(obj[field]))
    for edge in json.loads(scene.get('wz09_edges_json', '[]')):
        used.add(str(edge['id']))
    return used


def _fresh(used):
    value = str(uuid.uuid4())
    while value in used:
        value = str(uuid.uuid4())
    used.add(value)
    return value


def _owner(obj):
    if obj.get('wz09_type') == 'map_instance':
        return str(obj.get('wz09_id', ''))
    return str(obj.get('wz09_owner_map', ''))


def connector_placement(ctx, length_m, between_selected=False):
    """Return global midpoint/vector and optional map endpoints, without edits."""
    length = _positive(length_m, 'length_m')
    if not between_selected:
        return {'position_m': _point(ctx.scene.cursor.location), 'direction': (1,0,0),
                'length_m': length, 'from_map': '', 'to_map': ''}
    selected = list(ctx.selected_objects)
    if len(selected) != 2 or any(o.get('wz09_type') not in ('map_instance', 'nav_node') for o in selected):
        raise ValueError('Select exactly two current-scene map instances or nav/portal nodes')
    if any(not any(_same(o, item) for item in ctx.scene.objects) for o in selected):
        raise ValueError('Connector endpoints must belong to the current scene')
    selected.sort(key=lambda o: not _same(o, ctx.active_object))
    a, b = (_point(o.matrix_world.translation) for o in selected)
    direction = tuple(b[i] - a[i] for i in range(3))
    if math.hypot(direction[0], direction[1]) < .01:
        raise ValueError('Connector endpoints need nonzero horizontal separation')
    start, end = (_owner(o) for o in selected)
    if start and end and start == end:
        start = end = ''
    if bool(start) != bool(end):
        start = end = ''  # A connector manifest needs two actual map owners.
    return {'position_m': tuple((a[i]+b[i])/2 for i in range(3)), 'direction': direction,
            'length_m': math.sqrt(sum(v*v for v in direction)), 'from_map': start, 'to_map': end}


def create_connector(ctx, kind, *, length_m=12, width_m=6, height_m=3, thickness_m=.25,
                     between_selected=False, bpy_module=None):
    """Create editable, UV-mapped custom mesh for the existing GLB pipeline."""
    placement = connector_placement(ctx, length_m, between_selected)
    plan = geometry_plan(kind, placement['length_m'], width_m, height_m, thickness_m)
    used = _used_ids(ctx.scene)
    api = _api(bpy_module)
    mesh = api.data.meshes.new('WZ09_' + kind)
    obj = None
    try:
        mesh.from_pydata(plan['vertices'], [], plan['faces'])
        mesh.update()
        uv = mesh.uv_layers.new(name='UVMap')
        for polygon, face_uv in zip(mesh.polygons, plan['face_uvs']):
            for loop, point in zip(polygon.loop_indices, face_uv):
                uv.data[loop].uv = point
        obj = api.data.objects.new('WZ09_' + kind, mesh)
        _collection(ctx.scene, api, 'WZ09_GEOMETRY').objects.link(obj)
        obj.location = placement['position_m']
        if between_selected:
            from mathutils import Vector
            obj.rotation_mode = 'QUATERNION'
            obj.rotation_quaternion = Vector(placement['direction']).to_track_quat('X', 'Z')
        obj['wz09_custom_geometry'] = True
        obj['wz09_geometry_id'] = _fresh(used)
        obj['wz09_geometry_kind'] = kind
        obj['wz09_geometry_status'] = 'visual_placeholder'
        obj['wz09_connector_from'] = placement['from_map']
        obj['wz09_connector_to'] = placement['to_map']
        return obj
    except Exception:
        if obj is not None:
            api.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            api.data.meshes.remove(mesh)
        raise


def eligible_custom_objects(ctx, *, bpy_module=None):
    """Validate the complete selection before assigning flags or linking data."""
    api = _api(bpy_module)
    selected = list(ctx.selected_objects)
    if not selected:
        raise ValueError('Select local mesh/curve objects to mark as custom geometry')
    previews = [o for o in api.data.objects if o.get('wz09_preview_only') or o.get('wz09_preview_id')]
    used = _used_ids(ctx.scene)
    for obj in selected:
        if obj.get('wz09_editor_overlay'):
            raise ValueError('Editor overlays cannot become custom export geometry')
        if obj.type not in ('MESH', 'CURVE') or obj.get('wz09_type'):
            raise ValueError('Only untyped local mesh/curve objects can become custom geometry')
        if (getattr(obj, 'library', None) or getattr(obj, 'override_library', None)
                or getattr(obj.data, 'library', None) or getattr(obj.data, 'override_library', None)):
            raise ValueError('Linked or overridden source data cannot become custom geometry')
        owners = list(getattr(obj, 'users_scene', ()))
        if len(owners) != 1 or not _same(owners[0], ctx.scene):
            raise ValueError('Custom geometry must be owned exclusively by the current scene')
        parent = obj
        seen = set()
        while parent is not None:
            key = parent.as_pointer() if hasattr(parent, 'as_pointer') else id(parent)
            if key in seen:
                raise ValueError('Object parent cycle must be repaired before marking geometry')
            seen.add(key)
            if parent.get('wz09_preview_only') or parent.get('wz09_preview_id'):
                raise ValueError('Native preview objects or their descendants cannot become custom geometry')
            parent = getattr(parent, 'parent', None)
        if any(_same(obj.data, o.data) for o in previews if getattr(o, 'data', None) is not None):
            raise ValueError('Mesh/curve data shared with native previews cannot become custom geometry')
        if getattr(obj.data, 'users', 1) != 1:
            raise ValueError('Make shared mesh/curve data single-user before marking custom geometry')
        uid = obj.get('wz09_geometry_id')
        if uid:
            others = [o for o in ctx.scene.objects if not _same(o, obj)]
            if not isinstance(uid, str) or not uid.strip() or any(
                    uid == o.get(k) for o in others for k in ('wz09_id','wz09_geometry_id','wz09_preview_id')):
                raise ValueError('Existing custom geometry ID is invalid or duplicated')
            edge_ids = {str(e['id']) for e in json.loads(ctx.scene.get('wz09_edges_json', '[]'))}
            if uid == ctx.scene.get('wz09_geometry_id') or uid in edge_ids:
                raise ValueError('Existing custom geometry ID collides with another authoring ID')
    return selected


def mark_selected_custom_geometry(ctx, *, kind='building', bpy_module=None):
    """Mark local user geometry, preserving existing IDs and mesh data."""
    if kind not in GEOMETRY_KINDS:
        raise ValueError('Unsupported custom geometry kind')
    objects = eligible_custom_objects(ctx, bpy_module=bpy_module)
    used = _used_ids(ctx.scene)
    collection = _collection(ctx.scene, _api(bpy_module), 'WZ09_GEOMETRY')
    fields = ('wz09_geometry_id','wz09_custom_geometry','wz09_geometry_kind','wz09_geometry_status')
    originals = [(obj, {key: obj[key] for key in fields if key in obj}) for obj in objects]
    added = []
    try:
        for obj in objects:
            if not any(_same(c, collection) for c in obj.users_collection):
                collection.objects.link(obj)
                added.append(obj)
            if not obj.get('wz09_geometry_id'):
                obj['wz09_geometry_id'] = _fresh(used)
            obj['wz09_custom_geometry'] = True
            obj['wz09_geometry_kind'] = kind
            obj['wz09_geometry_status'] = 'authored_visual_geometry'
    except Exception:
        for obj in reversed(added):
            collection.objects.unlink(obj)
        for obj, previous in originals:
            for key in fields:
                if key in previous:
                    obj[key] = previous[key]
                elif key in obj:
                    del obj[key]
        raise
    return objects
