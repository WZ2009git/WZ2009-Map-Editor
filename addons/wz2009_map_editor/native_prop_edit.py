"""Verify rigid native prop edits without treating Blender names as identities.

Only the root transform may change after capture. A reference-only edit cannot
represent modified mesh data, missing pieces or independently moved children.
"""
import math
import uuid


PART_ID = 'wz09_native_edit_part_id'


def _metadata(obj):
    value = obj.get('iw4l')
    if value is None or not hasattr(value, 'get'):
        raise ValueError('Original prop requires native model and authored slot metadata')
    slot, model = value.get('authored_slot'), value.get('model')
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError('Original prop authored slot must be a nonnegative integer')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Original prop requires a nonempty native model name')
    return {'authored_slot': slot, 'model': model}


def _matrix(value):
    result = [float(element) for row in value for element in row]
    if len(result) != 16 or not all(math.isfinite(element) for element in result):
        raise ValueError('Original prop part requires a finite 4 by 4 matrix')
    return result


def _nodes(root):
    result, seen, pending = [], set(), [(root, None)]
    while pending:
        node, parent = pending.pop()
        # Object identity is only a traversal guard; saved identities are UUIDs.
        key = node.as_pointer() if hasattr(node, 'as_pointer') else id(node)
        if key in seen:
            raise ValueError('Original prop hierarchy contains a cycle or duplicate node')
        seen.add(key)
        if node.get('wz09_compacted_source_parts') or node.get('wz09_compact_records_json'):
            raise ValueError('Original prop is compacted or incomplete; unpack its complete model first')
        if node.type not in {'MESH', 'EMPTY'}:
            raise ValueError('Original prop reference edits require static mesh or empty nodes')
        if node.modifiers or getattr(node, 'constraints', ()):
            raise ValueError('Original prop reference edits cannot contain modifiers or constraints')
        if getattr(node, 'animation_data', None) is not None:
            raise ValueError('Original prop reference edits cannot contain animation or drivers')
        if node.type == 'MESH':
            if getattr(node.data, 'shape_keys', None) is not None:
                raise ValueError('Original prop reference edits cannot contain shape keys')
            if getattr(node.data, 'animation_data', None) is not None:
                raise ValueError('Original prop mesh cannot contain animation or drivers')
        result.append((node, parent))
        for child in node.children:
            if child.parent != node:
                raise ValueError('Original prop hierarchy has inconsistent parenting')
            pending.append((child, node))
    if not any(node.type == 'MESH' and len(node.data.polygons) for node, _ in result):
        raise ValueError('Original prop has no complete loaded mesh geometry')
    return result


def _identity(value):
    if not isinstance(value, str):
        raise ValueError('Original prop part identity must be a UUID')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError('Original prop part identity must be a UUID') from exc
    if str(parsed) != value:
        raise ValueError('Original prop part identity must be a canonical UUID')
    return value


def _proof(root, nodes, identities, mesh_digest):
    records = []
    for node, parent in nodes:
        key = node.as_pointer() if hasattr(node, 'as_pointer') else id(node)
        parent_key = (parent.as_pointer() if hasattr(parent, 'as_pointer') else id(parent)) if parent is not None else None
        row = {'id': identities[key], 'parent': identities[parent_key] if parent_key is not None else None,
               'kind': node.type, 'geometry_sha256': mesh_digest(node.data) if node.type == 'MESH' else None}
        # Root world placement is editable. Descendant transforms and inverse
        # parenting are geometry assembly and must remain unchanged.
        if node is not root:
            row['matrix_basis'] = _matrix(node.matrix_basis)
            row['matrix_parent_inverse'] = _matrix(node.matrix_parent_inverse)
        records.append(row)
    return {'version': 1, 'native': _metadata(root), 'parts': sorted(records, key=lambda row: row['id'])}


def capture_model(obj, mesh_digest):
    """Capture a JSON-safe rigid-prop baseline; add persistent IDs after validation."""
    _metadata(obj)
    nodes = _nodes(obj)
    identities, used = {}, set()
    for node, _ in nodes:
        identity = _identity(node[PART_ID]) if node.get(PART_ID) is not None else str(uuid.uuid4())
        if identity in used:
            raise ValueError('Original prop contains duplicated persistent part identities')
        used.add(identity)
        identities[node.as_pointer() if hasattr(node, 'as_pointer') else id(node)] = identity
    result = _proof(obj, nodes, identities, mesh_digest)
    for node, _ in nodes:
        node[PART_ID] = identities[node.as_pointer() if hasattr(node, 'as_pointer') else id(node)]
    return result


def verify_model(obj, baseline, mesh_digest):
    """Raise when a tracked prop differs beyond its allowed root transform."""
    if not isinstance(baseline, dict) or baseline.get('version') != 1:
        raise ValueError('Original prop has no supported captured hierarchy proof')
    nodes = _nodes(obj)
    identities, used = {}, set()
    for node, _ in nodes:
        identity = _identity(node.get(PART_ID))
        if identity in used:
            raise ValueError('Original prop contains duplicated persistent part identities')
        used.add(identity)
        identities[node.as_pointer() if hasattr(node, 'as_pointer') else id(node)] = identity
    if _proof(obj, nodes, identities, mesh_digest) != baseline:
        raise ValueError('Original prop mesh, assembly or hierarchy changed; only the root transform is supported')
