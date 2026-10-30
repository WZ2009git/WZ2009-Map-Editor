"""Hash-bound, read-only catalog of native model meshes in local glTF previews.

Model names are source metadata, never editor identities.  World geometry and
generic glTF meshes without the exporter's explicit model metadata are excluded.
Importing this module does not import Blender or read a game installation.
"""
import copy
import json
import math
import re
import uuid
try:
    from .source_bundle import verify_preview_bundle
except ImportError:
    from source_bundle import verify_preview_bundle


ASSET_NAMESPACE = uuid.UUID('95381a24-93f0-456a-9040-1256fbc3f1ad')
CATEGORIES = (
    ('vegetation', 'Vegetation', 'OUTLINER_OB_CURVES'),
    ('containers', 'Crates and containers', 'PACKAGE'),
    ('vehicles', 'Fahrzeuge', 'AUTO'),
    ('rocks', 'Felsen', 'MESH_ICOSPHERE'),
    ('structures', 'Bauteile', 'MESH_CUBE'),
    ('furniture', 'Furniture', 'OUTLINER_OB_MESH'),
    ('props', 'Requisiten', 'OBJECT_DATA'),
)
_SHA = re.compile(r'^[0-9a-f]{64}$')


def category_for_model(model):
    """Advisory search tags based on the retained original model name only."""
    text = model.casefold()
    if any(token in text for token in ('foliage', 'shrub', 'grass', 'reeds', 'tree', 'bush')):
        return 'vegetation'
    if any(token in text for token in ('crate', 'barrel', 'container', 'canister', 'gas_can', 'dumpster')):
        return 'containers'
    if any(token in text for token in ('vehicle_', 'truck', 'tank_d_', 'jeep', 'zodiac')):
        return 'vehicles'
    if any(token in text for token in ('rock', 'boulder')):
        return 'rocks'
    if any(token in text for token in ('furniture', 'chair', 'desk', 'bench', 'table')):
        return 'furniture'
    if any(token in text for token in ('pipe', 'barrier', 'metal', 'antenna', 'door', 'support_ring', 'woodlog')):
        return 'structures'
    return 'props'


def asset_id(source_map, reference_sha256, mesh_index, model):
    # Legacy field name: common_mp is a shared native zone, never a world map.
    if not isinstance(source_map, str) or not (source_map.startswith('mp_') or source_map=='common_mp'):
        raise ValueError('Asset source requires an explicit original map or common_mp zone codename')
    if not isinstance(reference_sha256, str) or not _SHA.fullmatch(reference_sha256):
        raise ValueError('Asset source requires a lowercase SHA-256')
    if type(mesh_index) is not int or mesh_index < 0:
        raise ValueError('Asset source requires a nonnegative mesh index')
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError('Asset model name must contain 1–512 characters')
    key = json.dumps([source_map, reference_sha256, mesh_index, model], ensure_ascii=False,
                     separators=(',', ':'))
    return str(uuid.uuid5(ASSET_NAMESPACE, key))


def catalog_from_document(document, bundle, source_map):
    """Build metadata after a caller verified the glTF bundle; pure for tests.

    The public build_catalog performs the mandatory byte/hash checks. This
    helper never promotes a glTF mesh name to proof of a native model.
    """
    if not isinstance(bundle, dict) or not _SHA.fullmatch(str(bundle.get('reference_sha256', ''))):
        raise ValueError('Verified preview bundle metadata required')
    if (not isinstance(document, dict) or not isinstance(document.get('asset'), dict)
            or document['asset'].get('version') != '2.0'):
        raise ValueError('Asset catalog requires a glTF 2.0 document')
    meshes, nodes = document.get('meshes', []), document.get('nodes', [])
    if not isinstance(meshes, list) or not isinstance(nodes, list):
        raise ValueError('glTF meshes/nodes must be arrays')
    models = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError('glTF node must be an object')
        extras = node.get('extras', {})
        metadata = extras.get('iw4l', {}) if isinstance(extras, dict) else {}
        if not isinstance(metadata, dict) or 'model' not in metadata:
            continue
        model, mesh_index = metadata['model'], node.get('mesh')
        if (not isinstance(model, str) or not model.strip() or model.casefold() in {'world', 'gfxworld'}
                or type(mesh_index) is not int or not 0 <= mesh_index < len(meshes)):
            raise ValueError(f'Native model node {index}: invalid model/mesh metadata')
        mesh = meshes[mesh_index]
        if not isinstance(mesh, dict) or not isinstance(mesh.get('primitives'), list) or not mesh['primitives']:
            raise ValueError(f'Native model mesh {mesh_index}: render primitives required')
        if mesh.get('name') != model:
            raise ValueError(f'Native model mesh {mesh_index}: mesh/model metadata disagree')
        if mesh_index in models and models[mesh_index]['model'] != model:
            raise ValueError(f'Native model mesh {mesh_index}: conflicting model identities')
        entry = models.setdefault(mesh_index, {
            'id': asset_id(source_map, bundle['reference_sha256'], mesh_index, model),
            'name': model, 'model': model, 'source_map': source_map,
            'category': category_for_model(model), 'mesh_index': mesh_index,
            'source_sha256': bundle['reference_sha256'],
            'reference_path': bundle['reference_path'], 'source_node_indices': [],
            'primitive_count': len(mesh['primitives']), 'physical_scale_verified': False,
            'coordinate_basis': 'imported_glTF_Blender_metres_export_convention',
        })
        entry['source_node_indices'].append(index)
    return {'kind': 'native_preview_model_catalog', 'version': 1,
            'sources': {bundle['reference_sha256']: {
                'source_map': source_map, 'preview_bundle': copy.deepcopy(bundle)}},
            'assets': sorted(models.values(), key=lambda item: (item['category'], item['name'], item['id'])),
            'collision_verified': False, 'runtime_integrated': False}


def build_catalog(bundle, source_map):
    """Verify all source buffers and textures before indexing original models."""
    document = verify_preview_bundle(bundle)
    return catalog_from_document(document, bundle, source_map)


def filter_assets(catalog, query='', category='ALL', source_map='ALL'):
    """Stable AND-token search over retained source names, map codes and tags."""
    if category != 'ALL' and category not in {item[0] for item in CATEGORIES}:
        raise ValueError('Unknown asset category')
    words = str(query).casefold().split()
    records = catalog.get('assets', []) if isinstance(catalog, dict) else []
    return [item for item in records if (category == 'ALL' or item['category'] == category)
            and (source_map == 'ALL' or item['source_map'] == source_map)
            and all(word in ' '.join((item['name'], item['source_map'], item['category'])).casefold()
                    for word in words)]


def validate_asset_record(record):
    """Check source identity without loading mutable Blender names or meshes."""
    if not isinstance(record, dict):
        raise ValueError('Asset metadata must be an object')
    expected = asset_id(record.get('source_map'), record.get('source_sha256'),
                        record.get('mesh_index'), record.get('model'))
    if record.get('id') != expected or record.get('name') != record.get('model'):
        raise ValueError('Asset identity does not match its hash-bound native model')
    if record.get('category') not in {item[0] for item in CATEGORIES}:
        raise ValueError('Unknown asset category')
    if record.get('physical_scale_verified') is not False:
        raise ValueError('Preview model does not prove physical metre calibration')
    nodes = record.get('source_node_indices')
    if (not isinstance(nodes, list) or not nodes or any(type(index) is not int or index < 0 for index in nodes)
            or len(set(nodes)) != len(nodes)):
        raise ValueError('Asset requires unique nonnegative original node indices')
    if type(record.get('primitive_count')) is not int or record['primitive_count'] < 1:
        raise ValueError('Asset requires a positive source primitive count')
    if not isinstance(record.get('reference_path'), str) or not record['reference_path'].strip():
        raise ValueError('Asset requires its original preview reference path')
    return record


def validate_placement_records(records, maps, markers, verify_files=False):
    """Semantic checks for optional visual-only manifest placements.

    Return compact path-prefixed errors, never mutate records or source files.
    File verification also binds each model/index to the actual glTF metadata.
    The caller supplies its already parsed map/marker arrays.
    """
    errors = []
    if not isinstance(records, list):
        return ['asset_placements: array required']
    maps_by_id = {item.get('id'): item for item in maps if isinstance(item, dict) and isinstance(item.get('id'), str)}
    markers_by_id = {item.get('id'): item for item in markers if isinstance(item, dict) and isinstance(item.get('id'), str)}
    ids, linked_markers, verified = set(), {}, {}
    for index, record in enumerate(records):
        field = f'asset_placements[{index}]'
        if not isinstance(record, dict):
            errors.append(field+': object required')
            continue
        uid = record.get('id')
        if (not isinstance(uid, str) or not uid.strip() or uid in ids
                or uid in maps_by_id or uid in markers_by_id):
            errors.append(field+'.id: unique persistent placement ID required')
        else:
            ids.add(uid)
        owner = record.get('owner_map')
        if not isinstance(owner, str) or owner not in maps_by_id:
            errors.append(field+'.owner_map: existing map required')
        for coordinate in ('position_m', 'rotation_deg', 'scale', 'origin_offset_m'):
            value = record.get(coordinate)
            if (not isinstance(value, (list, tuple)) or len(value) != 3
                    or any(type(component) not in (int, float) or not math.isfinite(component) for component in value)
                    or coordinate == 'scale' and any(component <= 0 for component in value)):
                errors.append(field+'.'+coordinate+': three finite'+(' positive' if coordinate == 'scale' else '')+' numbers required')
        if record.get('collision_verified') is not False or record.get('runtime_integrated') is not False:
            errors.append(field+': visual assets cannot imply collision/runtime verification')
        try:
            source = validate_asset_record(record.get('source'))
            if record.get('asset_id') != source['id']:
                raise ValueError('asset_id does not match source identity')
            bundle = record.get('preview_bundle')
            if (not isinstance(bundle, dict) or set(bundle) != {'reference_path', 'reference_sha256', 'resources'}
                    or bundle['reference_sha256'] != source['source_sha256']
                    or bundle['reference_path'] != source['reference_path']):
                raise ValueError('preview_bundle does not match asset source')
            if verify_files:
                cache_key = _source_key(bundle, source['source_map'])
                if cache_key not in verified:
                    document = verify_preview_bundle(bundle)
                    catalog = catalog_from_document(document, bundle, source['source_map'])
                    verified[cache_key] = {asset['id']: asset for asset in catalog['assets']}
                bound = verified[cache_key].get(source['id'])
                if bound is None or any(source.get(key) != bound.get(key) for key in bound):
                    raise ValueError('model metadata differs from actual hash-bound glTF')
        except (OSError, TypeError, ValueError, KeyError) as exc:
            errors.append(field+'.source: '+str(exc))
        marker_id = record.get('marker_id')
        if marker_id is not None:
            marker = markers_by_id.get(marker_id) if isinstance(marker_id, str) else None
            if marker is None:
                errors.append(field+'.marker_id: existing gameplay marker required')
            elif (not isinstance(marker.get('properties'), dict)
                  or marker['properties'].get('asset_placement_id') != uid):
                errors.append(field+'.marker_id: marker must reciprocally reference placement')
            elif marker.get('kind') not in {'loot_crate', 'buy_station', 'player_spawn', 'poi'}:
                errors.append(field+'.marker_id: unsupported linked marker kind')
            elif marker.get('properties', {}).get('editor_owner_map') != owner:
                errors.append(field+'.marker_id: linked marker owner must match placement')
            if isinstance(marker_id, str) and marker_id in linked_markers:
                errors.append(field+'.marker_id: marker is already linked to another placement')
            if isinstance(marker_id, str):
                linked_markers[marker_id] = uid
    for marker in markers_by_id.values():
        properties = marker.get('properties', {})
        if isinstance(properties, dict) and 'asset_placement_id' in properties:
            target = properties['asset_placement_id']
            if not isinstance(target, str) or target not in ids or linked_markers.get(marker.get('id')) != target:
                errors.append(f'markers[{marker.get("id")}].properties.asset_placement_id: reciprocal placement link required')
    return errors


def _source_key(bundle, source_map):
    return (source_map, json.dumps(bundle, sort_keys=True, ensure_ascii=False, allow_nan=False))
