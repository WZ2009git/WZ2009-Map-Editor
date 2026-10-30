"""Read-only integrity checks for a registered glTF reference and its buffers."""
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import struct
from urllib.parse import unquote, urlsplit
import zlib


_HASH = re.compile(r'^[0-9a-f]{64}$')


def sha256_file(path):
    """Hash files in bounded blocks, including large native geometry/buffers."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path, expected, field):
    if not isinstance(path, str) or not path.strip() or not Path(path).expanduser().is_absolute():
        raise ValueError(f'{field}: absolute path required')
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ValueError(f'{field}: lowercase SHA-256 required')
    target = Path(path).expanduser().resolve()
    if sha256_file(target) != expected:
        raise ValueError(f'{field}: source changed: {target}')
    return target


def _document(path):
    if path.suffix.lower() == '.gltf':
        return json.loads(path.read_text(encoding='utf-8-sig')), None
    if path.suffix.lower() != '.glb':
        raise ValueError('registration.reference_path: expected .gltf or .glb')
    with path.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b'glTF':
            raise ValueError('registration.reference_path: invalid GLB header')
        version, size = struct.unpack('<II', header[4:])
        if version != 2 or size != path.stat().st_size:
            raise ValueError('registration.reference_path: invalid GLB version/size')
        chunk = stream.read(8)
        if len(chunk) != 8 or chunk[4:] != b'JSON':
            raise ValueError('registration.reference_path: GLB JSON chunk required')
        length = int.from_bytes(chunk[:4], 'little')
        if length > 64 * 1024 * 1024 or length % 4 or 20 + length > size:
            raise ValueError('registration.reference_path: invalid/oversized GLB JSON')
        document = json.loads(stream.read(length).decode('utf-8'))
        binary_length = None
        cursor = 20 + length
        while cursor < size:
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise ValueError('registration.reference_path: incomplete GLB chunk')
            count = int.from_bytes(chunk[:4], 'little')
            cursor += 8 + count
            if count % 4 or cursor > size:
                raise ValueError('registration.reference_path: invalid GLB chunk length')
            if chunk[4:] == b'BIN\x00':
                if binary_length is not None:
                    raise ValueError('registration.reference_path: duplicate GLB BIN chunk')
                binary_length = count
            stream.seek(count, 1)
        return document, binary_length


def _external_resource(uri, base):
    if not isinstance(uri, str) or not uri:
        raise ValueError('registration reference: nonempty buffer URI required')
    if uri.startswith('data:'):
        # Embedded bytes are already bound by the reference-file hash.
        if ',' not in uri:
            raise ValueError('registration reference: malformed data URI')
        return None
    parsed = urlsplit(uri)
    decoded = unquote(parsed.path)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '\\' in decoded
            or ':' in decoded or any(ord(c) < 32 for c in decoded)):
        raise ValueError('registration reference: only local relative buffer URIs supported')
    target = (base / decoded).resolve()
    if Path(decoded).is_absolute() or not target.is_relative_to(base):
        raise ValueError('registration reference: buffer URI escapes reference directory')
    return target


def verify_gltf_bundle(reference_path, reference_sha256, resources, include_images=False):
    """Verify the reference and all its buffers; extra declared images are checked too.

    External buffer paths must stay inside the reference directory. Embedded data
    and GLB BIN bytes are protected by the main-file hash. No network is accessed.
    """
    reference = verify_file(reference_path, reference_sha256, 'registration.reference_sha256')
    if not isinstance(resources, list):
        raise ValueError('registration.resources: array required')
    base = reference.parent
    bound = {}
    for record in resources:
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('registration.resources: each resource requires only path/sha256')
        path = verify_file(record['path'], record['sha256'], 'registration.resources.sha256')
        if not path.is_relative_to(base) or path == reference:
            raise ValueError('registration.resources: resource must stay inside reference directory')
        if path in bound:
            raise ValueError('registration.resources: duplicate resource path')
        bound[path] = record['sha256']
    document, binary_length = _document(reference)
    if (not isinstance(document, dict) or not isinstance(document.get('asset'), dict)
            or document['asset'].get('version') != '2.0'):
        raise ValueError('registration.reference_path: glTF 2.0 asset required')
    buffers = document.get('buffers', [])
    if not isinstance(buffers, list):
        raise ValueError('registration reference: buffers must be an array')
    for index, buffer in enumerate(buffers):
        if not isinstance(buffer, dict):
            raise ValueError('registration reference: invalid buffer record')
        length = buffer.get('byteLength')
        if type(length) is not int or length <= 0:
            raise ValueError('registration reference: positive integer buffer.byteLength required')
        if 'uri' not in buffer:
            if index != 0 or binary_length is None or not length <= binary_length <= length + 3:
                raise ValueError('registration reference: missing/inconsistent GLB BIN buffer')
            continue
        target = _external_resource(buffer['uri'], base)
        if target is not None:
            if target not in bound:
                raise ValueError(f'registration.resources: missing referenced buffer: {target}')
            if target.stat().st_size < length:
                raise ValueError(f'registration reference: truncated buffer: {target}')
    if include_images:
        images = document.get('images', [])
        if not isinstance(images, list):
            raise ValueError('registration reference: images must be an array')
        for image in images:
            if not isinstance(image, dict):
                raise ValueError('registration reference: invalid image record')
            if 'uri' not in image:
                if 'bufferView' not in image:
                    raise ValueError('registration reference: image URI/bufferView required')
                continue
            target = _external_resource(image['uri'], base)
            if target is not None and target not in bound:
                raise ValueError(f'registration.resources: missing referenced image: {target}')
    return document


def capture_gltf_bundle(reference_path, include_images=True):
    """Capture hashes for the current local asset; this alone is not calibration."""
    reference = Path(reference_path).expanduser().resolve()
    document, _ = _document(reference)
    if not isinstance(document, dict):
        raise ValueError('registration.reference_path: glTF object required')
    targets = set()
    for name in ('buffers', 'images') if include_images else ('buffers',):
        records = document.get(name, [])
        if not isinstance(records, list):
            raise ValueError(f'registration reference: {name} must be an array')
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f'registration reference: invalid {name} record')
            if 'uri' in record:
                target = _external_resource(record['uri'], reference.parent)
                if target is not None:
                    targets.add(target)
    bundle = {'reference_path': str(reference), 'reference_sha256': sha256_file(reference),
              'resources': [{'path': str(target), 'sha256': sha256_file(target)}
                            for target in sorted(targets)]}
    verify_gltf_bundle(**bundle, include_images=include_images)
    return bundle


def verify_preview_bundle(bundle):
    """A preview manifest binds all referenced external buffers and image bytes."""
    if not isinstance(bundle, dict) or set(bundle) != {'reference_path', 'reference_sha256', 'resources'}:
        raise ValueError('provenance.preview_bundle: exact reference_path/reference_sha256/resources record required')
    return verify_gltf_bundle(**bundle, include_images=True)


def validate_orthographic_image(record, bounds, size):
    """Validate image metadata without claiming terrain, collision or physical scale."""
    required = {'path', 'sha256', 'width_px', 'height_px', 'bounds_m', 'kind',
                'physical_scale_verified', 'source_fingerprint', 'projection'}
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError('tactical_map.image: complete orthographic preview record required')
    if record['kind'] != 'orthographic_preview' or record['physical_scale_verified'] is not False:
        raise ValueError('tactical_map.image: orthographic_preview with physical_scale_verified false required')
    if not isinstance(record['projection'], dict) or not record['projection']:
        raise ValueError('tactical_map.image.projection: nonempty metadata object required')
    for name in ('sha256', 'source_fingerprint'):
        if not isinstance(record[name], str) or not _HASH.fullmatch(record[name]):
            raise ValueError(f'tactical_map.image.{name}: lowercase SHA-256 required')
    path = record['path']
    if not isinstance(path, str) or not path:
        raise ValueError('tactical_map.image.path: relative PNG path required')
    posix, windows = PurePosixPath(path), PureWindowsPath(path)
    if ('\\' in path or ':' in path or any(ord(c) < 32 for c in path)
            or posix.is_absolute() or windows.is_absolute() or windows.drive
            or '..' in posix.parts or path != posix.as_posix() or path in ('.', '')
            or posix.suffix.lower() != '.png'):
        raise ValueError('tactical_map.image.path: safe relative PNG path required')
    if any(type(record[name]) is not int or record[name] < 1 for name in ('width_px', 'height_px')):
        raise ValueError('tactical_map.image: positive integer dimensions required')
    actual_bounds = record['bounds_m']
    if (not isinstance(actual_bounds, (list, tuple)) or len(actual_bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in actual_bounds) or tuple(actual_bounds) != tuple(bounds)):
        raise ValueError('tactical_map.image.bounds_m: must exactly match tactical bounds')
    if (record['width_px'], record['height_px']) != tuple(size):
        raise ValueError('tactical_map.image: dimensions must match tactical viewport')
    return posix


def verify_orthographic_image(record, base_dir, bounds, size):
    """Check PNG bytes, chunk CRCs, dimensions and its exact tactical registration."""
    relative = validate_orthographic_image(record, bounds, size)
    base = Path(base_dir).expanduser().resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('tactical_map.image.path: resolves outside artifact directory')
    verify_file(str(path), record['sha256'], 'tactical_map.image.sha256')
    with path.open('rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n':
            raise ValueError('tactical_map.image: invalid PNG signature')
        first = True
        has_data = False
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError('tactical_map.image: missing PNG IEND chunk')
            length, kind = struct.unpack('>I4s', header)
            if length > 0x7fffffff or first and (kind != b'IHDR' or length != 13):
                raise ValueError('tactical_map.image: invalid PNG chunk header')
            if first:
                info = stream.read(length)
                if len(info) != 13:
                    raise ValueError('tactical_map.image: truncated PNG IHDR')
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', info)
                if (width, height) != tuple(size):
                    raise ValueError('tactical_map.image: PNG dimensions disagree with metadata')
                allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
                if depth not in allowed.get(color, ()) or compression or filtering or interlace not in (0, 1):
                    raise ValueError('tactical_map.image: invalid PNG IHDR fields')
                crc = zlib.crc32(info, zlib.crc32(kind))
                first = False
            else:
                if kind == b'IHDR':
                    raise ValueError('tactical_map.image: duplicate PNG IHDR')
                crc = zlib.crc32(kind)
                left = length
                while left:
                    block = stream.read(min(left, 1024 * 1024))
                    if not block:
                        raise ValueError('tactical_map.image: truncated PNG chunk')
                    left -= len(block)
                    crc = zlib.crc32(block, crc)
            checksum = stream.read(4)
            if len(checksum) != 4 or int.from_bytes(checksum, 'big') != crc:
                raise ValueError('tactical_map.image: PNG chunk CRC mismatch')
            if kind == b'IDAT':
                has_data = True
            if kind == b'IEND':
                if length or not has_data or stream.read(1):
                    raise ValueError('tactical_map.image: invalid PNG end/data sequence')
                break
    return path
