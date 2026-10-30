"""Measured source-coordinate calibration; usable without Blender or third-party modules."""
import importlib.util
import json
import math
from pathlib import Path
import re


def _source_bundle_module():
    path = Path(__file__).with_name('source_bundle.py')
    spec = importlib.util.spec_from_file_location('_wz09_source_bundle', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registration(data):
    required = {'kind', 'reference_path', 'reference_sha256', 'resources',
                'native_source_path', 'native_source_sha256', 'landmark_evidence_path',
                'landmark_evidence_sha256', 'method', 'exporter_sha256', 'physical_scale_verified'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('calibration.registration: complete export-registration record required')
    if data['kind'] != 'export_registration' or data['physical_scale_verified'] is not False:
        raise ValueError('calibration.registration: kind export_registration and physical_scale_verified false required')
    normalized = dict(data)
    if not isinstance(data['method'], str) or not data['method'].strip():
        raise ValueError('calibration.registration.method: nonempty text required')
    for name in ('reference_path', 'native_source_path', 'landmark_evidence_path'):
        if not isinstance(data[name], str) or not Path(data[name]).expanduser().is_absolute():
            raise ValueError(f'calibration.registration.{name}: absolute path required')
    for name in ('reference_sha256', 'native_source_sha256', 'landmark_evidence_sha256', 'exporter_sha256'):
        if not isinstance(data[name], str) or not re.fullmatch(r'[0-9a-fA-F]{64}', data[name]):
            raise ValueError(f'calibration.registration.{name}: SHA-256 required')
        normalized[name] = data[name].lower()
    if not isinstance(data['resources'], list):
        raise ValueError('calibration.registration.resources: array required')
    resources = []
    for record in data['resources']:
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('calibration.registration.resources: path/sha256 records required')
        if not isinstance(record['path'], str) or not Path(record['path']).expanduser().is_absolute():
            raise ValueError('calibration.registration.resources.path: absolute path required')
        if not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-fA-F]{64}', record['sha256']):
            raise ValueError('calibration.registration.resources.sha256: SHA-256 required')
        resources.append({'path': record['path'], 'sha256': record['sha256'].lower()})
    normalized['resources'] = resources
    return normalized


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{field}: expected a finite number')
    return float(value)


def _vec3(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{field}: expected three coordinates')
    return [_number(item, field) for item in value]


def _mapped(point, axes):
    point = _vec3(point, 'source point')
    return [(-1 if axis.startswith('-') else 1) * point['xyz'.index(axis[-1])] for axis in axes]


def _noncollinear(points):
    origin = points[0]
    vectors = [[p[i] - origin[i] for i in range(3)] for p in points[1:]]
    for a in vectors:
        aa = sum(x * x for x in a)
        if aa == 0:
            continue
        for b in vectors:
            bb = sum(x * x for x in b)
            cross = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
            if bb and sum(x*x for x in cross) > aa * bb * 1e-20:
                return True
    return False


def validate_calibration(data):
    """Return normalized evidence and its fitted uniform scale, or raise ValueError.

    The declared signed-axis permutation and offset are fixed. The positive scale is
    measured from centered landmarks and every resulting point must meet the stated
    error bound. This checks evidence consistency, not the physical truth of a survey.
    """
    if not isinstance(data, dict):
        raise ValueError('calibration: expected an object')
    basis = data.get('basis', 'measured_landmarks')
    if basis not in ('measured_landmarks', 'export_registration'):
        raise ValueError('calibration.basis: measured_landmarks or export_registration required')
    physical = data.get('physical_scale_verified')
    if 'physical_scale_verified' in data and type(physical) is not bool:
        raise ValueError('calibration.physical_scale_verified: explicit boolean required')
    registration = None
    if basis == 'export_registration':
        registration = _registration(data.get('registration'))
        if data.get('synthetic') is not False or physical is not False:
            raise ValueError('calibration: export_registration requires real source data and physical_scale_verified false')
    elif 'registration' in data:
        raise ValueError('calibration.registration: only allowed for basis export_registration')
    if data.get('synthetic') is True and physical is True:
        raise ValueError('calibration: synthetic evidence cannot verify physical scale')
    for name in ('source_path', 'measurement_basis'):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise ValueError(f'calibration.{name}: nonempty text required')
    digest = data.get('source_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('calibration.source_sha256: SHA-256 required')
    axes = data.get('axis_mapping')
    if not isinstance(axes, list) or len(axes) != 3 or any(
            not isinstance(a, str) or not re.fullmatch(r'[+-]?[xyzXYZ]', a) for a in axes):
        raise ValueError('calibration.axis_mapping: three signed axes required')
    axes = [a.lower() for a in axes]
    if {a[-1] for a in axes} != set('xyz'):
        raise ValueError('calibration.axis_mapping: each source axis must occur once')
    offset = _vec3(data.get('offset_m'), 'calibration.offset_m')
    error = _number(data.get('max_error_m'), 'calibration.max_error_m')
    if error < 0:
        raise ValueError('calibration.max_error_m: must be nonnegative')
    if type(data.get('synthetic')) is not bool:
        raise ValueError('calibration.synthetic: explicit boolean required')
    landmarks = data.get('landmarks')
    if not isinstance(landmarks, list) or len(landmarks) < 3:
        raise ValueError('calibration.landmarks: at least three measured landmarks required')
    normalized = []
    for index, point in enumerate(landmarks):
        if not isinstance(point, dict):
            raise ValueError(f'calibration.landmarks[{index}]: expected an object')
        normalized.append({'source': _vec3(point.get('source'), f'landmarks[{index}].source'),
                           'local_m': _vec3(point.get('local_m'), f'landmarks[{index}].local_m')})
    source = [_mapped(point['source'], axes) for point in normalized]
    target = [point['local_m'] for point in normalized]
    if not _noncollinear(source) or not _noncollinear(target):
        raise ValueError('calibration.landmarks: source and local landmarks must be non-collinear')
    center_s = [sum(p[i] for p in source) / len(source) for i in range(3)]
    center_t = [sum(p[i] for p in target) / len(target) for i in range(3)]
    numerator = sum((s[i]-center_s[i]) * (t[i]-center_t[i])
                    for s, t in zip(source, target) for i in range(3))
    denominator = sum((s[i]-center_s[i])**2 for s in source for i in range(3))
    scale = numerator / denominator
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('calibration: landmark fit does not yield a positive uniform scale')
    residual = max(math.sqrt(sum((s[i]*scale + offset[i]-t[i])**2 for i in range(3)))
                   for s, t in zip(source, target))
    if residual > error + 1e-9:
        raise ValueError(f'calibration: landmark residual {residual:.9g}m exceeds {error:.9g}m')
    declared_scale = data.get('source_units_to_meter')
    if declared_scale is not None and not math.isclose(
            _number(declared_scale, 'calibration.source_units_to_meter'), scale, rel_tol=1e-8, abs_tol=1e-12):
        raise ValueError('calibration.source_units_to_meter: differs from measured landmark scale')
    result = {'source_path': data['source_path'], 'source_sha256': digest.lower(),
            'measurement_basis': data['measurement_basis'], 'axis_mapping': axes,
            'offset_m': offset, 'landmarks': normalized, 'max_error_m': error,
            'synthetic': data['synthetic'], 'source_units_to_meter': scale}
    if 'basis' in data:
        result['basis'] = basis
    if 'physical_scale_verified' in data:
        result['physical_scale_verified'] = physical
    if registration is not None:
        result['registration'] = registration
    return result


def has_physical_scale(calibration):
    """Registration fits describe asset alignment, never independent physical scale."""
    evidence = validate_calibration(calibration)
    return (not evidence['synthetic'] and evidence.get('basis', 'measured_landmarks') == 'measured_landmarks'
            and evidence.get('physical_scale_verified') is not False)


def verify_calibration_source(calibration):
    """Check source bytes and registration's full buffer/evidence chain read-only."""
    calibration = validate_calibration(calibration)
    bundle = _source_bundle_module()
    bundle.verify_file(calibration['source_path'], calibration['source_sha256'], 'calibration.source_sha256')
    registration = calibration.get('registration')
    if registration is None:
        return
    bundle.verify_gltf_bundle(registration['reference_path'], registration['reference_sha256'],
                             registration['resources'])
    bundle.verify_file(registration['native_source_path'], registration['native_source_sha256'],
                       'registration.native_source_sha256')
    path = bundle.verify_file(registration['landmark_evidence_path'], registration['landmark_evidence_sha256'],
                              'registration.landmark_evidence_sha256')
    evidence = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(evidence, dict):
        raise ValueError('registration.landmark_evidence: object required')
    # The hash alone is insufficient: bind the actual fit and both original files.
    for name, expected in (('source_path', calibration['source_path']),
                           ('source_sha256', calibration['source_sha256']),
                           ('native_source_path', registration['native_source_path']),
                           ('native_source_sha256', registration['native_source_sha256']),
                           ('reference_path', registration['reference_path']),
                           ('reference_sha256', registration['reference_sha256']),
                           ('exporter_sha256', registration['exporter_sha256'])):
        value = evidence.get(name)
        matches = (isinstance(value, str) and Path(value).expanduser().is_absolute()
                   and Path(value).expanduser().resolve() == Path(expected).expanduser().resolve()
                   if name.endswith('_path') else value == expected)
        if not matches:
            raise ValueError(f'registration.landmark_evidence.{name}: disagrees with registered calibration')
    if 'exporter_path' in evidence:
        bundle.verify_file(evidence['exporter_path'], registration['exporter_sha256'],
                           'registration.landmark_evidence.exporter_sha256')
    for name in ('axis_mapping', 'offset_m', 'landmarks', 'max_error_m', 'source_units_to_meter'):
        if name not in evidence:
            raise ValueError(f'registration.landmark_evidence.{name}: required fit evidence missing')
    numeric_evidence = dict(calibration)
    numeric_evidence.update({name: evidence[name] for name in
                             ('axis_mapping', 'offset_m', 'landmarks', 'max_error_m', 'source_units_to_meter')})
    normalized = validate_calibration(numeric_evidence)
    if (not matching_calibration_transform(calibration, normalized)
            or normalized['landmarks'] != calibration['landmarks']
            or normalized['max_error_m'] != calibration['max_error_m']):
        raise ValueError('registration.landmark_evidence: fit differs from registered landmarks/transform')


def load_calibration(path):
    """Read a JSON calibration and verify the source hash; sources may be read-only."""
    path = Path(path).expanduser().resolve()
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(raw, dict) and isinstance(raw.get('registration'), dict):
        registration = raw['registration']
        for name in ('reference_path', 'native_source_path', 'landmark_evidence_path'):
            if isinstance(registration.get(name), str):
                target = Path(registration[name]).expanduser()
                registration[name] = str((path.parent / target).resolve() if not target.is_absolute() else target.resolve())
        for record in registration.get('resources', []) if isinstance(registration.get('resources'), list) else []:
            if isinstance(record, dict) and isinstance(record.get('path'), str):
                target = Path(record['path']).expanduser()
                record['path'] = str((path.parent / target).resolve() if not target.is_absolute() else target.resolve())
    data = validate_calibration(raw)
    source = Path(data['source_path']).expanduser()
    data['source_path'] = str((path.parent / source).resolve() if not source.is_absolute() else source.resolve())
    verify_calibration_source(data)
    return data


def matching_calibration_transform(left, right):
    """Check whether two independently sourced measurements define the same map space."""
    left=validate_calibration(left);right=validate_calibration(right)
    return ([axis.lstrip('+') for axis in left['axis_mapping']]
            ==[axis.lstrip('+') for axis in right['axis_mapping']]
            and math.isclose(left['source_units_to_meter'],right['source_units_to_meter'],
                             rel_tol=1e-8,abs_tol=1e-12)
            and all(math.isclose(a,b,rel_tol=0,abs_tol=1e-8)
                    for a,b in zip(left['offset_m'],right['offset_m'])))


def transform_source(point, scale, calibration):
    """Transform source coordinates to map-local meters using explicit measured data."""
    evidence = validate_calibration(calibration)
    scale = _number(scale, 'source_units_to_meter')
    if scale <= 0 or not math.isclose(scale, evidence['source_units_to_meter'], rel_tol=1e-8, abs_tol=1e-12):
        raise ValueError('source_units_to_meter: must match the measured calibration')
    mapped = _mapped(point, evidence['axis_mapping'])
    result = [mapped[i]*scale + evidence['offset_m'][i] for i in range(3)]
    if any(not math.isfinite(c) for c in result):
        raise ValueError('source point: transformed coordinates are nonfinite')
    return result
