"""Spatial audio authoring and deterministic listener preview, without playback.

Native aliases remain opaque references. Source-bound means a checksum-bound
source record, never that an alias resolves or that game audio was tested.
Blender is imported only by authoring/register functions for offline use.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

KINDS = {'music', 'ambient', 'radio'}
SHAPES = {'box', 'cylinder', 'sphere'}
EVIDENCE_STATUSES = {'unverified', 'source_bound'}
_CLASSES = []


def _number(value, field, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{field}: finite number required')
    result = float(value)
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f'{field}: outside allowed range')
    return result


def _vector(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{field}: exactly three coordinates required')
    return [_number(v, field) for v in value]


def _text(value, field, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > 4096:
        raise ValueError(f'{field}: valid text required')
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def unverified_evidence():
    return {'status': 'unverified', 'source_path': '', 'source_sha256': '', 'source_record': ''}


def validate_evidence(evidence, *, verify_sources=False, cache=None):
    if not isinstance(evidence, dict) or set(evidence) != {'status', 'source_path', 'source_sha256', 'source_record'}:
        raise ValueError('Evidence requires status/source_path/source_sha256/source_record')
    result = copy.deepcopy(evidence)
    if result['status'] not in EVIDENCE_STATUSES:
        raise ValueError('Evidence status must remain unverified or source_bound')
    for field in ('source_path', 'source_sha256', 'source_record'):
        _text(result[field], 'evidence.'+field, empty=result['status'] == 'unverified')
    digest = result['source_sha256']
    if digest and not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Evidence source_sha256 must be lowercase SHA-256')
    if result['status'] == 'source_bound' and not Path(result['source_path']).is_absolute():
        raise ValueError('Source-bound evidence requires an absolute source path')
    if result['status'] == 'source_bound' and verify_sources:
        path = Path(result['source_path'])
        key = str(path.resolve())
        cache = cache if cache is not None else {}
        if key not in cache:
            if not path.is_file():
                raise ValueError('Evidence source is missing: '+str(path))
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    h.update(chunk)
            cache[key] = h.hexdigest()
        if cache[key] != digest:
            raise ValueError('Evidence source SHA-256 changed: '+str(path))
    return result


def new_record(kind='music', shape='box', *, position=(0, 0, 0), map_instance_id=None, alias_ref=''):
    if kind == 'radio':
        shape = 'sphere'
    return {'id': str(uuid.uuid4()), 'map_instance_id': map_instance_id, 'asset_placement_id':None,
            'kind': kind, 'shape': shape, 'position_m': list(position), 'rotation_deg': [0, 0, 0],
            'dimensions_m': [40.0, 40.0, 12.0], 'radius_m': 20.0,
            'inner_radius_m': 3.0, 'height_m': 12.0, 'alias_ref': alias_ref,
            'priority': 0, 'volume': 1.0, 'fade_m': 3.0, 'fade_in_s': 1.0,
            'fade_out_s': 1.0, 'enabled': True, 'evidence': unverified_evidence(),
            'runtime_integrated': False}


def validate_records(records, map_ids=None, all_ids=None, *, verify_sources=False):
    if not isinstance(records, list):
        raise ValueError('audio_zones must be a list')
    seen, cache, result = set(all_ids or ()), {}, []
    for value in records:
        if not isinstance(value, dict):
            raise ValueError('Audio record must be an object')
        r = copy.deepcopy(value)
        r.setdefault('asset_placement_id',None)
        required = set(new_record())
        if set(r) != required:
            raise ValueError('Audio record fields differ: '+', '.join(sorted(set(r)^required)))
        uid = _text(r['id'], 'Audio ID')
        if uid in seen:
            raise ValueError('Duplicate audio ID: '+uid)
        seen.add(uid)
        owner = r['map_instance_id']
        if r['asset_placement_id'] is not None:
            _text(r['asset_placement_id'],'Audio asset placement ID')
            if owner is None:raise ValueError('Asset-bound audio requires a map owner')
        if owner is not None:
            _text(owner, 'Audio map ID')
            if map_ids is not None and owner not in map_ids:
                raise ValueError('Audio owner map is missing: '+owner)
        if r['kind'] not in KINDS or r['shape'] not in SHAPES:
            raise ValueError('Unsupported audio kind or shape')
        if r['kind'] == 'radio' and r['shape'] != 'sphere':
            raise ValueError('Radios require a sphere falloff')
        r['position_m'] = _vector(r['position_m'], 'Audio position')
        r['rotation_deg'] = _vector(r['rotation_deg'], 'Audio rotation')
        r['dimensions_m'] = _vector(r['dimensions_m'], 'Audio dimensions')
        if any(v <= 0 for v in r['dimensions_m']):
            raise ValueError('Audio dimensions must be positive')
        for field in ('radius_m', 'height_m'):
            r[field] = _number(r[field], field, .000001)
        for field in ('inner_radius_m', 'fade_m', 'fade_in_s', 'fade_out_s'):
            r[field] = _number(r[field], field, 0)
        if r['inner_radius_m'] > r['radius_m']:
            raise ValueError('Inner audio radius exceeds outer radius')
        if type(r['priority']) is not int or not -10000 <= r['priority'] <= 10000:
            raise ValueError('Audio priority must be an integer between -10000 and 10000')
        r['volume'] = _number(r['volume'], 'Audio volume', 0, 1)
        _text(r['alias_ref'], 'Alias reference', empty=True)
        if type(r['enabled']) is not bool or r['runtime_integrated'] is not False:
            raise ValueError('Audio enabled must be boolean; runtime_integrated must remain false')
        r['evidence'] = validate_evidence(r['evidence'], verify_sources=verify_sources, cache=cache)
        result.append(r)
    return result


def _local_point(point, record):
    """Inverse XYZ Euler transform; source units never appear in zone geometry."""
    p = [a-b for a, b in zip(_vector(point, 'Listener'), record['position_m'])]
    x, y, z = [math.radians(v) for v in record['rotation_deg']]
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    matrix = ((cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx),
              (sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx), (-sy, cy*sx, cy*cx))
    return [sum(matrix[row][column]*p[row] for row in range(3)) for column in range(3)]


def signed_distance(record, listener):
    """Negative inside, zero on boundary. Includes vertical extent and rotation."""
    p = _local_point(listener, record)
    if record['shape'] == 'sphere':
        return math.sqrt(sum(v*v for v in p))-record['radius_m']
    if record['shape'] == 'cylinder':
        d = [math.hypot(p[0], p[1])-record['radius_m'], abs(p[2])-record['height_m']/2]
    else:
        d = [abs(v)-size/2 for v, size in zip(p, record['dimensions_m'])]
    return math.sqrt(sum(max(v, 0)**2 for v in d))+min(max(d), 0)


def zone_gain(record, listener):
    """Spatial-only gain; temporal fades are exported, not simulated as playback."""
    if not record['enabled']:
        return 0.0
    distance = signed_distance(record, listener)
    if distance >= 0:
        return 0.0
    if record['kind'] == 'radio':
        radial = record['radius_m']+distance
        span = record['radius_m']-record['inner_radius_m']
        weight = 1.0 if radial <= record['inner_radius_m'] or span <= 1e-9 else (record['radius_m']-radial)/span
    else:
        weight = 1.0 if record['fade_m'] <= 1e-9 else min(1.0, -distance/record['fade_m'])
    weight = max(0.0, min(weight, 1.0))
    return weight*weight*(3-2*weight)*record['volume']


def listener_mix(records, listener):
    """Highest music priority wins, same-priority music blends; radios add.

    A lower-priority zone fills the remainder as a higher zone fades in. Ambient
    zones blend within their own bus. Gain is a diagnostic, no sound is emitted.
    """
    records = validate_records(records)
    candidates = [(r, zone_gain(r, listener)) for r in records if r['enabled'] and r['alias_ref']]
    output = []
    for kind in ('music', 'ambient'):
        levels = sorted({r['priority'] for r, g in candidates if r['kind'] == kind and g > 0}, reverse=True)
        remaining = 1.0
        for priority in levels:
            group = [(r, g) for r, g in candidates if r['kind'] == kind and r['priority'] == priority and g > 0]
            total = sum(g for _, g in group)
            contribution = min(1.0, total)*remaining
            for r, gain in group:
                output.append({'id': r['id'], 'alias_ref': r['alias_ref'], 'kind': kind,
                               'gain': contribution*gain/total, 'priority': priority})
            remaining *= 1-min(1.0, total)
    for r, gain in candidates:
        if r['kind'] == 'radio' and gain > 0:
            output.append({'id': r['id'], 'alias_ref': r['alias_ref'], 'kind': 'radio',
                           'gain': gain, 'priority': r['priority']})
    return sorted(output, key=lambda r: (r['kind'], -r['priority'], r['id']))


def contour_geometry(record, segments=64):
    """Local wire vertices/edges: outer+inner footprints and full height."""
    r = validate_records([record])[0]
    vertices, edges = [], []
    def loop(points):
        start = len(vertices)
        vertices.extend(points)
        edges.extend((start+i, start+(i+1)%len(points)) for i in range(len(points)))
        return start
    if r['shape'] == 'box':
        hx, hy, hz = [v/2 for v in r['dimensions_m']]
        a = loop([(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz)])
        b = loop([(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)])
        edges.extend((a+i, b+i) for i in range(4))
        f = r['fade_m']
        if f > 0 and hx > f and hy > f:
            loop([(-hx+f, -hy+f, 0), (hx-f, -hy+f, 0), (hx-f, hy-f, 0), (-hx+f, hy-f, 0)])
    else:
        if type(segments) is not int or not 8 <= segments <= 512:
            raise ValueError('Circle segments must be between 8 and 512')
        def circle(radius, z=0, plane='XY'):
            points = []
            for i in range(segments):
                a = 2*math.pi*i/segments
                x, y = radius*math.cos(a), radius*math.sin(a)
                points.append((x, y, z) if plane == 'XY' else ((x, z, y) if plane == 'XZ' else (z, x, y)))
            return loop(points)
        radius = r['radius_m']
        circle(radius)
        if r['shape'] == 'sphere':
            circle(radius, plane='XZ'); circle(radius, plane='YZ')
            if r['inner_radius_m'] > 0:
                circle(r['inner_radius_m'])
        else:
            a = circle(radius, -r['height_m']/2)
            b = circle(radius, r['height_m']/2)
            edges.extend((a+i, b+i) for i in range(0, segments, max(1, segments//8)))
            if 0 < r['fade_m'] < radius:
                circle(radius-r['fade_m'])
    return vertices, edges


def records_from_createfx(source, source_path, source_sha256, source_units_to_meter, *,
                          axis_mapping=None, offset_m=None, map_instance_id=None):
    """Read a narrow plaintext CreateFX loop format; never infer music/radio.

    Conversion requires an explicit supplied scale. Quoted aliases and origins
    are captured only in createLoopSound blocks. Unsupported/malformed loops
    reject the import instead of inventing positions or accepting partial data.
    """
    scale = _number(source_units_to_meter, 'Explicit source scale', 1e-12)
    if (not isinstance(axis_mapping, (list, tuple)) or len(axis_mapping) != 3
            or any(not isinstance(v, str) or not re.fullmatch('[+-]?[xyz]', v) for v in axis_mapping)
            or {v[-1] for v in axis_mapping} != set('xyz')):
        raise ValueError('Explicit signed source axis mapping required, e.g. x,y,z')
    offset = _vector(offset_m, 'Explicit source offset')
    if not isinstance(source, str):
        raise ValueError('CreateFX source must be UTF-8 text')
    evidence = {'status': 'source_bound', 'source_path': str(Path(source_path).resolve()),
                'source_sha256': source_sha256, 'source_record': 'createfx'}
    validate_evidence(evidence, verify_sources=True)
    if hashlib.sha256(source.encode('utf-8')).hexdigest() != source_sha256:
        raise ValueError('CreateFX text differs from checksum-bound source bytes')
    # Strip comments lexically, preserving quoted strings and newline positions.
    # Comment text must never create phantom emitters or override source fields.
    chars, cursor, quoted = list(source), 0, False
    while cursor < len(source):
        if source[cursor] == '"':
            quoted = not quoted; cursor += 1; continue
        if quoted:
            cursor += 2 if source[cursor] == '\\' else 1
            continue
        if source.startswith('//', cursor):
            end = source.find('\n', cursor)
            end = len(source) if end < 0 else end
        elif source.startswith('/*', cursor):
            end = source.find('*/', cursor+2)
            if end < 0: raise ValueError('Unterminated CreateFX comment')
            end += 2
        else:
            cursor += 1; continue
        for i in range(cursor, end):
            if chars[i] not in '\r\n': chars[i] = ' '
        cursor = end
    parsed_source = ''.join(chars)
    starts = list(re.finditer(r'(?im)^\s*[^\n]*\bcreate(?:LoopSound|OneshotEffect|Exploder|IntervalSound)\b[^\n]*$', parsed_source))
    records = []
    for index, start in enumerate(starts):
        first = start.group(0)
        if not re.search(r'\bcreateLoopSound\b', first):
            continue
        block = parsed_source[start.end():starts[index+1].start() if index+1 < len(starts) else len(parsed_source)]
        origins = re.findall(r'\[\s*["\']origin["\']\s*\]\s*=\s*\(([^)]+)\)', block)
        aliases = re.findall(r'\[\s*["\']soundalias["\']\s*\]\s*=\s*"([^"\r\n]+)"', block)
        if len(origins) != 1 or len(aliases) != 1:
            raise ValueError('CreateFX loop requires exactly one origin and alias')
        try:
            coordinates = [float(v.strip()) for v in origins[0].split(',')]
        except ValueError as exc:
            raise ValueError('Unsupported CreateFX origin') from exc
        coordinates = _vector(coordinates, 'CreateFX origin')
        position = [coordinates['xyz'.index(axis[-1])]*(-1 if axis.startswith('-') else 1)*scale+offset[i]
                    for i, axis in enumerate(axis_mapping)]
        r = new_record('ambient', 'sphere', position=position, map_instance_id=map_instance_id, alias_ref=aliases[0])
        # Stable identity binds content hash and lexical loop ordinal, not label.
        r['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'wz09:createfx:{source_sha256}:{index}:{map_instance_id}'))
        transform = {'axis_mapping': list(axis_mapping), 'offset_m': offset, 'source_units_to_editor_unit': scale}
        r['evidence'] = dict(evidence, source_record=f'createLoopSound:{index}; transform='+_encoded(transform))
        records.append(r)
    if not records:
        raise ValueError('No supported CreateFX loops found; no fallback positions created')
    return validate_records(records, verify_sources=True)


def _object_record(obj, key):
    raw = obj.get(key, '')
    try:
        record = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid authoring record on '+obj.name) from exc
    if not isinstance(record,dict):
        raise ValueError('Invalid authoring record on '+obj.name+': object required')
    matrix = obj.matrix_world
    scale = [float(v) for v in matrix.to_scale()]
    if any(not math.isfinite(v) or v <= 1e-9 for v in scale) or matrix.determinant() <= 0:
        raise ValueError('Audio/action transforms require positive nonzero scale')
    normalized = matrix.to_3x3().normalized()
    if any(abs(normalized.col[i].dot(normalized.col[j])) > 1e-5 for i in range(3) for j in range(i)):
        raise ValueError('Audio/action transforms must not contain shear')
    record['position_m'] = [float(v) for v in matrix.translation]
    record['rotation_deg'] = [math.degrees(v) for v in matrix.to_euler('XYZ')]
    return record, scale


def scale_audio_record(record, scale):
    """Encode object scale without changing the visible boundary/fade meaning.

    A scalar physical fade cannot represent unequal axis scales. Use the
    explicit dimensions/radius/height controls for those edits instead.
    """
    r = validate_records([record])[0]
    factors = _vector(scale, 'Audio scale')
    if min(factors) <= 0:
        raise ValueError('Audio scale must be positive')
    uniform = max(factors)-min(factors) <= 1e-5
    if r['fade_m'] > 0 and not uniform:
        raise ValueError('Audio boundary fade needs uniform scale; edit width/depth/height or radius in Audio settings')
    if r['shape'] in {'sphere', 'cylinder'}:
        if abs(factors[0]-factors[1]) > 1e-5 or r['shape'] == 'sphere' and not uniform:
            raise ValueError('Circular audio zones need uniform radial scale; edit radius/height in Audio settings')
        r['radius_m'] *= factors[0]
        r['inner_radius_m'] *= factors[0]
    r['dimensions_m'] = [a*b for a, b in zip(r['dimensions_m'], factors)]
    r['height_m'] *= factors[2]
    if uniform:
        r['fade_m'] *= factors[0]
    return validate_records([r])[0]


def serialize_scene(scene):
    records = []
    maps = {str(o.get('wz09_id')) for o in scene.objects if o.get('wz09_type') == 'map_instance'}
    for obj in scene.objects:
        if not obj.get('wz09_audio_zone'):
            continue
        r, scale = _object_record(obj, 'wz09_audio_json')
        if r['id'] != obj.get('wz09_audio_id'):
            raise ValueError('Audio stable ID differs from stored record')
        try:
            from .authoring_helpers import resolve_owner_map
        except ImportError:
            from authoring_helpers import resolve_owner_map
        owner = resolve_owner_map(obj)
        r['map_instance_id'] = str(owner.get('wz09_id')) if owner is not None else None
        parent=obj.parent;bound=None
        while parent is not None and parent!=owner:
            if parent.get('wz09_asset_placement'):
                bound=str(parent['wz09_asset_placement_id']);break
            parent=parent.parent
        if r.get('asset_placement_id') is not None and r['asset_placement_id']!=bound:
            raise ValueError('Radio asset binding differs from parent hierarchy')
        r['asset_placement_id']=bound
        records.append(scale_audio_record(r, scale))
    return validate_records(records, maps, verify_sources=True)


def _record_for_settings(obj):
    """Read this selected zone's actual pose without scanning or hashing a world."""
    try:from .authoring_helpers import resolve_owner_map
    except ImportError:from authoring_helpers import resolve_owner_map
    r,scale=_object_record(obj,'wz09_audio_json')
    r=validate_records([r],verify_sources=False)[0]
    if r['id']!=obj.get('wz09_audio_id'):
        raise ValueError('Audio stable ID differs from stored record')
    owner=resolve_owner_map(obj)
    r['map_instance_id']=str(owner['wz09_id']) if owner is not None else None
    parent=obj.parent;bound=None
    while parent is not None and parent!=owner:
        if parent.get('wz09_asset_placement'):
            bound=str(parent['wz09_asset_placement_id']);break
        parent=parent.parent
    if r.get('asset_placement_id') is not None and r['asset_placement_id']!=bound:
        raise ValueError('Radio asset binding differs from parent hierarchy')
    r['asset_placement_id']=bound
    return scale_audio_record(r,scale)


def _wire(context, root, record, bpy, *, replace_existing=True):
    vertices, edges = contour_geometry(record)
    old = [c for c in root.children if c.get('wz09_audio_contour')] if replace_existing else []
    mesh = bpy.data.meshes.new('WZ Audio Contour')
    mesh.from_pydata(vertices, edges, []); mesh.update()
    obj = bpy.data.objects.new('♫ '+record['kind']+' · boundary / fade', mesh)
    context.scene.collection.objects.link(obj)
    obj.parent = root
    obj['wz09_audio_contour'] = True
    obj['wz09_preview_only'] = True
    obj['wz09_preview_id'] = str(uuid.uuid4())
    obj['wz09_audio_id'] = record['id']
    obj.hide_render = True; obj.hide_select = True; obj.show_in_front = True
    obj.display_type = 'WIRE'
    obj.color = (0.2, 0.7, 1.0, 1.0) if record['kind'] != 'radio' else (1.0, .65, .1, 1.0)
    for child in old:
        old_mesh = child.data
        bpy.data.objects.remove(child, do_unlink=True)
        if old_mesh and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)


def create_zone(context, record, *, owner=None, _existing_ids=None, _source_verified=False):
    import bpy
    from mathutils import Matrix, Euler, Vector
    r = validate_records([record], verify_sources=not _source_verified)[0]
    if getattr(context, 'mode', 'OBJECT') != 'OBJECT':
        raise ValueError('Switch to Object Mode before adding an audio zone')
    if owner is not None:
        if owner.get('wz09_type') != 'map_instance' or str(owner.get('wz09_id')) != r['map_instance_id']:
            raise ValueError('Audio owner map does not match record')
    elif r['map_instance_id'] is not None:
        owner = next((o for o in context.scene.objects if o.get('wz09_type') == 'map_instance' and o.get('wz09_id') == r['map_instance_id']), None)
        if owner is None:
            raise ValueError('Audio owner map is missing')
    duplicate=(r['id'] in _existing_ids if _existing_ids is not None else
               any(o.get('wz09_audio_id') == r['id'] and o.get('wz09_audio_zone') for o in context.scene.objects))
    if duplicate:
        raise ValueError('Audio ID already exists; import not duplicated')
    parent=owner
    if r.get('asset_placement_id') is not None:
        parent=next((o for o in context.scene.objects if o.get('wz09_asset_placement')
                     and o.get('wz09_asset_placement_id')==r['asset_placement_id']),None)
        if parent is None:raise ValueError('Audio visual asset is missing')
        from .authoring_helpers import resolve_owner_map
        if resolve_owner_map(parent)!=owner:raise ValueError('Audio visual and sound zone belong to different maps')
    root = bpy.data.objects.new('♫ '+r['kind'].title()+' · '+(r['alias_ref'] or 'choose alias'), None)
    context.scene.collection.objects.link(root)
    root['wz09_audio_zone'] = True; root['wz09_audio_id'] = r['id']
    root['wz09_audio_json'] = _encoded(r); root['wz09_preview_only'] = True
    root['wz09_preview_id'] = str(uuid.uuid4())
    root.empty_display_type = 'CUBE'; root.empty_display_size = .7
    root.show_name = True; root.show_in_front = True
    if parent is not None:
        root.parent = parent; root.matrix_parent_inverse = Matrix.Identity(4)
    root.matrix_world = Matrix.LocRotScale(Vector(r['position_m']), Euler([math.radians(v) for v in r['rotation_deg']], 'XYZ').to_quaternion(), Vector((1, 1, 1)))
    try:
        _wire(context, root, r, bpy,replace_existing=False)
    except Exception:
        bpy.data.objects.remove(root, do_unlink=True)
        raise
    for obj in context.selected_objects:
        obj.select_set(False)
    root.select_set(True); context.view_layer.objects.active = root
    if _existing_ids is not None: _existing_ids.add(r['id'])
    return root


def _selected(context):
    obj = context.active_object
    while obj is not None and not obj.get('wz09_audio_zone'):
        obj = obj.parent
    return obj


def _settings_record(settings, context, *, previous=None):
    from .authoring_helpers import resolve_owner_map
    owner = resolve_owner_map(context.active_object)
    r = copy.deepcopy(previous) if previous else new_record(settings.kind, settings.shape,
        position=list(context.scene.cursor.location), map_instance_id=str(owner['wz09_id']) if owner is not None else None)
    for field in ('kind', 'shape', 'alias_ref', 'priority', 'volume', 'radius_m', 'inner_radius_m',
                  'height_m', 'fade_m', 'fade_in_s', 'fade_out_s', 'enabled'):
        r[field] = getattr(settings, field)
    r['dimensions_m'] = list(settings.dimensions_m)
    if r['kind'] == 'radio':
        r['shape'] = 'sphere'
    else:
        r['inner_radius_m'] = min(r['inner_radius_m'], r['radius_m'])
    if previous and previous['alias_ref'] != r['alias_ref']:
        r['evidence'] = unverified_evidence()
    choice=json.loads(context.scene.get('wz09_audio_alias_evidence_json','{}'))
    if (choice.get('alias_ref')==r['alias_ref'] and choice.get('map_instance_id')==r['map_instance_id']
            and r['alias_ref']):r['evidence']=validate_evidence(choice['evidence'],verify_sources=True)
    return validate_records([r])[0]


def _load_settings(context, r):
    settings = context.scene.wz09_audio_settings
    for field in ('kind', 'shape', 'alias_ref', 'priority', 'volume', 'radius_m', 'inner_radius_m',
                  'height_m', 'fade_m', 'fade_in_s', 'fade_out_s', 'enabled', 'dimensions_m'):
        setattr(settings, field, r[field])


def register():
    import bpy
    from bpy.props import (StringProperty, EnumProperty, FloatProperty, IntProperty,
                           BoolProperty, FloatVectorProperty, PointerProperty)
    from bpy_extras.io_utils import ImportHelper

    class WZ09_AudioSettings(bpy.types.PropertyGroup):
        kind: EnumProperty(name='Typ', items=[('music', 'Musikzone', 'Background music with priority'), ('ambient', 'Umgebungszone', 'Additional ambient sounds'), ('radio', 'Radio / Sender', 'Point source with inner and outer range')], default='music')
        shape: EnumProperty(name='Shape', items=[('box', 'Rectangle', 'G/R to move and rotate; width/depth/height below'), ('cylinder', 'Circle', 'Circular area with bounded height'), ('sphere', 'Sphere', 'Spatial range')], default='box')
        alias_ref: StringProperty(name='Sound-Alias', description='Exact native sound alias; names are not guessed')
        dimensions_m: FloatVectorProperty(name='Width / depth / height', size=3, default=(40, 40, 12), min=.001, subtype='XYZ', unit='LENGTH')
        radius_m: FloatProperty(name='Outer radius', default=20, min=.001, unit='LENGTH')
        inner_radius_m: FloatProperty(name='Full volume until', default=3, min=0, unit='LENGTH')
        height_m: FloatProperty(name='Zone height', default=12, min=.001, unit='LENGTH')
        priority: IntProperty(name='Priority', default=0, min=-10000, max=10000, description='Higher music priority fades out lower-priority zones')
        volume: FloatProperty(name='Volume', default=1, min=0, max=1, subtype='FACTOR')
        fade_m: FloatProperty(name='Edge fade', default=3, min=0, unit='LENGTH')
        fade_in_s: FloatProperty(name='Fade in (s)', default=1, min=0)
        fade_out_s: FloatProperty(name='Fade out (s)', default=1, min=0)
        enabled: BoolProperty(name='Enabled', default=True)
        follow_asset: BoolProperty(name='Radio follows selected asset',default=True,
            description='Bind the emitter to a placed radio visual; movement follows the asset')
        list_search: StringProperty(name='Search sound sources',update=lambda self,ctx:setattr(self,'list_page',1))
        list_owner_only: BoolProperty(name='Selected map only',default=True,update=lambda self,ctx:setattr(self,'list_page',1))
        list_kind: EnumProperty(name='List',items=[('ALL','All types','')]+[(k,k,'') for k in KINDS],
            update=lambda self,ctx:setattr(self,'list_page',1))
        list_page: IntProperty(name='Page',default=1,min=1,max=10000)
        source_scale: FloatProperty(name='Explicit source units to editor units', default=0, min=0, precision=8, description='Zero blocks native coordinate import; use verified map registration')
        source_axes: StringProperty(name='Source axes (signed x,y,z)', default='', description='Explicit signed source axes, e.g. x,y,z; no implicit axis assumption')
        source_offset: FloatVectorProperty(name='Source offset', size=3, default=(0, 0, 0), subtype='XYZ')
        show_import: BoolProperty(name='Manual sidecar / CreateFX import',default=False)

    class WZ09_OT_audio_add(bpy.types.Operator):
        bl_idname = 'wz09.audio_add'; bl_label = 'Add Audio Zone at Cursor'; bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                settings=context.scene.wz09_audio_settings
                r=_settings_record(settings,context)
                if settings.kind=='radio' and settings.follow_asset:
                    from .editor_ux import resolve_asset_root
                    asset=resolve_asset_root(context.active_object)
                    if asset is not None:
                        r['asset_placement_id']=str(asset['wz09_asset_placement_id'])
                        r['position_m']=list(asset.matrix_world.translation)
                create_zone(context,r)
                return {'FINISHED'}
            except (ValueError, RuntimeError, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_audio_load(bpy.types.Operator):
        bl_idname = 'wz09.audio_load'; bl_label = 'Read Selected Audio Settings'
        def execute(self, context):
            try:
                obj = _selected(context)
                if obj is None: raise ValueError('Select an audio zone first')
                r = _record_for_settings(obj)
                _load_settings(context, r)
                return {'FINISHED'}
            except (ValueError, StopIteration, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_audio_apply(bpy.types.Operator):
        bl_idname = 'wz09.audio_apply'; bl_label = 'Apply to Selected Audio Zone'; bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                from mathutils import Matrix, Euler, Vector
                obj = _selected(context)
                if obj is None: raise ValueError('Select an audio zone first')
                previous = next(r for r in serialize_scene(context.scene) if r['id'] == obj['wz09_audio_id'])
                r = _settings_record(context.scene.wz09_audio_settings, context, previous=previous)
                _wire(context, obj, r, bpy)
                obj['wz09_audio_json'] = _encoded(r)
                obj.matrix_world = Matrix.LocRotScale(Vector(r['position_m']), Euler([math.radians(v) for v in r['rotation_deg']], 'XYZ').to_quaternion(), Vector((1, 1, 1)))
                obj.name = '♫ '+r['kind'].title()+' · '+(r['alias_ref'] or 'choose alias')
                return {'FINISHED'}
            except (ValueError, RuntimeError, StopIteration, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_audio_preview(bpy.types.Operator):
        bl_idname = 'wz09.audio_preview'; bl_label = 'Check Listener at 3D Cursor'
        def execute(self, context):
            try:
                mix = listener_mix(serialize_scene(context.scene), list(context.scene.cursor.location))
                context.scene['wz09_audio_listener_json'] = _encoded(mix)
                self.report({'INFO'}, f'{len(mix)} active aliases; spatial diagnostic only, no playback')
                return {'FINISHED'}
            except (ValueError, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_audio_select(bpy.types.Operator):
        bl_idname = 'wz09.audio_select'; bl_label = 'Select Audio Zone'
        audio_id: StringProperty()
        def execute(self, context):
            obj = next((o for o in context.scene.objects if o.get('wz09_audio_zone') and o.get('wz09_audio_id') == self.audio_id), None)
            if obj is None:
                self.report({'ERROR'}, 'Audio zone no longer exists'); return {'CANCELLED'}
            for selected in context.selected_objects: selected.select_set(False)
            obj.hide_set(False); obj.select_set(True); context.view_layer.objects.active = obj
            try:
                _load_settings(context, _record_for_settings(obj))
            except (ValueError, OSError) as exc:
                self.report({'WARNING'}, str(exc)[:220])
            return {'FINISHED'}

    class WZ09_OT_audio_remove(bpy.types.Operator):
        bl_idname = 'wz09.audio_remove'; bl_label = 'Remove Selected Audio Zone'; bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            obj = _selected(context)
            if obj is None:
                self.report({'ERROR'}, 'Select an audio zone first'); return {'CANCELLED'}
            for child in list(obj.children):
                if not child.get('wz09_audio_contour'):
                    self.report({'ERROR'}, 'Audio zone has attached objects; detach them before removing'); return {'CANCELLED'}
            for child in list(obj.children):
                data = child.data
                bpy.data.objects.remove(child, do_unlink=True)
                if data and data.users == 0: bpy.data.meshes.remove(data)
            bpy.data.objects.remove(obj, do_unlink=True)
            return {'FINISHED'}

    class WZ09_OT_audio_import(bpy.types.Operator, ImportHelper):
        bl_idname = 'wz09.audio_import'; bl_label = 'Import Native Audio Sidecar / CreateFX'; bl_options = {'REGISTER', 'UNDO'}
        filename_ext = '.json'
        filter_glob: StringProperty(default='*.json;*.gsc;*.txt', options={'HIDDEN'})
        def execute(self, context):
            created = []
            try:
                from .authoring_helpers import resolve_owner_map
                owner = resolve_owner_map(context.active_object)
                path = Path(self.filepath).resolve()
                if path.stat().st_size > 16*1024*1024: raise ValueError('Audio metadata exceeds 16 MiB limit')
                payload = path.read_bytes()
                if len(payload) > 16*1024*1024: raise ValueError('Audio metadata exceeds 16 MiB limit')
                if path.suffix.casefold() == '.json':
                    document = json.loads(payload.decode('utf-8'))
                    if document.get('kind') != 'wz09_native_audio_sidecar' or document.get('version') != 1:
                        raise ValueError('Expected wz09_native_audio_sidecar version 1')
                    records = validate_records(document.get('records'), verify_sources=True)
                else:
                    records = records_from_createfx(payload.decode('utf-8'), str(path), hashlib.sha256(payload).hexdigest(),
                        context.scene.wz09_audio_settings.source_scale,
                        axis_mapping=[v.strip() for v in context.scene.wz09_audio_settings.source_axes.split(',')],
                        offset_m=list(context.scene.wz09_audio_settings.source_offset),
                        map_instance_id=str(owner['wz09_id']) if owner else None)
                    # CreateFX origins are map-local. Apply the selected map transform.
                    if owner is not None:
                        from mathutils import Vector
                        for r in records:
                            r['position_m'] = list(owner.matrix_world @ Vector(r['position_m']))
                existing = {o.get('wz09_audio_id') for o in context.scene.objects if o.get('wz09_audio_zone')}
                if existing.intersection(r['id'] for r in records):
                    raise ValueError('Audio sidecar duplicates existing IDs; no objects imported')
                for r in records:
                    created.append(create_zone(context, r))
                self.report({'WARNING'}, f'{len(created)} source-bound/draft audio records; alias resolution/runtime untested')
                return {'FINISHED'}
            except (ValueError, OSError, UnicodeError, RuntimeError) as exc:
                for root in reversed(created):
                    for child in list(root.children):
                        mesh = child.data
                        bpy.data.objects.remove(child, do_unlink=True)
                        if mesh and mesh.users == 0: bpy.data.meshes.remove(mesh)
                    bpy.data.objects.remove(root, do_unlink=True)
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_PT_audio(bpy.types.Panel):
        bl_label = 'Music zones and radios'; bl_idname = 'WZ09_PT_audio'
        bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = 'WZ2009'; bl_parent_id = 'WZ09_PT_main'
        @classmethod
        def poll(cls, context): return getattr(context.scene, 'wz09_workspace', '') == 'AUDIO'
        def draw(self, context):
            layout, settings = self.layout, context.scene.wz09_audio_settings
            from .authoring_overlay import draw_contour_focus,browser_records
            draw_contour_focus(layout,context)
            layout.label(text='G: move - R: rotate', icon='INFO')
            from .native_semantics import draw_captured_aliases
            draw_captured_aliases(layout,context)
            from . import native_audio_preview
            native_audio_preview.draw(layout,context)
            layout.prop(settings, 'kind')
            if settings.kind != 'radio': layout.prop(settings, 'shape')
            layout.prop(settings, 'alias_ref', icon='SOUND')
            shape = 'sphere' if settings.kind == 'radio' else settings.shape
            if shape == 'box': layout.prop(settings, 'dimensions_m')
            else:
                layout.prop(settings, 'radius_m')
                if shape == 'cylinder': layout.prop(settings, 'height_m')
            if settings.kind == 'radio':
                layout.prop(settings, 'inner_radius_m');layout.prop(settings,'follow_asset')
            else:
                layout.prop(settings, 'fade_m'); layout.prop(settings, 'priority')
            layout.prop(settings, 'volume'); layout.prop(settings, 'enabled')
            row = layout.row(align=True); row.prop(settings, 'fade_in_s'); row.prop(settings, 'fade_out_s')
            layout.operator('wz09.audio_add',text='Create zone at cursor', icon='ADD')
            row = layout.row(align=True)
            row.operator('wz09.audio_load', text='Read selection', icon='EYEDROPPER')
            row.operator('wz09.audio_apply', text='Apply', icon='CHECKMARK')
            selected = _selected(context)
            if selected is not None:
                box = layout.box(); box.label(text='Selected: '+selected.name, icon='SOUND')
                box.prop(selected, 'location', text='Local position')
                box.prop(selected, 'rotation_euler', text='Local rotation')
                raw = json.loads(selected.get('wz09_audio_json', '{}'))
                box.label(text='Source: '+raw.get('evidence', {}).get('status', 'unverified'))
                box.label(text='Alias resolution / runtime untested', icon='INFO')
            from .authoring_list import filter_page
            from .authoring_helpers import resolve_owner_map
            owner=resolve_owner_map(context.active_object)
            records,index_error,index_pending=browser_records(context.scene,'audio')
            row=layout.row(align=True);row.operator('wz09.authoring_index_refresh',text='Refresh list',icon='FILE_REFRESH')
            if index_pending:layout.label(text='List updates outside viewport drawing',icon='TIME')
            if index_error:layout.label(text=index_error[:240],icon='ERROR')
            layout.prop(settings,'list_search',text='',icon='VIEWZOOM')
            row=layout.row(align=True);row.prop(settings,'list_owner_only');row.prop(settings,'list_kind',text='')
            page=filter_page(records,query=settings.list_search,
                owner_map_id=str(owner['wz09_id']) if owner is not None and settings.list_owner_only else None,
                kind=settings.list_kind,page=settings.list_page)
            row=layout.row(align=True);row.prop(settings,'list_page');row.label(text=f"{page['page']}/{page['pages']} · {page['total']} Treffer")
            for record in page['items']:
                obj=record['_object']
                row = layout.row(align=True)
                row.operator('wz09.audio_select', text=obj.name, icon='SOUND', depress=obj is selected).audio_id = str(obj['wz09_audio_id'])
            if selected is not None: layout.operator('wz09.audio_remove', icon='TRASH')
            layout.operator('wz09.audio_preview',text='Test listener at cursor', icon='VIEWZOOM')
            for item in json.loads(context.scene.get('wz09_audio_listener_json', '[]'))[:12]:
                layout.label(text=f'{item["gain"]:.0%}  {item["kind"]}: {item["alias_ref"]}', icon='SOUND')
            layout.prop(settings,'show_import',icon='IMPORT')
            if settings.show_import:
                box = layout.box()
                box.prop(settings, 'source_scale')
                box.prop(settings, 'source_axes'); box.prop(settings, 'source_offset')
                box.operator('wz09.audio_import',text='Load sidecar / CreateFX', icon='IMPORT')
                box.label(text='Specify coordinates explicitly')

    _CLASSES[:] = [WZ09_AudioSettings, WZ09_OT_audio_add, WZ09_OT_audio_load, WZ09_OT_audio_apply,
                   WZ09_OT_audio_preview, WZ09_OT_audio_select, WZ09_OT_audio_remove,
                   WZ09_OT_audio_import, WZ09_PT_audio]
    for cls in _CLASSES: bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_audio_settings = PointerProperty(type=WZ09_AudioSettings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene, 'wz09_audio_settings'): del bpy.types.Scene.wz09_audio_settings
    for cls in reversed(_CLASSES): bpy.utils.unregister_class(cls)
    _CLASSES.clear()
