"""Explicit, undoable authoring helpers; importing this module needs no Blender.

The UI operators provide Blender's UNDO boundary. No helper repairs IDs or drops
edges as a side effect of export. Mesh and material data are never edited here.
"""
import json
import uuid


_EDGE_KEY = 'wz09_edges_json'


def _api(bpy_module):
    if bpy_module is None:
        import bpy
        return bpy
    return bpy_module


def _key(item):
    return item.as_pointer() if hasattr(item, 'as_pointer') else id(item)


def _same(item, items):
    return any(_key(item) == _key(candidate) for candidate in items)


def _uid(obj):
    value = obj.get('wz09_id', '')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Authored object requires a persistent wz09_id')
    return value


def _edges(scene):
    raw = scene.get(_EDGE_KEY, '[]')
    try:
        records = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError('Nonfinite edge JSON')))
    except (TypeError, ValueError) as exc:
        raise ValueError('wz09_edges_json: invalid JSON') from exc
    if not isinstance(records, list):
        raise ValueError('wz09_edges_json: expected a list')
    for edge in records:
        if not isinstance(edge, dict) or any(
                not isinstance(edge.get(key), str) or not edge[key].strip()
                for key in ('id', 'from', 'to')):
            raise ValueError('Every edge requires nonempty id/from/to strings')
    return records


def _encoded(edges):
    return json.dumps(edges, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _nav_ids(scene):
    return {_uid(obj) for obj in scene.objects if obj.get('wz09_type') == 'nav_node'}


def prune_orphan_edges(scene):
    """Explicitly remove edges whose endpoints no longer identify scene nav nodes.

    Surviving dictionaries, including UUIDs and extra evidence, are retained. A
    malformed edge document is rejected before any mutation.
    """
    records = _edges(scene)
    ids = _nav_ids(scene)
    kept = [edge for edge in records if edge['from'] in ids and edge['to'] in ids]
    removed = len(records) - len(kept)
    if removed:
        scene[_EDGE_KEY] = _encoded(kept)
    return removed


def remove_nav_nodes(scene, nodes, *, bpy_module=None):
    """Delete an explicit scene-local nav selection and its incident stored edges.

    Edge validation and serialization finish before removal begins. Objects shared
    with another scene or from a linked library are rejected because Blender's
    global object removal would otherwise affect another scene. The calling UI
    operator must use UNDO for rollback of Blender-level deletion failures.
    """
    chosen = list(nodes)
    if not chosen:
        raise ValueError('Select at least one WZ09 nav node')
    scene_objects = list(scene.objects)
    seen = set()
    for obj in chosen:
        if not _same(obj, scene_objects) or obj.get('wz09_type') != 'nav_node':
            raise ValueError('Selection must contain only nav nodes in this scene')
        if _key(obj) in seen:
            raise ValueError('Nav selection contains the same object twice')
        seen.add(_key(obj))
        if getattr(obj, 'library', None) is not None or getattr(obj, 'override_library', None) is not None:
            raise ValueError('Linked or overridden nav objects cannot be removed here')
        if any(not _same(owner, (scene,)) for owner in getattr(obj, 'users_scene', (scene,))):
            raise ValueError('Nav object belongs to another scene; unlink it there first')
    chosen_ids = {_uid(obj) for obj in chosen}
    if any(_same(getattr(obj, 'parent', None), chosen) and not _same(obj, chosen)
           for obj in scene_objects):
        raise ValueError('Selected nav nodes have other child objects; reparent those before deletion')
    if any(obj.get('wz09_type') == 'nav_node' and str(obj.get('wz09_links', '')).strip()
           for obj in scene_objects):
        raise ValueError('Migrate legacy nav links before deleting nodes')
    if any(obj.get('wz09_type') == 'nav_node' and _uid(obj) in chosen_ids
           and not _same(obj, chosen) for obj in scene_objects):
        raise ValueError('Duplicate nav IDs must be repaired before deleting nodes')
    records = _edges(scene)
    kept = [edge for edge in records
            if edge['from'] not in chosen_ids and edge['to'] not in chosen_ids]
    payload = _encoded(kept)
    api = _api(bpy_module)
    previous = scene.get(_EDGE_KEY)
    existed = _EDGE_KEY in scene
    scene[_EDGE_KEY] = payload
    removed = 0
    try:
        for obj in chosen:
            api.data.objects.remove(obj, do_unlink=True)
            removed += 1
    except Exception:
        # No deletion occurred: restore the original exact property. If Blender
        # removed an earlier object, preserve the safe edge list and use UI UNDO.
        if not removed:
            if existed:
                scene[_EDGE_KEY] = previous
            else:
                del scene[_EDGE_KEY]
        raise
    return {'nodes_removed': removed, 'edges_removed': len(records) - len(kept)}


def _scene_collections(scene):
    result = []
    pending = [scene.collection]
    while pending:
        collection = pending.pop()
        if _same(collection, result):
            continue
        result.append(collection)
        pending.extend(collection.children)
    return result


def _hierarchy(scene, source):
    """Only descendants actually linked to this scene are part of the copy."""
    result = [source]
    pending = [source]
    objects = list(scene.objects)
    while pending:
        parent = pending.pop()
        for obj in objects:
            if _same(getattr(obj, 'parent', None), (parent,)):
                if _same(obj, result):
                    raise ValueError('Map hierarchy contains a parent cycle')
                if obj.get('wz09_type') == 'map_instance':
                    raise ValueError('Nested map instances must be separated before duplication')
                result.append(obj)
                pending.append(obj)
    return result


def duplicate_map_instance(context, source, *, bpy_module=None):
    """Copy a local map hierarchy, remapping authoring IDs and internal nav edges.

    Object.copy() keeps preview mesh/material data shared and preserves all flags.
    Authored geometry gets an independent mesh/curve datablock for safe editing.
    Only links into current-scene collections are created. Edges crossing the
    copied hierarchy boundary are never copied; originals remain byte-for-byte.
    Errors roll back newly created objects and leave original scene data intact.
    """
    scene = context.scene
    objects = list(scene.objects)
    if not _same(source, objects) or source.get('wz09_type') != 'map_instance':
        raise ValueError('Select a map instance in the current scene')
    if getattr(source, 'parent', None) is not None:
        raise ValueError('Map instance must be a root object before duplication')
    hierarchy = _hierarchy(scene, source)
    authored = [obj for obj in objects if obj.get('wz09_type')]
    original_ids = [_uid(obj) for obj in authored]
    if len(set(original_ids)) != len(original_ids):
        raise ValueError('Duplicate authoring IDs must be repaired before duplication')
    records = _edges(scene)
    nav_ids = _nav_ids(scene)
    edge_ids = [edge['id'] for edge in records]
    if len(set(edge_ids)) != len(edge_ids) or set(edge_ids) & set(original_ids):
        raise ValueError('Duplicate edge IDs must be repaired before duplication')
    if any(edge['from'] not in nav_ids or edge['to'] not in nav_ids for edge in records):
        raise ValueError('Repair orphan nav links before duplicating a map')
    for obj in hierarchy:
        if getattr(obj, 'library', None) is not None or getattr(obj, 'override_library', None) is not None:
            raise ValueError('Linked or overridden map objects cannot be duplicated here')
        if getattr(obj, 'constraints', ()):
            raise ValueError('Apply or remove object constraints before duplicating the map')
        if getattr(obj, 'animation_data', None) is not None:
            raise ValueError('Map duplication requires objects without animation or drivers')
        for modifier in getattr(obj, 'modifiers', ()):
            properties = getattr(getattr(modifier, 'bl_rna', None), 'properties', ())
            for prop in properties:
                if prop.identifier in {'rna_type', 'id_data'} or getattr(prop, 'type', '') != 'POINTER':
                    continue
                target_type = getattr(getattr(prop, 'fixed_type', None), 'identifier', '')
                if target_type in {'Object', 'Collection'} and getattr(modifier, prop.identifier, None) is not None:
                    raise ValueError('Apply or remove modifier object/collection references before duplicating the map')
        if obj.get('wz09_type') == 'nav_node' and str(obj.get('wz09_owner_map', '')) != _uid(source):
            raise ValueError('Map nav-node owner is inconsistent with the hierarchy')
        if obj.get('wz09_type') == 'nav_node' and str(obj.get('wz09_links', '')).strip():
            raise ValueError('Migrate legacy nav links before duplicating the map')
    collections = _scene_collections(scene)
    destinations = {}
    for obj in hierarchy:
        local = [collection for collection in obj.users_collection if _same(collection, collections)]
        if not local:
            raise ValueError('Map object has no collection in the current scene')
        destinations[_key(obj)] = local
    used_ids = set(original_ids) | set(edge_ids)
    for obj in objects:
        geometry_id = obj.get('wz09_geometry_id')
        if geometry_id:
            used_ids.add(str(geometry_id))
        for key in ('wz09_audio_id','wz09_action_id','wz09_region_id','wz09_ground_patch_id',
                    'wz09_asset_placement_id','wz09_preview_id'):
            if obj.get(key):used_ids.add(str(obj[key]))

    def fresh_id():
        value = str(uuid.uuid4())
        while value in used_ids:
            value = str(uuid.uuid4())
        used_ids.add(value)
        return value

    id_map = {_uid(obj): fresh_id() for obj in hierarchy if obj.get('wz09_type')}
    placements={obj.get('wz09_asset_placement_id'):fresh_id() for obj in hierarchy if obj.get('wz09_asset_placement')}
    extra_ids={}
    for obj in hierarchy:
        for flag,key in (('wz09_audio_zone','wz09_audio_id'),('wz09_action_element','wz09_action_id'),
                         ('wz09_region_kind','wz09_region_id'),('wz09_ground_patch','wz09_ground_patch_id')):
            if obj.get(flag) and obj.get(key):extra_ids[str(obj[key])]=fresh_id()
    routes={}
    for obj in hierarchy:
        if obj.get('wz09_type')=='aircraft_keyframe':
            props=json.loads(obj.get('wz09_properties_json','{}'))
            route=obj.get('wz09_route_id') or props.get('route_id')
            if route and route not in routes:routes[route]=fresh_id()
    copied_nav_ids = {_uid(obj) for obj in hierarchy if obj.get('wz09_type') == 'nav_node'}
    added_edges = []
    for edge in records:
        if edge['from'] in copied_nav_ids and edge['to'] in copied_nav_ids:
            clone = dict(edge)
            clone.update(id=fresh_id(), **{'from': id_map[edge['from']], 'to': id_map[edge['to']]})
            added_edges.append(clone)
    payload = _encoded(records + added_edges)
    native_payloads={}
    for index_key in ('wz09_native_semantics_index_json','wz09_native_visual_index_json'):
        if scene.get(index_key):
            inventory=json.loads(scene[index_key])
            if not isinstance(inventory,dict):raise ValueError('Native source inventory must be an object')
            if _uid(source) in inventory:
                inventory[id_map[_uid(source)]]=dict(inventory[_uid(source)])
                native_payloads[index_key]=json.dumps(inventory,sort_keys=True,allow_nan=False)
    api = _api(bpy_module)
    created = []
    created_data = []
    copies = {}
    try:
        for obj in hierarchy:
            clone = obj.copy()
            created.append(clone)
            copies[_key(obj)] = clone
            if ((obj.get('wz09_custom_geometry') and not obj.get('wz09_preview_only'))
                    or obj.get('wz09_region_kind')) and obj.data is not None:
                clone.data = obj.data.copy()
                created_data.append((clone.data, getattr(obj, 'type', '')))
            if obj.get('wz09_type'):
                clone['wz09_id'] = id_map[_uid(obj)]
            if obj.get('wz09_geometry_id'):
                clone['wz09_geometry_id'] = fresh_id()
            if obj.get('wz09_preview_id'):
                clone['wz09_preview_id'] = fresh_id()
            if obj.get('wz09_asset_placement_id') in placements:
                clone['wz09_asset_placement_id']=placements[obj['wz09_asset_placement_id']]
            for key in ('wz09_audio_id','wz09_action_id','wz09_region_id','wz09_core_crop_region',
                        'wz09_ground_patch_id','wz09_ground_core_id'):
                if obj.get(key) in extra_ids:clone[key]=extra_ids[obj[key]]
            for key in ('wz09_audio_json','wz09_action_json'):
                if not obj.get(key):continue
                extra=json.loads(obj[key]);extra['id']=extra_ids[extra['id']]
                if extra.get('map_instance_id') in id_map:extra['map_instance_id']=id_map[extra['map_instance_id']]
                if extra.get('marker_id') in id_map:extra['marker_id']=id_map[extra['marker_id']]
                if extra.get('asset_placement_id') in placements:extra['asset_placement_id']=placements[extra['asset_placement_id']]
                clone[key]=json.dumps(extra,sort_keys=True)
            if obj.get('wz09_region_kind'):
                clone['wz09_core_confirmed']=False
                for key in ('wz09_core_confirmed_sha','wz09_core_boundary_shape_sha',
                            'wz09_core_boundary_heights_json','wz09_core_boundary_heights_local_json'):
                    if key in clone:del clone[key]
                for key in ('wz09_core_ground_patch_id','wz09_core_ground_sample_json'):
                    if key in clone:del clone[key]
            if obj.get('wz09_scatter_set_id'):
                # Copy retains real placement identity, but is no longer a member
                # of the source generation's deterministic scatter set.
                if 'wz09_scatter_set_id' in clone:del clone['wz09_scatter_set_id']
            if obj.get('wz09_asset_placement'):
                clone['wz09_owner_map']=id_map[_uid(source)]
                if obj.get('wz09_asset_marker_id') in id_map:clone['wz09_asset_marker_id']=id_map[obj['wz09_asset_marker_id']]
            if obj.get('wz09_type') and obj.get('wz09_properties_json'):
                props=json.loads(obj['wz09_properties_json'])
                if props.get('asset_placement_id') in placements:props['asset_placement_id']=placements[props['asset_placement_id']]
                if props.get('editor_owner_map') in id_map:props['editor_owner_map']=id_map[props['editor_owner_map']]
                clone['wz09_properties_json']=json.dumps(props,sort_keys=True)
            if obj.get('wz09_type') == 'nav_node':
                clone['wz09_owner_map'] = id_map[_uid(source)]
            if obj.get('wz09_type')=='aircraft_keyframe':
                props=json.loads(obj.get('wz09_properties_json','{}'))
                route=obj.get('wz09_route_id') or props.get('route_id')
                if route in routes:
                    if obj.get('wz09_route_id'):clone['wz09_route_id']=routes[route]
                    if 'route_id' in props:
                        props['route_id']=routes[route];clone['wz09_properties_json']=json.dumps(props,sort_keys=True)
            for key in ('wz09_connector_from', 'wz09_connector_to'):
                if obj.get(key) in id_map:
                    clone[key] = id_map[obj[key]]
            # Store matrices before parenting; Blender keeps the original local
            # transform and parent inverse when the copied parent is substituted.
            local = obj.matrix_basis.copy()
            inverse = obj.matrix_parent_inverse.copy()
            clone.parent = copies.get(_key(getattr(obj, 'parent', None)))
            clone.matrix_parent_inverse = inverse
            clone.matrix_basis = local
            for collection in destinations[_key(obj)]:
                collection.objects.link(clone)
        scene[_EDGE_KEY] = payload
        for index_key,native_payload in native_payloads.items():scene[index_key]=native_payload
    except Exception:
        for clone in reversed(created):
            api.data.objects.remove(clone, do_unlink=True)
        for data, kind in created_data:
            collection = getattr(api.data, {'MESH': 'meshes', 'CURVE': 'curves'}.get(kind, ''), None)
            if collection is not None and getattr(data, 'users', 0) == 0:
                collection.remove(data)
        raise
    return copies[_key(source)]
