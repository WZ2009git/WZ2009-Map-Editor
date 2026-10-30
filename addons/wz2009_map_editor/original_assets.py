"""Resolve portable native FF requirements against a user's local MW2 install.

Requirements contain map codes and original FF SHA-256 hashes only. Preview
glTF hashes are not original-asset fingerprints. No game files are copied or
modified, and no Blender or game runtime is imported here.
"""
import hashlib
from pathlib import Path
import re


_MAP = re.compile(r'[a-z0-9_]+', re.ASCII)
_HASH = re.compile(r'[0-9a-f]{64}', re.ASCII)


class OriginalAssetMissing(FileNotFoundError):
    """The requested native FF is absent from the installation."""


class OriginalAssetMismatch(ValueError):
    """Present FF candidates do not match the required original revision."""


def source_requirement(source_map, native_sha256):
    """Return a validated portable descriptor; never accepts filesystem paths."""
    if (not isinstance(source_map, str) or not _MAP.fullmatch(source_map)
            or len(source_map) > 128):
        raise ValueError('Original source_map must be a safe lowercase native map code')
    if not isinstance(native_sha256, str) or not _HASH.fullmatch(native_sha256):
        raise ValueError('Original native_sha256 must be a lowercase FF SHA-256')
    return {'source_map': source_map, 'native_sha256': native_sha256}


def _requirement(requirement):
    if not isinstance(requirement, dict) or set(requirement) != {'source_map', 'native_sha256'}:
        raise ValueError('Original requirement must contain only source_map and native_sha256')
    return source_requirement(requirement['source_map'], requirement['native_sha256'])


def _contained(path, root):
    target = path.resolve()
    if not target.is_relative_to(root):
        raise ValueError('Original asset candidate escapes the selected installation')
    return target


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def resolve_original(install_root, requirement):
    """Return the matching local Path, or a missing/mismatch/containment error.

    Only zone/<map>.ff and zone/<language>/<map>.ff are considered. Locale
    order is deterministic; all candidate paths are checked before any hash.
    Paths returned here are local results, never part of portable descriptors.
    """
    required = _requirement(requirement)
    root = Path(install_root).expanduser().resolve()
    if not root.is_dir():
        raise OriginalAssetMissing('Original game installation directory is missing')
    zone = _contained(root / 'zone', root)
    if not zone.is_dir():
        raise OriginalAssetMissing('Original installation has no zone directory')
    filename = required['source_map'] + '.ff'
    candidates = [zone / filename]
    for language in sorted(zone.iterdir(), key=lambda p: p.name.casefold()):
        if language.is_dir():
            candidates.append(language / filename)
    present = []
    seen = set()
    for path in candidates:
        target = _contained(path, root)
        if target.is_file() and target not in seen:
            present.append(target)
            seen.add(target)
    if not present:
        raise OriginalAssetMissing('Original asset missing: ' + filename)
    for path in present:
        if _sha256(path) == required['native_sha256']:
            return path
    raise OriginalAssetMismatch('Incompatible original asset edition/hash: ' + filename)


def verify_requirements(install_root, requirements):
    """Return {source_map: local Path}; reject invalid or duplicate requirements.

    A failed verification raises without returning a partial success mapping.
    This validates original source availability; it does not import geometry or
    assert that runtime consumers support the exported edits.
    """
    if not isinstance(requirements, list):
        raise ValueError('Original sources must be an array')
    validated = [_requirement(item) for item in requirements]
    names = [item['source_map'] for item in validated]
    if len(set(names)) != len(names):
        raise ValueError('Original sources contain duplicate source_map requirements')
    return {item['source_map']: resolve_original(install_root, item) for item in validated}
