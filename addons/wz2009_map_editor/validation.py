"""Bounded, bpy-free preflight for this repository's world-v1 contract.

This small evaluator supports only the keywords in the bundled schema. Unknown
keywords fail closed. Static evidence checks do not prove walkability.
"""
import hashlib
import importlib.util
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

try:
    from .nav_diagnostics import diagnose
except ImportError:
    _diagnostics_spec = importlib.util.spec_from_file_location('_wz09_preflight_nav_diagnostics',
                                                              Path(__file__).with_name('nav_diagnostics.py'))
    _diagnostics_module = importlib.util.module_from_spec(_diagnostics_spec)
    _diagnostics_spec.loader.exec_module(_diagnostics_module)
    diagnose = _diagnostics_module.diagnose

MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 320
_KEYWORDS = {'$schema', '$id', 'title', 'description', '$defs', '$ref', 'type',
             'additionalProperties', 'required', 'properties', 'enum', 'const',
             'minLength', 'maxLength', 'pattern', 'minimum', 'exclusiveMinimum', 'maximum',
             'items', 'minItems', 'maxItems', 'uniqueItems'}
_HASH = re.compile(r'^[0-9a-f]{64}$')


class _Messages:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.error_count = self.warning_count = 0

    def add(self, kind, message):
        name = 'error_count' if kind == 'errors' else 'warning_count'
        setattr(self, name, getattr(self, name) + 1)
        messages = getattr(self, kind)
        if len(messages) < MAX_MESSAGES:
            messages.append(str(message)[:MAX_MESSAGE_CHARS])

    def error(self, message):
        self.add('errors', message)

    def warn(self, message):
        self.add('warnings', message)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(left, right):
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _matches(value, kind):
    return {'object': lambda: isinstance(value, dict),
            'array': lambda: isinstance(value, list),
            'string': lambda: isinstance(value, str),
            'number': lambda: _number(value),
            'integer': lambda: _number(value) and (isinstance(value, int) or value.is_integer()),
            'boolean': lambda: isinstance(value, bool),
            'null': lambda: value is None}.get(kind, lambda: False)()


def _schema_path():
    here = Path(__file__).resolve().parent
    packaged = here / 'data/world-v1.schema.json'
    return packaged if packaged.is_file() else here.parents[1] / 'schema/world-v1.schema.json'


def _check_schema(schema, emit, path='$schema', depth=0):
    if depth > 64:
        emit(f'{path}: schema nesting limit exceeded')
        return
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        emit(f'{path}: schema must be object or boolean')
        return
    for key in schema:
        if key not in _KEYWORDS:
            emit(f'{path}.{key}: unsupported schema keyword (fail closed)')
    for key in ('properties', '$defs'):
        if key in schema:
            if not isinstance(schema[key], dict):
                emit(f'{path}.{key}: expected schema map')
            else:
                for name, child in schema[key].items():
                    _check_schema(child, emit, f'{path}.{key}.{name}', depth + 1)
    for key in ('items', 'additionalProperties'):
        if key in schema:
            _check_schema(schema[key], emit, f'{path}.{key}', depth + 1)
    if '$ref' in schema and (not isinstance(schema['$ref'], str) or
                            not schema['$ref'].startswith('#/$defs/')):
        emit(f'{path}.$ref: only local $defs references supported')
    if 'pattern' in schema:
        try:
            re.compile(schema['pattern'])
        except (TypeError, re.error):
            emit(f'{path}.pattern: invalid regular expression')
    if 'type' in schema:
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not kinds or any(k not in ('object', 'array', 'string', 'number', 'integer',
                                      'boolean', 'null') for k in kinds):
            emit(f'{path}.type: unsupported JSON type')
    if 'uniqueItems' in schema and type(schema['uniqueItems']) is not bool:
        emit(f'{path}.uniqueItems: boolean required')
    for key in ('minLength', 'maxLength'):
        if key in schema and (not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0):
            emit(f'{path}.{key}: nonnegative integer required')


def _evaluate(value, schema, root, emit, path='$', depth=0):
    if depth > 128:
        emit(f'{path}: nesting limit exceeded')
        return
    if isinstance(schema, bool):
        if not schema:
            emit(f'{path}: forbidden by schema')
        return
    if '$ref' in schema:
        target = root
        for part in schema['$ref'][2:].split('/'):
            target = target.get(part.replace('~1', '/').replace('~0', '~')) if isinstance(target, dict) else None
        if target is None:
            emit(f'{path}: unresolved schema reference')
        else:
            _evaluate(value, target, root, emit, path, depth + 1)
    if 'type' in schema:
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(_matches(value, kind) for kind in kinds):
            emit(f'{path}: expected {"/".join(kinds)}')
            return
    if 'const' in schema and not _same(value, schema['const']):
        emit(f'{path}: expected constant {schema["const"]!r}')
    if 'enum' in schema and not any(_same(value, item) for item in schema['enum']):
        emit(f'{path}: unsupported value {str(value)[:80]!r}')
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                emit(f'{path}.{key}: required field missing')
        props = schema.get('properties', {})
        for key, item in value.items():
            if key in props:
                _evaluate(item, props[key], root, emit, f'{path}.{key}', depth + 1)
            elif 'additionalProperties' in schema:
                _evaluate(item, schema['additionalProperties'], root, emit,
                          f'{path}.{str(key)[:80]}', depth + 1)
    if isinstance(value, list):
        if schema.get('uniqueItems'):
            duplicate=(len(value)!=len(set(value)) if all(isinstance(v,str) for v in value)
                       else any(_same(item,other) for i,item in enumerate(value) for other in value[:i]))
            if duplicate:emit(f'{path}: duplicate array items')
        for key, comparison in (('minItems', lambda n, b: n < b),
                                ('maxItems', lambda n, b: n > b)):
            if key in schema and comparison(len(value), schema[key]):
                emit(f'{path}: violates {key}={schema[key]}')
        if 'items' in schema:
            for index, item in enumerate(value):
                _evaluate(item, schema['items'], root, emit, f'{path}[{index}]', depth + 1)
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            emit(f'{path}: string too short')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            emit(f'{path}: string too long (maxLength={schema["maxLength"]})')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            emit(f'{path}: does not match required pattern')
    if _number(value):
        for key, comparison in (('minimum', lambda n, b: n < b),
                                ('exclusiveMinimum', lambda n, b: n <= b),
                                ('maximum', lambda n, b: n > b)):
            if key in schema and comparison(value, schema[key]):
                emit(f'{path}: violates {key}={schema[key]}')


def _finite_json(value, emit, *, max_nodes=10000000, max_depth=128,
                 max_string_bytes=1024 * 1024, max_total_string_bytes=256 * 1024 * 1024):
    """Bounded JSON preflight, including opaque properties, without recursion.

    A sixteen-map world repeats legitimate source bundles on thousands of
    placements. Ten million value/container visits accommodate that structure;
    separate depth and UTF-8 budgets still reject pathological input. Iterator
    frames retain only the active ancestry, rather than queuing every sibling.
    Shared containers are checked on every occurrence, just like parsed JSON.
    Keyword budgets are private test seams; report() always uses these defaults.
    """
    budgets = (max_nodes, max_depth, max_string_bytes, max_total_string_bytes)
    if any(type(limit) is not int for limit in budgets) or min(max_nodes, max_string_bytes, max_total_string_bytes) < 1 or max_depth < 0:
        raise ValueError('JSON limits require positive integer budgets and nonnegative depth')
    frames = []
    ancestors = set()
    visited = total_string_bytes = 0

    def check_text(text, path):
        nonlocal total_string_bytes
        # ASCII paths/hashes dominate large source inventories. Their UTF-8
        # size equals len(), avoiding millions of temporary byte allocations.
        if len(text) > max_string_bytes:
            emit(f'{path}: JSON string byte limit exceeded')
            return False
        try:
            size = len(text) if text.isascii() else len(text.encode('utf-8'))
        except UnicodeEncodeError:
            emit(f'{path}: invalid UTF-8 text')
            return False
        total_string_bytes += size
        if size > max_string_bytes or total_string_bytes > max_total_string_bytes:
            emit(f'{path}: JSON string byte limit exceeded')
            return False
        return True

    item, path, depth = value, '$', 0
    while True:
        visited += 1
        if visited > max_nodes or depth > max_depth:
            emit(f'{path}: JSON size/nesting limit exceeded')
            return False
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in ancestors:
                emit(f'{path}: cyclic input is not JSON')
                return False
            ancestors.add(identity)
            is_object = isinstance(item, dict)
            pairs = iter(item.items()) if is_object else enumerate(item)
            frames.append((pairs, path, depth, identity, is_object))
        elif isinstance(item, str):
            if not check_text(item, path):
                return False
        elif isinstance(item, float) and not math.isfinite(item):
            emit(f'{path}: nonfinite number')
            return False
        elif item is not None and not isinstance(item, (int, float, bool)):
            emit(f'{path}: non-JSON value {type(item).__name__}')
            return False
        # Pull only the next child. Completed frames remove their ancestor ID,
        # so aliases are legal while actual cycles remain unambiguously invalid.
        while frames:
            pairs, parent_path, parent_depth, identity, is_object = frames[-1]
            try:
                key, item = next(pairs)
            except StopIteration:
                frames.pop()
                ancestors.remove(identity)
                continue
            if is_object:
                if not isinstance(key, str):
                    emit(f'{parent_path}: object keys must be strings')
                    return False
                if not check_text(key, parent_path):
                    return False
            path, depth = f'{parent_path}.{str(key)[:80]}', parent_depth + 1
            break
        else:
            return True


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _uid(value):
    return isinstance(value, str) and bool(value)


def _file_reference(record, where, base_dir, emit, extension=None):
    path = record.get('path')
    digest = record.get('sha256')
    if not isinstance(path, str) or not path:
        emit(f'{where}.path: nonempty relative file path required')
        return
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    bad_parts = any(part.endswith(('.', ' ')) or re.match(
        r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.IGNORECASE)
                    for part in posix.parts)
    if ('\\' in path or any(ord(c) < 32 for c in path) or ':' in path or bad_parts or posix.is_absolute() or
            windows.is_absolute() or windows.drive or '..' in posix.parts or
            path != posix.as_posix() or path in ('.', '')):
        emit(f'{where}.path: unsafe or nonportable relative path')
        return
    if extension and posix.suffix.lower() != extension:
        emit(f'{where}.path: expected {extension} file')
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        emit(f'{where}.sha256: lowercase SHA-256 required')
        return
    if base_dir is None:
        return
    try:
        base = Path(base_dir).resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base):
            emit(f'{where}.path: resolves outside manifest directory')
            return
        if not target.is_file():
            emit(f'{where}.path: referenced file does not exist: {path}')
            return
        with target.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            emit(f'{where}.sha256: file hash mismatch')
        if extension == '.glb':
            size = target.stat().st_size
            with target.open('rb') as stream:
                header = stream.read(12)
                chunk_header = stream.read(8)
                valid_header = (len(header) == 12 and header[:4] == b'glTF' and
                                int.from_bytes(header[4:8], 'little') == 2 and
                                int.from_bytes(header[8:12], 'little') == size)
                chunk_length = int.from_bytes(chunk_header[:4], 'little')
                valid_chunk = (len(chunk_header) == 8 and chunk_header[4:] == b'JSON' and
                               chunk_length > 0 and chunk_length % 4 == 0 and
                               chunk_length + 20 <= size)
                if valid_header and valid_chunk:
                    # Check container boundaries without expanding any mesh into output.
                    cursor = 20 + chunk_length
                    while cursor < size:
                        stream.seek(cursor)
                        more = stream.read(8)
                        if len(more) != 8:
                            valid_chunk = False
                            break
                        count = int.from_bytes(more[:4], 'little')
                        cursor += 8 + count
                        if count % 4 or cursor > size:
                            valid_chunk = False
                            break
                    valid_chunk = valid_chunk and cursor == size
            if not valid_header or not valid_chunk:
                emit(f'{where}.path: invalid GLB v2 header/length')
    except (OSError, ValueError, TypeError) as exc:
        emit(f'{where}.path: cannot verify file ({str(exc)[:120]})')


def _calibration_module():
    """Reuse the same numeric calibration solver as CSV/preview import."""
    module_path = Path(__file__).with_name('calibration.py')
    spec = importlib.util.spec_from_file_location('_wz09_preflight_calibration', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calibration(data):
    return _calibration_module().validate_calibration(data)


def _calibration_source(data, base_dir, where, emit):
    if base_dir is None:
        return False
    source = Path(data['source_path']).expanduser()
    if not source.is_absolute():
        source = Path(base_dir) / source
    resolved = dict(data, source_path=str(source.resolve()))
    try:
        _calibration_module().verify_calibration_source(resolved)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
        emit(f'{where}: {str(exc)[:180]}')
        return False
    return True


def _components(graph):
    unseen = set(graph)
    sizes = []
    while unseen:
        stack = [unseen.pop()]
        count = 0
        while stack:
            node = stack.pop()
            count += 1
            for child in graph[node]:
                if child in unseen:
                    unseen.remove(child)
                    stack.append(child)
        sizes.append(count)
    return sorted(sizes, reverse=True)


def _strong_components(graph):
    """Iterative Kosaraju: no recursion limit on large waypoint graphs."""
    seen = set()
    order = []
    for start in graph:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(graph[start]))]
        while stack:
            node, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                order.append(node)
                stack.pop()
            elif nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(graph[nxt])))
    reverse = {node: set() for node in graph}
    for node, children in graph.items():
        for child in children:
            reverse[child].add(node)
    sizes = []
    seen.clear()
    for start in reversed(order):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        count = 0
        while stack:
            node = stack.pop()
            count += 1
            for child in reverse[node]:
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        sizes.append(count)
    return sorted(sizes, reverse=True)


def report(world, base_dir=None, require_calibration=False, diagnostic_limits=None):
    """Return JSON-safe ok/errors/warnings/stats, with at most 50 of each message.

    base_dir is the manifest directory: references must stay within it. Without
    it, file existence remains unknown. require_calibration rejects placeholders.
    """
    messages = _Messages()
    stats = {}
    diagnostics = {}

    def finish():
        stats.update(error_count=messages.error_count, warning_count=messages.warning_count,
                     errors_truncated=max(0, messages.error_count - len(messages.errors)),
                     warnings_truncated=max(0, messages.warning_count - len(messages.warnings)))
        return {'ok': messages.error_count == 0, 'errors': messages.errors,
                'warnings': messages.warnings, 'stats': stats, 'diagnostics': diagnostics}

    if not _finite_json(world, messages.error):
        return finish()
    try:
        schema = json.loads(_schema_path().read_text(encoding='utf-8'))
        before = messages.error_count
        _check_schema(schema, messages.error)
        if messages.error_count != before:
            return finish()
        _evaluate(world, schema, schema, messages.error)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        messages.error(f'$schema: cannot validate bundled schema ({str(exc)[:150]})')
        return finish()
    if not isinstance(world, dict):
        return finish()
    maps = _records(world.get('maps'))
    markers = _records(world.get('markers'))
    nav = world.get('nav') if isinstance(world.get('nav'), dict) else {}
    nodes = _records(nav.get('nodes'))
    edges = _records(nav.get('edges'))
    geometry = _records(world.get('geometry'))
    assets = _records(world.get('asset_placements'))
    ids = set()
    for category, records in (('maps', maps), ('markers', markers),
                              ('nav.nodes', nodes), ('nav.edges', edges), ('geometry', geometry), ('asset_placements', assets)):
        for index, record in enumerate(records):
            uid = record.get('id')
            if not _uid(uid):
                messages.error(f'{category}[{index}].id: nonempty stable ID required')
            elif uid in ids:
                messages.error(f'{category}[{index}].id: duplicate global ID {uid[:80]}')
            else:
                ids.add(uid)
    mapids = {m['id'] for m in maps if _uid(m.get('id'))}
    if 'products' in world:
        try:
            try:from .product_export import read_references
            except ImportError:from product_export import read_references
            read_references(world,base_dir)
            if base_dir is None:messages.warn('products: no base_dir; sidecar files/hashes not verified')
        except (OSError,ValueError,TypeError,KeyError,ImportError,AttributeError,ArithmeticError) as exc:
            messages.error('products: '+str(exc)[:220])
    if any(key in world for key in ('composition','audio_zones','action_elements')):
        try:
            try:from .composition_data import validate_extensions
            except ImportError:from composition_data import validate_extensions
            validate_extensions(world,set(ids))
            for key in ('audio_zones','action_elements'):
                if world.get(key):messages.warn(key+': editor bindings only; runtime integration unverified')
        except (OSError,ValueError,TypeError,KeyError,ImportError,AttributeError,ArithmeticError) as exc:
            messages.error('editor_extensions: '+str(exc)[:200])
    nodeids = {n['id'] for n in nodes if _uid(n.get('id'))}
    owners = {n['id']: n.get('owner_map') for n in nodes if _uid(n.get('id'))}
    for index, record in enumerate(maps):
        where = f'maps[{index}]'
        factor = record.get('source_units_to_meter')
        calibrated = record.get('calibration_status') == 'calibrated'
        proof = record.get('calibration')
        if calibrated:
            if not _number(factor) or factor <= 0:
                messages.error(f'{where}: calibrated map requires positive source_units_to_meter')
            if not isinstance(proof, dict) or proof.get('synthetic') is not False:
                messages.error(f'{where}: calibrated status requires non-synthetic landmark evidence')
        elif factor is not None:
            messages.error(f'{where}: unverified map must use null source_units_to_meter')
        normalized = None
        source_verified = False
        if proof is not None:
            try:
                normalized = _calibration(proof)
                if calibrated and not _calibration_module().has_physical_scale(normalized):
                    messages.error(f'{where}: export registration cannot promote physical calibration status')
                expected = normalized['source_units_to_meter']
                if calibrated and _number(factor) and not math.isclose(factor, expected, rel_tol=1e-7, abs_tol=1e-10):
                    messages.error(f'{where}: scale disagrees with measured landmarks')
                source_verified = _calibration_source(normalized, base_dir, where + '.calibration', messages.error)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
                messages.error(f'{where}.calibration: {str(exc)[:180]}')
        provenance = record.get('provenance')
        preview_bundle = provenance.get('preview_bundle') if isinstance(provenance, dict) else None
        if preview_bundle is not None:
            if base_dir is None:
                messages.warn(f'{where}.provenance.preview_bundle: no base_dir; preview files/hashes not verified')
            else:
                try:
                    _calibration_module()._source_bundle_module().verify_preview_bundle(preview_bundle)
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
                    messages.error(f'{where}.provenance.preview_bundle: {str(exc)[:180]}')
        obj_bundle = provenance.get('obj_preview_bundle') if isinstance(provenance, dict) else None
        if obj_bundle is not None:
            if base_dir is None:
                messages.warn(f'{where}.provenance.obj_preview_bundle: no base_dir; OBJ resources/hashes not verified')
            else:
                try:
                    obj_spec = importlib.util.spec_from_file_location('_wz09_preflight_obj_bundle',
                                                                    Path(__file__).with_name('obj_bundle.py'))
                    obj_module = importlib.util.module_from_spec(obj_spec)
                    obj_spec.loader.exec_module(obj_module)
                    obj_module.verify_obj_bundle(obj_bundle)
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
                    messages.error(f'{where}.provenance.obj_preview_bundle: {str(exc)[:180]}')
        sources = provenance.get('calibration_sources') if isinstance(provenance, dict) else None
        if sources is not None:
            if not isinstance(sources, list):
                messages.error(f'{where}.provenance.calibration_sources: expected array')
            elif normalized is None and sources:
                messages.error(f'{where}.provenance.calibration_sources: principal calibration required')
            else:
                source_records = {}
                for source_index, source in enumerate(sources):
                    source_where = f'{where}.provenance.calibration_sources[{source_index}]'
                    try:
                        evidence = _calibration(source)
                        _calibration_source(evidence, base_dir, source_where, messages.error)
                        if normalized is not None and not _calibration_module().matching_calibration_transform(normalized, evidence):
                            messages.error(f'{source_where}: calibration source defines a different map transform')
                        key = (evidence['source_path'], evidence['source_sha256'])
                        previous = source_records.get(key)
                        if previous is not None and previous != evidence:
                            messages.error(f'{source_where}: conflicting evidence for the same source')
                        source_records[key] = evidence
                    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
                        messages.error(f'{source_where}: {str(exc)[:180]}')
        if proof is not None and base_dir is None:
            messages.warn(f'{where}.calibration: no base_dir; source file existence/hash not verified')
        if not calibrated:
            registration = normalized is not None and normalized.get('basis') == 'export_registration'
            (messages.error if require_calibration else messages.warn)(
                f'{where}: asset registration verified; physical metre scale unverified' if registration and source_verified
                else f'{where}: asset registration fitted; bundle not checked; physical metre scale unverified' if registration
                else f'{where}: map calibration not verified (synthetic evidence is not real calibration)')
    if assets or any(isinstance(m.get('properties'),dict) and m['properties'].get('asset_placement_id') for m in markers):
        try:
            try:from .asset_catalog import validate_placement_records
            except ImportError:from asset_catalog import validate_placement_records
            for error in validate_placement_records(assets,maps,markers,verify_files=base_dir is not None):messages.error(error)
            if base_dir is None and assets:messages.warn('asset_placements: source hashes not checked without base_dir')
        except (OSError,ValueError,TypeError,KeyError,ImportError) as exc:
            messages.error('asset_placements: '+str(exc)[:180])
    for index, node in enumerate(nodes):
        owner = node.get('owner_map')
        if owner is not None and (not _uid(owner) or owner not in mapids):
            messages.error(f'nav.nodes[{index}].owner_map: unknown map instance')
    directed = {uid: set() for uid in nodeids}
    weak = {uid: set() for uid in nodeids}
    pending = 0
    pairs = set()
    for index, edge in enumerate(edges):
        where = f'nav.edges[{index}]'
        a, b = edge.get('from'), edge.get('to')
        valid_refs = _uid(a) and _uid(b) and a in nodeids and b in nodeids
        if not valid_refs:
            messages.error(f'{where}: broken node reference (must reference nav.nodes only)')
        elif a == b:
            messages.error(f'{where}: self loop')
        else:
            if edge.get('enabled', True) is not False:
                directed[a].add(b)
                weak[a].add(b)
                weak[b].add(a)
            pair = (a, b, str(edge.get('kind')))
            if pair in pairs:
                messages.warn(f'{where}: duplicate directed endpoint/kind pair')
            pairs.add(pair)
        status = edge.get('status')
        if 'enabled' in edge and not isinstance(edge['enabled'], bool):
            messages.error(f'{where}.enabled: expected boolean')
        if 'condition' in edge and not isinstance(edge['condition'], str):
            messages.error(f'{where}.condition: expected string')
        if status != 'runtime_verified':
            pending += 1
        if status in ('geometry_checked', 'runtime_verified'):
            proof = edge.get('evidence')
            required = ('method', 'source_path', 'source_sha256', 'checked_at')
            if not isinstance(proof, dict) or any(not isinstance(proof.get(k), str) or not proof[k] for k in required):
                messages.error(f'{where}.evidence: checked status requires method/source_path/source_sha256/checked_at')
            elif not _HASH.fullmatch(proof['source_sha256']) or proof.get('synthetic') is not False:
                messages.error(f'{where}.evidence: non-synthetic evidence with SHA-256 required')
            else:
                if status == 'runtime_verified' and not _uid(proof.get('runtime_build')):
                    messages.error(f'{where}.evidence.runtime_build: runtime verification requires build identity')
                if base_dir is not None:
                    _file_reference({'path': proof['source_path'], 'sha256': proof['source_sha256']},
                                    where + '.evidence', base_dir, messages.error)
        if valid_refs and owners.get(a) != owners.get(b) and status == 'proposed':
            messages.warn(f'{where}: proposed cross-map link; no collision/navigation proof')
    components = _components(weak)
    strong = _strong_components(directed)
    if len(components) > 1:
        messages.warn(f'{len(components)} weak graph components (largest sizes {components[:10]}); inspect intended islands')
    if len(strong) > len(components):
        messages.warn(f'{len(strong)} directed strongly connected components; weak connectivity does not imply return routes')
    if pending:
        messages.warn('Navigation contains non-runtime-verified edges: NOT proof of walkability')
    if edges:
        messages.warn('Static evidence/status checks do not execute collision traces or runtime navigation')
    for index, record in enumerate(geometry):
        _file_reference(record, f'geometry[{index}]', base_dir, messages.error, '.glb')
        bounds = record.get('bounds_m')
        if bounds is not None and isinstance(bounds, list) and len(bounds) not in (4, 6):
            messages.error(f'geometry[{index}].bounds_m: expected 4 or 6 coordinates')
        connector = record.get('connector')
        if isinstance(connector, dict):
            for segment_index, segment in enumerate(_records(connector.get('segments'))):
                where = f'geometry[{index}].connector.segments[{segment_index}]'
                uid = segment.get('id')
                if _uid(uid):
                    if uid in ids:
                        messages.error(f'{where}.id: duplicate global ID {uid[:80]}')
                    ids.add(uid)
                for key in ('from_map', 'to_map'):
                    owner = segment.get(key)
                    if not _uid(owner) or owner not in mapids:
                        messages.error(f'{where}.{key}: unknown map instance')
                if segment.get('from_map') == segment.get('to_map'):
                    messages.error(f'{where}: connector endpoints must be different map instances')
                if segment.get('status') != 'proposed':
                    messages.error(f'{where}.status: connector remains proposed until a separate integration gate')
                if 'kind' in segment and segment['kind'] not in ('road', 'bridge', 'corridor', 'tunnel', 'building', 'curve_road'):
                    messages.error(f'{where}.kind: unsupported connector kind')
                portal_keys = ('from_portal', 'to_portal')
                if any(key in segment for key in portal_keys):
                    node_lookup = {node['id']: node for node in nodes if _uid(node.get('id'))}
                    for portal_key, map_key in zip(portal_keys, ('from_map', 'to_map')):
                        portal_id = segment.get(portal_key)
                        node = node_lookup.get(portal_id) if _uid(portal_id) else None
                        if node is None:
                            messages.error(f'{where}.{portal_key}: existing nav-node ID required; declare both portal endpoints')
                            continue
                        properties = node.get('properties')
                        if not isinstance(properties, dict) or properties.get('is_portal') is not True:
                            messages.error(f'{where}.{portal_key}: node requires explicit properties.is_portal true')
                        if node.get('owner_map') != segment.get(map_key):
                            messages.error(f'{where}.{portal_key}: portal owner_map disagrees with {map_key}')
                    if segment.get('from_portal') == segment.get('to_portal'):
                        messages.error(f'{where}: connector portal endpoints must be different nodes')
    tactical = world.get('tactical_map')
    if isinstance(tactical, dict) and tactical.get('image') is not None and tactical.get('status') != 'generated':
        messages.error('tactical_map.image: image requires generated tactical map')
    if isinstance(tactical, dict) and tactical.get('status') == 'generated':
        for key in ('path', 'sha256', 'width_px', 'height_px', 'pixel_to_world', 'bounds_m', 'layers'):
            if key not in tactical:
                messages.error(f'tactical_map.{key}: generated map requires field')
        _file_reference(tactical, 'tactical_map', base_dir, messages.error)
        bounds = tactical.get('bounds_m')
        if isinstance(bounds, list) and len(bounds) == 4 and all(_number(x) for x in bounds):
            if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                messages.error('tactical_map.bounds_m: bounds must have positive width and height')
        transform = tactical.get('pixel_to_world')
        if isinstance(transform, list) and len(transform) == 6 and all(_number(x) for x in transform):
            if abs(transform[0] * transform[4] - transform[1] * transform[3]) < 1e-20:
                messages.error('tactical_map.pixel_to_world: singular affine transform')
        image = tactical.get('image')
        if image is not None:
            try:
                helper = _calibration_module()._source_bundle_module()
                size = (tactical.get('width_px'), tactical.get('height_px'))
                helper.validate_orthographic_image(image, bounds, size)
                if base_dir is not None:
                    helper.verify_orthographic_image(image, base_dir, bounds, size)
                if tactical.get('depiction') != 'schematic' or 'preview_image' not in tactical.get('layers', []):
                    messages.error('tactical_map.image: schematic depiction and preview_image layer required')
                x0, y0, x1, y1 = bounds
                expected = [(x1-x0)/size[0], 0, x0, 0, -(y1-y0)/size[1], y1]
                if not isinstance(transform, list) or len(transform) != 6 or any(
                        not _number(a) or not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)
                        for a, b in zip(transform or [], expected)):
                    messages.error('tactical_map.image: pixel_to_world disagrees with image bounds/viewport')
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, ArithmeticError) as exc:
                messages.error(f'tactical_map.image: {str(exc)[:180]}')
    if base_dir is None and (geometry or isinstance(tactical, dict) and tactical.get('status') == 'generated'):
        messages.warn('No base_dir supplied: referenced file existence and hashes were not verified')
    stats.update(maps=len(maps), markers=len(markers), geometry=len(geometry),
                 nav_nodes=len(nodes), nav_edges=len(edges), nav_components=len(components),
                 nav_strong_components=len(strong), graph_largest_component=components[0] if components else 0,
                 graph_largest_strong_component=strong[0] if strong else 0,
                 unverified_nav_edges=pending)
    try:
        diagnostics = diagnose(world, diagnostic_limits)
        for warning in diagnostics['warnings']:
            messages.warn(warning)
        messages.warning_count += diagnostics['warnings_truncated']
        stats.update(nav_enabled_edges=diagnostics['enabled_edges'],
                     nav_disabled_edges=diagnostics['disabled_edges'],
                     nav_portals=diagnostics['portal_count'],
                     nav_unconnected_portals=diagnostics['unconnected_portal_count'])
    except ValueError as exc:
        messages.error(str(exc))
    return finish()
