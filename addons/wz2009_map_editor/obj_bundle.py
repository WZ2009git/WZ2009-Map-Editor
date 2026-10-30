"""Read-only bindings for local Wavefront OBJ, MTL and referenced textures.

This does not run C2M, import a mesh, infer source units or validate collision.
Unsupported texture options and external/network paths fail explicitly.
"""
import importlib.util
import math
from pathlib import Path
import shlex

try:
    from .source_bundle import sha256_file, verify_file
except ImportError:
    # validation.py is also loaded by file specification from the CLI. In that
    # case this module has neither a package nor an add-on directory on sys.path.
    # Resolve the actual sibling instead of relying on CWD or a same-named
    # globally installed module.
    _spec = importlib.util.spec_from_file_location(
        '_wz09_obj_source_bundle', Path(__file__).with_name('source_bundle.py'))
    _source_bundle = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_source_bundle)
    sha256_file = _source_bundle.sha256_file
    verify_file = _source_bundle.verify_file


_ONE = {'-blendu', '-blendv', '-boost', '-texres', '-clamp', '-bm',
        '-imfchan', '-type', '-colorspace', '-cc'}
_VECTOR = {'-o', '-s', '-t'}
_TEXTURES = {'bump', 'disp', 'decal', 'refl', 'norm'}


def _local_path(name, directory, base, label):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(char) < 32 for char in name)
            or Path(name).is_absolute()):
        raise ValueError(f'{label}: only local relative resource paths inside the OBJ directory are supported')
    path = (directory/name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError(f'{label}: missing resource or path escapes OBJ directory: {name}')
    return path


def _records(path):
    if path.stat().st_size > 512*1024*1024:
        raise ValueError(f'OBJ bundle text exceeds 512 MiB limit: {path}')
    with path.open('r', encoding='utf-8-sig', errors='strict') as stream:
        for number, line in enumerate(stream, 1):
            fields = line.lstrip().split(None, 1)
            if not fields or fields[0].startswith('#'):
                continue
            yield number, fields[0].lower(), fields[1].strip() if len(fields) > 1 else ''


def _tokens(text, label):
    if '\\' in text:
        raise ValueError(f'{label}: use forward slashes; escaped/backslash references are unsupported')
    try:
        return shlex.split(text, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError(f'{label}: invalid quoted resource path') from exc


def _texture_name(tokens, label):
    index = 0
    while index < len(tokens) and tokens[index].startswith('-'):
        option = tokens[index].lower()
        index += 1
        if option in _ONE or option == '-mm':
            count = 2 if option == '-mm' else 1
            if index+count >= len(tokens):
                raise ValueError(f'{label}: missing {option} value or texture path')
            index += count
        elif option in _VECTOR:
            count = 0
            while index < len(tokens) and count < 3:
                try:
                    value = float(tokens[index])
                except ValueError:
                    break
                if not math.isfinite(value):
                    raise ValueError(f'{label}: nonfinite texture option')
                index += 1
                count += 1
            if not count:
                raise ValueError(f'{label}: numeric {option} value required')
        else:
            raise ValueError(f'{label}: unsupported texture option {option}')
    if index >= len(tokens):
        raise ValueError(f'{label}: texture path required')
    # Wavefront texture commands reference a single filename. Remaining words
    # can belong to an unquoted filename containing spaces; they cannot launch
    # commands and are still resolved strictly inside the OBJ resource folder.
    return ' '.join(tokens[index:])


def _targets(reference):
    base = reference.parent
    materials = set()
    for number, kind, text in _records(reference):
        if kind != 'mtllib':
            continue
        label = f'{reference}:{number}'
        names = _tokens(text, label)
        if not names:
            raise ValueError(f'{label}: material library path required')
        for name in names:
            path = _local_path(name, base, base, label)
            if path.suffix.lower() != '.mtl':
                raise ValueError(f'{label}: expected a local .mtl material library')
            materials.add(path)
        if len(materials) > 4096:
            raise ValueError('OBJ bundle contains more than 4096 material libraries')
    targets = set(materials)
    for material in sorted(materials):
        for number, kind, text in _records(material):
            if not kind.startswith('map_') and kind not in _TEXTURES:
                continue
            label = f'{material}:{number}'
            filename = _texture_name(_tokens(text, label), label)
            targets.add(_local_path(filename, material.parent, base, label))
            if len(targets) > 8192:
                raise ValueError('OBJ bundle exceeds 8192 material/texture resources')
    if reference in targets:
        raise ValueError('OBJ bundle cannot refer back to its own OBJ as a material/texture')
    return targets


def capture_obj_bundle(reference_path):
    """Capture the complete supported local OBJ/MTL/texture dependency set."""
    reference = Path(reference_path).expanduser().resolve()
    if reference.suffix.lower() != '.obj' or not reference.is_file():
        raise ValueError('OBJ preview requires an existing local .obj file')
    digest = sha256_file(reference)
    resources = [{'path': str(path), 'sha256': sha256_file(path)}
                 for path in sorted(_targets(reference))]
    if sha256_file(reference) != digest:
        raise ValueError('OBJ source changed while capturing its resource bindings')
    bundle = {'reference_path': str(reference), 'reference_sha256': digest,
              'resources': resources}
    verify_obj_bundle(bundle)
    return bundle


def verify_obj_bundle(bundle):
    """Require exact, absolute SHA-bound resources; never access the network."""
    if not isinstance(bundle, dict) or set(bundle) != {'reference_path', 'reference_sha256', 'resources'}:
        raise ValueError('OBJ bundle requires only reference_path/reference_sha256/resources')
    reference = verify_file(bundle['reference_path'], bundle['reference_sha256'], 'OBJ reference')
    if reference.suffix.lower() != '.obj':
        raise ValueError('OBJ bundle reference requires .obj format')
    if not isinstance(bundle['resources'], list):
        raise ValueError('OBJ bundle resources must be an array')
    expected, declared = _targets(reference), set()
    for item in bundle['resources']:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('OBJ bundle resource requires only path/sha256')
        path = verify_file(item['path'], item['sha256'], 'OBJ resource')
        if not path.is_relative_to(reference.parent) or path == reference or path in declared:
            raise ValueError('OBJ bundle resource is duplicated or outside its resource folder')
        declared.add(path)
    if declared != expected:
        raise ValueError('OBJ bundle must bind exactly all referenced material libraries and textures')
    return reference
