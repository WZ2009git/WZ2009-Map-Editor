"""Small, explicit runtime interchange products; no game consumer or bpy import.

The host evaluates Blender constraints/F-curves and supplies world transforms.
Sampling this module's old linear authoring routes is deliberately insufficient.
All publication is exclusive and content addressed inside the editor workspace.
"""
import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid

KINDS = ('wz09_runtime_world', 'wz09_intro_runtime', 'wz09_royale_layout',
         'wz09_terrain_collision')
MAX_WORLD_BYTES = 2 * 1024 * 1024
SURFACES = ('sand', 'dirt', 'grass', 'snow', 'rock', 'asphalt')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[\x21-\x7e]{1,128}$')


def _module(name):
    return importlib.import_module('.'+name if __package__ else name, __package__ or None)


def encoded(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def identity(world_id, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'wz09:product:'+world_id+':'+kind))


def _number(value, where, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(where+': finite number required')
    if low is not None and value < low or high is not None and value > high:
        raise ValueError(where+': outside supported bounds')
    return float(value)


def _vector(value, where, length=3):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(where+': wrong coordinate count')
    return [_number(v, where) for v in value]


def _identifier(value, where):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(where+': printable ASCII ID of 1..128 characters required')
    return value


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('resource.path: relative POSIX path required')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '.' in value.split('/') or path.as_posix() != value:
        raise ValueError('resource.path: escaping path refused')
    return path


def resource(payload, *, folder='products', suffix='.json'):
    """Describe actual bytes; caller publishes the matching payload separately."""
    if not isinstance(payload, bytes):
        raise ValueError('resource requires bytes')
    digest = hashlib.sha256(payload).hexdigest()
    return dict(path=folder+'/'+digest+suffix, sha256=digest, byte_length=len(payload))


def tactical_reference(tactical_map, base_dir, *, labels_baked):
    """Normalize an existing, actually generated PNG without rendering or copying.

    The caller explicitly confirms whether the underlying PNG has baked labels;
    a schematic SVG with POI text is not substituted for an unlabelled image.
    """
    if type(labels_baked) is not bool or labels_baked:
        raise ValueError('Runtime tactical image requires explicit labels_baked:false')
    if tactical_map.get('status') != 'generated' or not tactical_map.get('image'):
        return dict(status='not_generated', image=None, width_px=0, height_px=0,
                    bounds_m=list(tactical_map.get('bounds_m', [-3072, -3072, 3072, 3072])),
                    pixel_to_world=[0, 0, 0, 0, 0, 0], format='none', labels_baked=False)
    image = tactical_map['image']
    relative = _path(image['path'])
    base = Path(base_dir).resolve()
    path = (base/relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError('Tactical PNG missing or escaped')
    ref = dict(path=image['path'], sha256=image['sha256'], byte_length=path.stat().st_size)
    _check_resource(ref, base)
    header = path.read_bytes()[:29]
    if len(header) != 29 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[24] != 8 or header[25] not in (2, 6):
        raise ValueError('Runtime tactical image must be actual RGB/RGBA8 PNG')
    width, height = image['width_px'], image['height_px']
    bounds = image['bounds_m']
    affine = _vector(tactical_map.get('pixel_to_world'), 'tactical_map.pixel_to_world', 6)
    return dict(status='generated', image=ref, width_px=width, height_px=height, bounds_m=list(bounds),
                pixel_to_world=list(affine), format='png_rgba8' if header[25] == 6 else 'png_rgb8', labels_baked=False)


def _check_resource(record, base_dir=None):
    if not isinstance(record, dict) or set(record) != {'path', 'sha256', 'byte_length'}:
        raise ValueError('resource: exact path/hash/length record required')
    _path(record['path'])
    if not isinstance(record['sha256'], str) or not _SHA.fullmatch(record['sha256']):
        raise ValueError('resource.sha256: lowercase SHA-256 required')
    if type(record['byte_length']) is not int or record['byte_length'] <= 0:
        raise ValueError('resource.byte_length: positive integer required')
    if base_dir is not None:
        base = Path(base_dir).resolve()
        target = (base/record['path']).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise ValueError('resource: missing or escaped file: '+record['path'])
        if target.stat().st_size != record['byte_length']:
            raise ValueError('resource: byte length differs: '+record['path'])
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
        if digest.hexdigest() != record['sha256']:
            raise ValueError('resource: hash differs: '+record['path'])


def _header(kind, world_id):
    return dict(kind=kind, version=1, id=identity(world_id, kind), world_id=world_id,
                units='meters', coordinate_system='world_z_up', runtime_integrated=False,
                physical_scale_verified=False, clearance_verified=False)


def _pose(record, where, camera=False):
    fields = {'position_m', 'forward', 'up'} | ({'hfov_deg'} if camera else set())
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError(where+': exact evaluated pose fields required')
    result = {k: _vector(record[k], where+'.'+k) for k in ('position_m', 'forward', 'up')}
    for axis in ('forward', 'up'):
        norm = math.hypot(*result[axis])
        if norm < 1e-9:
            raise ValueError(where+': zero orientation vector')
        result[axis] = [c/norm for c in result[axis]]
    dot = sum(a*b for a, b in zip(result['forward'], result['up']))
    if abs(dot) > 1e-5:
        raise ValueError(where+': forward/up are not orthogonal')
    if camera:
        result['hfov_deg'] = _number(record['hfov_deg'], where+'.hfov_deg', .01, 179.99)
    return result


def sample_times(duration_s):
    duration = _number(duration_s, 'duration_s', .001, 3600)
    times = [i/30 for i in range(math.floor(duration*30+1e-8)+1)]
    if not math.isclose(times[-1], duration, abs_tol=1e-8):
        times.append(duration)
    else:
        times[-1] = duration
    return times


def bake_intro_samples(timeline, world, evaluator, *, evaluation_source,
                       aircraft_forward_axis, aircraft_up_axis):
    """Bake evaluated world poses at 30 Hz, preserving cuts and camera lens.

    evaluator(t_s) returns aircraft/camera dictionaries in world metres. Native
    callers evaluate the depsgraph at fractional frames and derive camera FOV
    from the evaluated render view frustum, including sensor fit/aspect ratio.
    'synthetic_fixture' is accepted solely with synthetic source_status.
    """
    _module('cinematic_core').validate_document(timeline, world)
    if evaluation_source not in ('blender_evaluated_depsgraph', 'synthetic_fixture'):
        raise ValueError('evaluated Blender dependency graph or explicit synthetic fixture required')
    events = [e for e in timeline['events'] if e['kind'] == 'aircraft_insertion']
    if len(events) != 1:
        raise ValueError('Exactly one aircraft insertion event required')
    event = events[0]
    p = event['parameters']
    if aircraft_forward_axis not in ('+X', '-X', '+Y', '-Y', '+Z', '-Z') or aircraft_up_axis not in ('+X', '-X', '+Y', '-Y', '+Z', '-Z'):
        raise ValueError('Explicit signed aircraft model axes required')
    if aircraft_forward_axis[-1] == aircraft_up_axis[-1]:
        raise ValueError('Aircraft forward and up axes must differ')
    result = _header('wz09_intro_runtime', world['world_id'])
    result.update(sample_rate_hz=30, evaluation_source=evaluation_source,
                  source_status='synthetic' if evaluation_source == 'synthetic_fixture' else 'evaluated_editor_scene',
                  source_timeline_id=timeline['id'], source_timeline_sha256=hashlib.sha256(encoded(timeline)).hexdigest(),
                  source_routes_sha256=timeline['source_routes_sha256'],
                  aircraft_model_ref='vehicle_ac130_coop@common_mp',
                  aircraft_forward_axis=aircraft_forward_axis, aircraft_up_axis=aircraft_up_axis,
                  spawn_marker_id=event['target_id'], spawn_offset_m=list(p['spawn_offset_m']),
                  insertion_s=event['time_s'], jump_open_s=p['jump_open_s'], jump_close_s=p['jump_close_s'],
                  jump_open_abs_s=event['time_s']+p['jump_open_s'],
                  jump_close_abs_s=event['time_s']+p['jump_close_s'], auto_jump_at_close=True,
                  camera_axes=dict(forward='-Z', up='+Y'),
                  duration_s=timeline['duration_s'], samples=[], audio_events=[])
    for time in sample_times(timeline['duration_s']):
        raw = evaluator(time)
        if not isinstance(raw, dict) or set(raw) != {'aircraft', 'camera'}:
            raise ValueError('evaluator must return aircraft and camera world poses')
        result['samples'].append(dict(t_s=time, presentation_only=time < event['time_s'],
                                      aircraft=_pose(raw['aircraft'], 'aircraft'),
                                      camera=_pose(raw['camera'], 'camera', True)))
    for e in timeline['events']:
        if e['kind'] == 'audio':
            alias = e['reference']
            if not isinstance(alias, str) or not re.fullmatch('[A-Za-z0-9_]+', alias):
                raise ValueError('Runtime audio requires a MW2 alias name, never a path or zone ID')
            result['audio_events'].append(dict(id=e['id'], t_s=e['time_s'], alias_ref=alias,
                                                parameters=copy.deepcopy(e['parameters'])))
        elif e['kind'] == 'screen_fade':
            # Copy validated declarative presentation data; never resimulate
            # Blender curves or infer fades from sampled camera movement.
            p_fade = e['parameters']
            result.setdefault('screen_fades', []).append(dict(
                id=identity(world['world_id'], 'intro-fade:'+e['id']), source_event_id=e['id'],
                t_s=e['time_s'], color=list(p_fade['color']), from_alpha=p_fade['from_alpha'],
                to_alpha=p_fade['to_alpha'], duration_s=p_fade['duration_s']))
    if 'screen_fades' in result:
        result['screen_fades'].sort(key=lambda fade: (fade['t_s'], fade['id']))
    speeds = [math.dist(a['aircraft']['position_m'], b['aircraft']['position_m'])/(b['t_s']-a['t_s'])
              for a, b in zip(result['samples'], result['samples'][1:])]
    result['continuation'] = dict(route_id=p['route_id'], route_elapsed_at_insertion_s=p['route_elapsed_s'],
                                   start_abs_s=event['time_s'], end_abs_s=timeline['duration_s'],
                                   interpolation='sampled_linear', speed_mode='per_segment',
                                   segment_speeds_mps=speeds)
    validate_document(result)
    return result


def royale_layout(world_id, *, play_bounds, drop_bounds, out_of_bounds,
                   flight_altitude_m, spawn, aircraft_corridor, parachute,
                   zone_center_candidates=(), poi_regions=(), speed_mps=None):
    """Explicit design inputs. This function performs no ground/clearance trace."""
    result = _header('wz09_royale_layout', world_id)
    result.update(play_bounds=copy.deepcopy(play_bounds), drop_bounds=copy.deepcopy(drop_bounds),
                  out_of_bounds=copy.deepcopy(list(out_of_bounds)), flight_altitude_m=flight_altitude_m,
                  spawn=copy.deepcopy(spawn), aircraft_corridor=copy.deepcopy(aircraft_corridor),
                  parachute=copy.deepcopy(parachute), zone_center_candidates=copy.deepcopy(list(zone_center_candidates)),
                  poi_regions=copy.deepcopy(list(poi_regions)), gulag_arena=None)
    if speed_mps is not None:
        speed = _number(speed_mps, 'royale.speed_mps')
        if speed <= 0:
            raise ValueError('royale.speed_mps: positive actual route speed required')
        result['speed_mps'] = speed
    validate_document(result)
    return result


def _yaw(rotation, where, quarter_turn=False):
    values = _vector(rotation, where)
    if abs(values[0]) > 1e-7 or abs(values[1]) > 1e-7:
        raise ValueError(where+': pitch/roll must equal zero')
    yaw = values[2] % 360
    if quarter_turn and yaw not in (0, 90, 180, 270):
        raise ValueError(where+': map yaw must be 0/90/180/270 degrees')
    return [0., 0., 0. if math.isclose(yaw, 360, abs_tol=1e-7) else yaw]


def source_yaw_rotation(rotation, where, quarter_turn=False):
    """Canonicalize only bounded native Float32 pose readback at export.

    Runtime documents themselves still require exact quarter turns. The
    1e-5-degree source bound covers measured Blender 90 ->89.9999957 readback
    without permitting free map rotation or changing native source records.
    """
    values = _vector(rotation, where)
    tolerance = 1e-5
    if abs(values[0]) > tolerance or abs(values[1]) > tolerance:
        raise ValueError(where+': source pitch/roll exceeds Float32 readback tolerance')
    yaw = values[2] % 360
    if quarter_turn:
        nearest = min((0., 90., 180., 270., 360.), key=lambda value: abs(yaw-value))
        if abs(yaw-nearest) > tolerance:
            raise ValueError(where+': source map yaw must be a bounded 0/90/180/270 readback')
        yaw = nearest % 360
    elif min(yaw, 360-yaw) <= tolerance:
        yaw = 0.
    return _yaw([0., 0., yaw], where, quarter_turn)


def _runtime_calibration(record, verify_sources):
    proof = record.get('calibration')
    if not isinstance(proof, dict):
        raise ValueError('map '+record['id']+': explicit calibration required')
    calibration = _module('calibration')
    normalized = calibration.validate_calibration(proof)
    if verify_sources:
        calibration.verify_calibration_source(normalized)
    factor = normalized.get('source_units_to_meter')
    if factor is None or abs(factor-.0254) > 1e-6:
        raise ValueError('map '+record['id']+': runtime requires documented factor 0.0254 +/- 1e-6')
    if normalized['axis_mapping'] != ['x', 'y', 'z'] or any(abs(v) > 1e-8 for v in normalized['offset_m']):
        raise ValueError('map '+record['id']+': native origin requires identity axes and zero calibration offset')
    basis = normalized.get('basis', 'measured_landmarks')
    if basis not in ('export_registration', 'p1a_export_convention'):
        raise ValueError('map '+record['id']+': unsupported runtime calibration basis')
    registration = normalized.get('registration', normalized.get('export_convention', {}))
    exporter_hash = registration.get('exporter_sha256', normalized.get('exporter_sha256'))
    if not isinstance(exporter_hash, str) or not _SHA.fullmatch(exporter_hash):
        raise ValueError('map '+record['id']+': exporter_sha256 required')
    result = dict(basis=basis, source_units_to_meter=factor, axis_mapping=['x', 'y', 'z'],
                  offset_m=[0., 0., 0.], exporter_sha256=exporter_hash,
                  source_sha256=normalized['source_sha256'],
                  evidence_sha256=hashlib.sha256(encoded(normalized)).hexdigest(),
                  evidence_status='synthetic' if normalized.get('synthetic') else 'source_bound' if verify_sources else 'unverified',
                  physical_scale_verified=False)
    return result


def runtime_world(world, *, intro_ref, royale_ref, tactical_map, terrain_ref=None,
                  verify_sources=True, synthetic_fixture=False):
    """Select compact runtime data, rejecting missing calibration and rotations.

    Absolute source paths and placement/audio/action blobs are never copied.
    World coordinates locate the original native origin without recentering.
    """
    if world.get('world_id') != 'wz09_karvorsk':
        raise ValueError('Runtime product requires world_id wz09_karvorsk')
    result = _header('wz09_runtime_world', world['world_id'])
    result.update(world_kind='composite', source_status='synthetic' if synthetic_fixture else 'source_bound',
                  runtime_ready=False, maps=[], markers=[],
                  products=dict(intro=copy.deepcopy(intro_ref), royale_layout=copy.deepcopy(royale_ref),
                                terrain_collision=copy.deepcopy(terrain_ref)), tactical_map=copy.deepcopy(tactical_map))
    for m in world['maps']:
        calibration = _runtime_calibration(m, verify_sources)
        if calibration['evidence_status'] == 'synthetic' and not synthetic_fixture:
            raise ValueError('Synthetic map calibration cannot be published as a real source')
        result['maps'].append(dict(id=m['id'], source_map=m['source_map'],
                                   position_m=list(m['position_m']), rotation_deg=source_yaw_rotation(m['rotation_deg'], 'map rotation', True),
                                   source_units_to_meter=calibration['source_units_to_meter'], calibration=calibration))
    allowed = {'poi', 'loot_crate', 'buy_station', 'player_spawn', 'aircraft_keyframe',
               'loadout_drop', 'ground_loot', 'warmup_spawn'}
    marker_fields = {'name', 'loot_table', 'shop_catalog', 'profile', 'route_id', 'sequence', 'time_s', 'ground_snap'}
    for m in world.get('markers', []):
        if m['kind'] not in allowed:
            continue
        props = m.get('properties', {})
        result['markers'].append(dict(id=m['id'], kind=m['kind'], map_instance_id=m.get('map_instance_id'),
                                      position_m=list(m['position_m']), rotation_deg=source_yaw_rotation(m['rotation_deg'], 'marker rotation'),
                                      properties={k: copy.deepcopy(v) for k, v in props.items() if k in marker_fields}))
    validate_document(result)
    return result


def _polygon(value, where):
    return _module('terrain_core').validate_polygon(value, where)


def _check_pose(record, where, camera=False):
    normalized = _pose(record, where, camera)
    for axis in ('forward', 'up'):
        if any(abs(a-b) > 1e-5 for a, b in zip(record[axis], normalized[axis])):
            raise ValueError(where+': unit orientation vector required')


def validate_document(document, world=None, base_dir=None):
    """Strict structural and semantic gate for one runtime product."""
    kind = document.get('kind') if isinstance(document, dict) else None
    if kind not in KINDS:
        raise ValueError('Unsupported runtime product kind')
    validation = _module('validation')
    errors = []
    if not validation._finite_json(document, errors.append, max_nodes=2000000,
                                   max_total_string_bytes=8*1024*1024):
        raise ValueError(errors[0])
    definitions = _module('runtime_schema').definitions()
    schema = {'$defs': definitions, '$ref': '#/$defs/product_'+kind}
    validation._evaluate(document, schema, schema, errors.append)
    if errors:
        raise ValueError(errors[0])
    ids = set()
    def add_id(value, where):
        _identifier(value, where)
        if value in ids:
            raise ValueError(where+': duplicate global ID '+value)
        ids.add(value)
    add_id(document['id'], 'product.id')
    if kind == 'wz09_intro_runtime':
        if document['aircraft_forward_axis'][-1] == document['aircraft_up_axis'][-1]:
            raise ValueError('intro: aircraft local axes must be orthogonal')
        if (document['evaluation_source'] == 'synthetic_fixture') != (document['source_status'] == 'synthetic'):
            raise ValueError('intro: synthetic evaluation/source status differs')
        expected = sample_times(document['duration_s'])
        samples = document['samples']
        if len(samples) != len(expected) or any(abs(s['t_s']-t) > 1e-7 for s, t in zip(samples, expected)):
            raise ValueError('intro.samples: exact 30 Hz grid with final endpoint required')
        for sample in samples:
            _check_pose(sample['aircraft'], 'sample.aircraft')
            _check_pose(sample['camera'], 'sample.camera', True)
            if sample['presentation_only'] != (sample['t_s'] < document['insertion_s']):
                raise ValueError('intro.samples: presentation flag differs from insertion')
            if not sample['presentation_only']:
                for pose in ('aircraft', 'camera'):
                    if max(abs(c) for c in sample[pose]['position_m']) > 3200:
                        raise ValueError('intro.samples: playable samples exceed +/-3200m')
        for rel, absolute in (('jump_open_s', 'jump_open_abs_s'), ('jump_close_s', 'jump_close_abs_s')):
            if abs(document['insertion_s']+document[rel]-document[absolute]) > 1e-7:
                raise ValueError('intro: absolute jump time differs')
        if not (document['insertion_s'] <= document['jump_open_abs_s'] < document['jump_close_abs_s'] <= document['duration_s']):
            raise ValueError('intro: insertion/jump window outside sampled continuation')
        continuation = document['continuation']
        if continuation['start_abs_s'] != document['insertion_s'] or continuation['end_abs_s'] != document['duration_s']:
            raise ValueError('intro.continuation: must cover insertion through final sample')
        speeds = continuation['segment_speeds_mps']
        if len(speeds) != len(samples)-1:
            raise ValueError('intro.continuation: speed required for every sample segment')
        for a, b, speed in zip(samples, samples[1:], speeds):
            actual = math.dist(a['aircraft']['position_m'], b['aircraft']['position_m'])/(b['t_s']-a['t_s'])
            if not math.isclose(actual, speed, rel_tol=1e-7, abs_tol=1e-7):
                raise ValueError('intro.continuation: speed differs from evaluated samples')
        for event in document['audio_events']:
            add_id(event['id'], 'audio.id')
            if event['t_s'] > document['duration_s']:
                raise ValueError('intro.audio: event beyond sampled duration')
        previous = None
        source_events = set()
        for fade in sorted(document.get('screen_fades', []), key=lambda record: record['t_s']):
            add_id(fade['id'], 'screen_fade.id')
            if fade['source_event_id'] in source_events:
                raise ValueError('intro.screen_fades: duplicate source event reference')
            source_events.add(fade['source_event_id'])
            end = fade['t_s']+fade['duration_s']
            if end > document['duration_s']+1e-7:
                raise ValueError('intro.screen_fades: fade extends beyond sampled duration')
            if previous is not None:
                if fade['t_s'] < previous['t_s']+previous['duration_s']-1e-7:
                    raise ValueError('intro.screen_fades: fade intervals overlap')
                if abs(fade['from_alpha']-previous['to_alpha']) > 1e-7:
                    raise ValueError('intro.screen_fades: alpha is discontinuous')
                if previous['to_alpha'] > 0 and fade['color'] != previous['color']:
                    raise ValueError('intro.screen_fades: color changes while opaque')
            previous = fade
    elif kind == 'wz09_royale_layout':
        for key in ('play_bounds', 'drop_bounds'):
            _polygon(document[key], 'royale.'+key)
        for area in document['out_of_bounds']:
            add_id(area['id'], 'out_of_bounds.id')
            _polygon(area['polygon_m'], 'out_of_bounds.polygon')
        core = _module('terrain_core')
        if any(not core.point_in_polygon(p, document['play_bounds']) for p in document['drop_bounds']):
            raise ValueError('royale.drop_bounds: must lie inside play_bounds')
        for point in document['zone_center_candidates']:
            add_id(point['id'], 'zone_center.id')
            if any(core.point_in_polygon(point['position_m'][:2], area['polygon_m']) for area in document['out_of_bounds']):
                raise ValueError('royale.zone_center: candidate lies in out_of_bounds')
        for region in document['poi_regions']:
            _polygon(region['polygon_m'], 'poi_region.polygon')
        for value in _coordinates(document):
            if abs(value) > 3200:
                raise ValueError('royale: gameplay coordinate exceeds +/-3200m')
    elif kind == 'wz09_runtime_world':
        if len(encoded(document)) >= MAX_WORLD_BYTES:
            raise ValueError('runtime_world: must remain smaller than 2 MiB')
        map_ids = {m['id'] for m in document['maps']}
        for m in document['maps']:
            add_id(m['id'], 'map.id')
            _yaw(m['rotation_deg'], 'map.rotation', True)
            c = m['calibration']
            if c['axis_mapping'] != ['x', 'y', 'z'] or c['offset_m'] != [0, 0, 0]:
                raise ValueError('map.calibration: native-origin identity axes required')
            if abs(c['source_units_to_meter']-.0254) > 1e-6 or c['source_units_to_meter'] != m['source_units_to_meter']:
                raise ValueError('map.calibration: factor differs from runtime contract')
            if document['source_status'] != 'synthetic' and c['evidence_status'] != 'source_bound':
                raise ValueError('runtime_world: unverified/synthetic calibration is not source-bound')
        for m in document['markers']:
            add_id(m['id'], 'marker.id')
            _yaw(m['rotation_deg'], 'marker.rotation')
            if m['map_instance_id'] is not None and m['map_instance_id'] not in map_ids:
                raise ValueError('marker: unknown map instance ID')
        for ref in document['products'].values():
            if ref is not None:
                _check_resource(ref, base_dir)
        tactical = document['tactical_map']
        if tactical['status'] == 'generated':
            _check_resource(tactical['image'], base_dir)
            x0, y0, x1, y1 = tactical['bounds_m']
            a, b, c, d, e, f = tactical['pixel_to_world']
            width, height = tactical['width_px'], tactical['height_px']
            if x1 <= x0 or y1 <= y0 or b != 0 or d != 0 or a <= 0 or e >= 0:
                raise ValueError('tactical_map: north-aligned positive bounds required')
            if any(abs(v-w) > 1e-7 for v, w in zip((c, f, c+a*width, f+e*height), (x0, y1, x1, y0))):
                raise ValueError('tactical_map: affine and image bounds differ')
            if width <= 0 or height <= 0 or tactical['format'] not in ('png_rgb8', 'png_rgba8'):
                raise ValueError('tactical_map: generated image requires dimensions and PNG format')
            if base_dir is not None:
                image = dict(tactical['image'], width_px=width, height_px=height,
                             bounds_m=tactical['bounds_m'], kind='orthographic_preview',
                             physical_scale_verified=False, source_fingerprint='0'*64,
                             projection={'kind': 'runtime_registered_tactical_image'})
                image.pop('byte_length')
                path = _module('source_bundle').verify_orthographic_image(image, base_dir, tactical['bounds_m'], (width, height))
                header = path.read_bytes()[:29]
                if header[24] != 8 or header[25] != (6 if tactical['format'] == 'png_rgba8' else 2):
                    raise ValueError('tactical_map: PNG must match RGB/RGBA8 declaration')
        elif tactical['image'] is not None or tactical['format'] != 'none' or tactical['width_px'] or tactical['height_px']:
            raise ValueError('tactical_map: not_generated cannot claim image metadata')
        for group in ('maps', 'markers'):
            for m in document[group]:
                if max(abs(v) for v in m['position_m']) > 3200:
                    raise ValueError('runtime_world: gameplay coordinate exceeds +/-3200m')
        if base_dir is not None:
            _validate_bundle_links(document, base_dir)
    elif kind == 'wz09_terrain_collision':
        _module('terrain_collision').validate_document(document, base_dir)
    if world is not None and document['world_id'] != world['world_id']:
        raise ValueError('Runtime product world identity differs')
    if world is not None and kind == 'wz09_runtime_world':
        source_maps = {m['id']: m for m in world['maps']}
        for m in document['maps']:
            source = source_maps.get(m['id'])
            if source is None or any(m[k] != source[k] for k in ('source_map', 'position_m')) or m['rotation_deg'] != source_yaw_rotation(source['rotation_deg'], 'source map rotation', True):
                raise ValueError('runtime_world: map differs from source world')
    return document


def _validate_bundle_links(document, base_dir):
    products = {}
    expected = {'intro': 'wz09_intro_runtime', 'royale_layout': 'wz09_royale_layout', 'terrain_collision': 'wz09_terrain_collision'}
    for key, kind in expected.items():
        ref = document['products'][key]
        if ref is None:
            continue
        if ref['byte_length'] > 64*1024*1024:
            raise ValueError('runtime product exceeds read budget')
        child = json.loads((Path(base_dir)/ref['path']).read_text(encoding='utf-8'))
        if child.get('kind') != kind or child.get('world_id') != document['world_id']:
            raise ValueError('runtime product typed reference differs: '+key)
        validate_document(child, base_dir=base_dir)
        products[key] = child
    intro, royale = products['intro'], products['royale_layout']
    markers = {m['id']: m for m in document['markers']}
    marker = markers.get(intro['spawn_marker_id'])
    if marker is None or marker['kind'] != 'player_spawn' or royale['spawn']['marker_id'] != marker['id']:
        raise ValueError('runtime spawn references must identify the same player_spawn')
    if royale['spawn']['position_m'] != marker['position_m'] or royale['spawn']['aircraft_local_offset_m'] != intro['spawn_offset_m']:
        raise ValueError('runtime spawn pose/offset differs between products')
    at = intro['insertion_s']*intro['sample_rate_hz']
    lo = min(int(at),len(intro['samples'])-2)
    fraction = min(1.,max(0.,at-lo))
    a,b = intro['samples'][lo]['aircraft']['position_m'],intro['samples'][lo+1]['aircraft']['position_m']
    aircraft = [a[k]+(b[k]-a[k])*fraction for k in range(3)]
    if sum((aircraft[k]-marker['position_m'][k])**2 for k in range(3)) > 0.01**2:
        raise ValueError('runtime spawn differs from evaluated aircraft at insertion')
    if document['source_status'] != 'synthetic' and intro['source_status'] == 'synthetic':
        raise ValueError('runtime real world cannot reference synthetic intro')
    if 'terrain_collision' in products:
        out_ids = {r['id'] for r in royale['out_of_bounds']}
        maps = {m['id'] for m in document['maps']}
        for core in products['terrain_collision']['collision_cores']:
            if core['map_instance_id'] not in maps:
                raise ValueError('collision_core: unknown map instance ID')
        for water in products['terrain_collision']['water_volumes']:
            if water['out_of_bounds_id'] is not None and water['out_of_bounds_id'] not in out_ids:
                raise ValueError('water: missing royale out_of_bounds reference')


def _coordinates(document):
    """Gameplay positions only; arbitrary scalar properties are not coordinates."""
    for key in ('play_bounds', 'drop_bounds'):
        for point in document[key]:
            yield from point
    for area in document['out_of_bounds']:
        for point in area['polygon_m']:
            yield from point
    yield from document['spawn']['position_m']
    for point in document['zone_center_candidates']:
        yield from point['position_m']


def publish_bytes(path, payload, *, root):
    """Exclusive immutable publication, including binary grid resources."""
    safe = _module('scene_io').safe_output_path
    path = safe(path, root)
    if path.exists():
        if path.read_bytes() == payload:
            return path
        raise FileExistsError('Refusing to overwrite existing runtime resource: '+str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if safe(path, root) != path:
        raise ValueError('Runtime output parent changed')
    fd, temporary = tempfile.mkstemp(prefix='.wz09-runtime-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError('Refusing raced runtime output replacement')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def align_cinematic_spawn(world, intro, royale):
    """Derive the runtime insertion marker from evaluated aircraft samples.

    Authoring objects are untouched. The aircraft-local harness offset stays
    separate, as required by the consumer contract; the marker is its root.
    """
    validate_document(intro, world)
    samples = intro['samples']
    at = float(intro['insertion_s'])*int(intro['sample_rate_hz'])
    lo = min(int(at), len(samples)-2)
    fraction = min(1., max(0., at-lo))
    a, b = samples[lo]['aircraft']['position_m'], samples[lo+1]['aircraft']['position_m']
    position = [a[k]+(b[k]-a[k])*fraction for k in range(3)]
    result, layout = copy.deepcopy(world), copy.deepcopy(royale)
    marker = next((m for m in result.get('markers', []) if m['id']==intro['spawn_marker_id']), None)
    if marker is None or marker['kind']!='player_spawn' or layout['spawn']['marker_id']!=marker['id']:
        raise ValueError('cinematic insertion must reference one existing player_spawn')
    marker['position_m'] = position
    layout['spawn']['position_m'] = list(position)
    layout['spawn']['aircraft_local_offset_m'] = list(intro['spawn_offset_m'])
    return result, layout


def publish_runtime_bundle(world, intro, royale, tactical_map, output_dir, *, root,
                           terrain=None, terrain_resources=None, verify_sources=True,
                           synthetic_fixture=False):
    """Return runtime document and immutable path after complete validation."""
    output = Path(output_dir)
    world, royale = align_cinematic_spawn(world, intro, royale)
    documents = [intro, royale] + ([terrain] if terrain is not None else [])
    refs = {}
    payloads = dict(terrain_resources or {})
    for document in documents:
        validate_document(document, world)
        payload = encoded(document)
        ref = resource(payload)
        refs[document['kind']] = ref
        payloads[ref['path']] = payload
    runtime = runtime_world(world, intro_ref=refs['wz09_intro_runtime'], royale_ref=refs['wz09_royale_layout'],
                            tactical_map=tactical_map, terrain_ref=refs.get('wz09_terrain_collision'),
                            verify_sources=verify_sources, synthetic_fixture=synthetic_fixture)
    # Validation above must finish before any bytes are published.
    for name, payload in payloads.items():
        _path(name)
        publish_bytes(output/name, payload, root=root)
    for document in documents:
        validate_document(document, world, output)
    validate_document(runtime, world, output)
    target = output/('wz09_runtime_world-'+hashlib.sha256(encoded(runtime)).hexdigest()[:20]+'.json')
    publish_bytes(target, encoded(runtime), root=root)
    return dict(document=runtime, path=target, documents=documents, references=refs)
