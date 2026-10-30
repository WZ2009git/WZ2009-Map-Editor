"""Typed gameplay blueprints on existing editor placements/actions/markers.

Profiles are authoring data. Existing native actions remain source baselines;
this module does not execute damage, shops, loot, driving, scripts or physics.
"""
import copy
import json
import math
import re
import uuid
from pathlib import Path

try:
    from . import prefab_core as core, action_elements as actions, audio_zones as audio
    from .authoring_helpers import resolve_owner_map
except ImportError:
    import prefab_core as core
    import action_elements as actions
    import audio_zones as audio
    from authoring_helpers import resolve_owner_map

_CLASSES = []
MARKERS = {'loot_crate','buy_station','player_spawn','loadout_drop','mission','poi','aircraft_keyframe'}
ACTION_KINDS = {'vehicle':'destructible','destructible':'destructible',
                'loot_crate':'loot_crate','buy_station':'buy_station','trigger':'trigger','door':'trigger'}
LABELS = {'vehicle':'Fahrzeug / Explosion','loot_crate':'Loot-Kiste','buy_station':'Buy Station',
    'loadout_drop':'Loadout Drop','player_spawn':'Player Spawn','redeploy':'Redeploy',
    'objective':'Missionsziel','trigger':'Trigger','door':'Door','destructible':'Destructible object',
    'audio_emitter':'Audio Emitter','vfx_emitter':'VFX Emitter'}
ICONS = {'vehicle':'MOD_EXPLODE','loot_crate':'PACKAGE','buy_station':'CART',
    'loadout_drop':'IMPORT','player_spawn':'OUTLINER_OB_ARMATURE','redeploy':'FILE_REFRESH',
    'objective':'SOLO_ON','trigger':'CUBE','door':'MESH_PLANE','destructible':'MOD_EXPLODE',
    'audio_emitter':'SOUND','vfx_emitter':'PARTICLES'}


def _raw(obj):
    try:
        record = json.loads(obj.get('wz09_prefab_json',''))
    except (ValueError,TypeError) as exc:
        raise ValueError('Invalid prefab record: '+getattr(obj,'name','object')) from exc
    if not isinstance(record,dict) or record.get('id') != obj.get('wz09_prefab_id'):
        raise ValueError('Prefab ID differs from the saved record')
    return core.validate_records([record])[0]


def _target(context):
    obj = context.active_object
    while obj is not None:
        if obj.get('wz09_prefab'):
            return obj
        if obj.get('wz09_asset_placement'):
            helpers=[child for child in getattr(obj,'children',()) if child.get('wz09_prefab') and child.get('wz09_world_size_helper')]
            if len(helpers)>1:raise ValueError('Multiple bound prefabs; select the intended helper')
            if helpers:return helpers[0]
            linked = obj.get('wz09_asset_marker_id')
            if linked:
                marker = next((o for o in context.scene.objects if o.get('wz09_id') == linked
                    and (o.get('wz09_prefab') or o.get('wz09_action_element'))),None)
                if marker is not None:
                    return marker
            return obj
        if obj.get('wz09_action_element') or obj.get('wz09_type') in MARKERS:
            return obj
        if obj.get('wz09_type') == 'map_instance':
            return None
        obj = getattr(obj,'parent',None)
    return None


def _ensure_product_id(scene):
    value = scene.get('wz09_prefab_product_id')
    if not value:
        value = str(uuid.uuid4())
        scene['wz09_prefab_product_id'] = value
    return audio._text(value,'Prefab product ID')


def _refs(scene, world=None):
    refs = {key:{} for key in ('maps','placements','markers','actions')}
    def add(kind, identity, record):
        identity = audio._text(identity,kind+' ID')
        if identity in refs[kind]:
            raise ValueError('Duplicate existing '+kind+' ID: '+identity)
        refs[kind][identity] = record
    if world is not None:
        if not isinstance(world,dict):
            raise ValueError('world: exported world document required')
        for key,field in (('maps','maps'),('placements','asset_placements'),('markers','markers'),('actions','action_elements')):
            for record in world.get(field,[]):
                add(key,record['id'],record)
        return refs
    for obj in scene.objects:
        typ = obj.get('wz09_type')
        if typ == 'map_instance':
            add('maps',obj.get('wz09_id'),dict(id=obj.get('wz09_id')))
        if typ in MARKERS:
            owner = resolve_owner_map(obj)
            add('markers',obj.get('wz09_id'),dict(map_instance_id=str(owner['wz09_id']) if owner is not None else None))
        if obj.get('wz09_asset_placement'):
            owner = resolve_owner_map(obj)
            add('placements',obj.get('wz09_asset_placement_id'),dict(owner_map=str(owner['wz09_id']) if owner is not None else None))
        if obj.get('wz09_action_element'):
            record = json.loads(obj.get('wz09_action_json',''))
            if record.get('id') != obj.get('wz09_action_id'):
                raise ValueError('Action-ID differs from stored record')
            owner = resolve_owner_map(obj)
            add('actions',record['id'],dict(record,map_instance_id=str(owner['wz09_id']) if owner is not None else None))
    return refs


def _object_record(obj):
    _raw(obj)
    actions.validate_asset_helper(obj)
    record,scale = audio._object_record(obj,'wz09_prefab_json')
    if record.get('id') != obj.get('wz09_prefab_id'):
        raise ValueError('Prefab ID differs from stored record')
    if max(scale)-min(scale) > 1e-5:
        raise ValueError('Prefab radii require uniform scale; edit dimensions in the profile')
    for key in ('interaction_radius_m','inner_radius_m','outer_radius_m'):
        if key in record['parameters']:
            record['parameters'][key] *= scale[0]
    if 'dimensions_m' in record['parameters']:
        record['parameters']['dimensions_m'] = [v*scale[0] for v in record['parameters']['dimensions_m']]
    owner = resolve_owner_map(obj)
    if owner is None:
        raise ValueError('Prefab needs an existing owner map')
    record['map_instance_id'] = str(owner['wz09_id'])
    return record


def _verify_variants(scene, records):
    variants = {r['parameters'].get('destroyed_model_asset_id') for r in records
                if r['parameters'].get('destroyed_model_asset_id') is not None}
    if variants:
        try:
            from . import asset_browser
        except ImportError:
            import asset_browser
        catalog=asset_browser._catalog(scene);entries={r['id']:r for r in catalog['assets']};checked={}
        try:
            from .asset_catalog import catalog_from_document
        except ImportError:
            from asset_catalog import catalog_from_document
        for asset_id in sorted(variants):
            entry=entries.get(asset_id)
            if entry is None:raise ValueError('Destruction model missing from the source catalog')
            asset_browser.validate_asset_record(entry)
            source=catalog['sources'].get(entry['source_sha256'])
            if source is None or source.get('source_map')!=entry['source_map']:raise ValueError('Destruction model has no matching source')
            if entry['source_sha256'] not in checked:
                document=asset_browser.verify_preview_bundle(source['preview_bundle'])
                checked[entry['source_sha256']]={r['id']:r for r in catalog_from_document(document,source['preview_bundle'],entry['source_map'])['assets']}
            actual=checked[entry['source_sha256']].get(asset_id)
            if actual is None or any(entry.get(k)!=v for k,v in actual.items()):raise ValueError('Destruction model differs from its original source')


def serialize_scene(scene, world=None):
    """Validate a standalone sidecar. Never allocate/persist IDs while serializing."""
    refs = _refs(scene,world)
    records = [_object_record(o) for o in scene.objects if o.get('wz09_prefab')]
    product_id = scene.get('wz09_prefab_product_id')
    if not product_id:
        world_id = world.get('world_id') if isinstance(world,dict) else None
        if not world_id:
            raise ValueError('Prefab product ID missing; create a prefab or provide the world first')
        product_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:prefab-product:'+str(world_id)))
    all_ids = set()
    for values in refs.values():
        if all_ids.intersection(values):
            raise ValueError('Existing world reference IDs collide across record types')
        all_ids.update(values)
    if world is not None:
        for field in ('audio_zones',):
            all_ids.update(r['id'] for r in world.get(field,[]))
        for field in ('nodes','edges'):
            all_ids.update(r['id'] for r in world.get('nav',{}).get(field,[]))
    if product_id in all_ids:
        raise ValueError('Prefab product ID collides with a world record')
    result = core.validate_records(records,maps=refs['maps'],placements=refs['placements'],
        markers=refs['markers'],actions=refs['actions'],all_ids=all_ids|{product_id},verify_sources=True)
    _verify_variants(scene,result)
    return dict(kind='wz09_gameplay_prefabs',version=1,id=product_id,
                prefabs=sorted(result,key=lambda r:r['id']),runtime_integrated=False)


def _action_profile(record):
    p = record['parameters']
    return p.get({'vehicle':'damage_profile','destructible':'damage_profile','door':'door_profile',
        'loot_crate':'loot_table','buy_station':'shop_profile','trigger':'event_ref'}.get(record['kind'],''),'')


def _action_record(record):
    result = actions.new_record(ACTION_KINDS[record['kind']],position=record['position_m'],
        map_instance_id=record['map_instance_id'])
    result.update(rotation_deg=list(record['rotation_deg']),radius_m=record['parameters']['interaction_radius_m'],
        behavior_ref=_action_profile(record),health=record['parameters'].get('max_health'),
        enabled=record['parameters']['enabled'],evidence=copy.deepcopy(record['evidence']))
    # An authored action derived from a native prefab must not impersonate its
    # original ordinal. Native attach itself reuses the existing action instead.
    if result['evidence']['status'] == 'source_bound':
        result['evidence']['source_record'] = 'authored_prefab_action_from_'+record['id']
    if record['kind'] in {'vehicle','destructible','door'}:
        result['dependencies'] = ['native_collider_profile','runtime_adapter_unverified']
        result['missing_dependencies'] = list(result['dependencies'])
    return actions.validate_records([result])[0]


def _store(root, record):
    root['wz09_prefab'] = True
    root['wz09_prefab_id'] = record['id']
    root['wz09_prefab_kind'] = record['kind']
    root['wz09_prefab_json'] = audio._encoded(record)
    if hasattr(root,'show_name'):
        root.show_name = True
        root.show_in_front = True


def _wire_prefab(context, root, record, bpy):
    """Reuse existing audio volume contours for explicit trigger/emitter bounds."""
    if record['kind'] not in {'trigger','audio_emitter'}:
        return
    p = record['parameters']
    shape = p.get('shape','sphere')
    zone = audio.new_record('ambient',shape)
    zone['fade_m'] = 0
    if record['kind'] == 'audio_emitter':
        zone['radius_m'],zone['inner_radius_m'] = p['outer_radius_m'],p['inner_radius_m']
    else:
        zone['dimensions_m'] = p['dimensions_m']
        zone['height_m'] = p['dimensions_m'][2]
        zone['radius_m'],zone['inner_radius_m'] = p['interaction_radius_m'],0
    vertices,edges = audio.contour_geometry(zone)
    old = [c for c in root.children if c.get('wz09_prefab_contour')]
    mesh = bpy.data.meshes.new('WZ Prefab Bounds')
    mesh.from_pydata(vertices,edges,[]);mesh.update()
    child = bpy.data.objects.new('Prefab · '+LABELS[record['kind']]+' · bounds',mesh)
    context.scene.collection.objects.link(child);child.parent=root
    child['wz09_prefab_contour']=True;child['wz09_editor_overlay']=True
    child['wz09_preview_only']=True;child['wz09_preview_id']=str(uuid.uuid4())
    child.hide_render=True;child.hide_select=True;child.show_in_front=True;child.display_type='WIRE'
    for previous in old:
        data=previous.data
        bpy.data.objects.remove(previous,do_unlink=True)
        if data and data.users == 0:
            bpy.data.meshes.remove(data)


def create_prefab(context, record, *, bind_object=None, _source_verified=False, _existing_ids=None):
    """Attach to an existing root or create an existing-style marker/action."""
    import bpy
    from mathutils import Matrix,Euler,Vector
    if getattr(context,'mode','OBJECT') != 'OBJECT':
        raise ValueError('Switch to Object Mode to create prefabs')
    record = core.validate_records([record],verify_sources=not _source_verified)[0]
    _verify_variants(context.scene,[record])
    existing = _existing_ids if _existing_ids is not None else {
        o.get('wz09_prefab_id') for o in context.scene.objects if o.get('wz09_prefab')}
    if record['id'] in existing:
        raise ValueError('Prefab-ID existiert bereits')
    owner = resolve_owner_map(bind_object if bind_object is not None else context.active_object)
    if owner is None or str(owner['wz09_id']) != record['map_instance_id']:
        raise ValueError('Select a target map; profile and target must belong to the same map')
    root = bind_object
    if root is not None:
        if root.get('wz09_asset_placement') and root.get('wz09_asset_marker_id'):
            linked = root['wz09_asset_marker_id']
            marker = next((o for o in context.scene.objects if o.get('wz09_id') == linked
                and (o.get('wz09_prefab') or o.get('wz09_action_element'))),None)
            if marker is not None:
                root = marker
        if root.get('wz09_prefab'):
            raise ValueError('Selection already has a prefab; use Read selection and Apply')
        if not (root.get('wz09_asset_placement') or root.get('wz09_action_element') or root.get('wz09_type') in MARKERS):
            raise ValueError('Bind only an existing asset placement or gameplay marker')
        if root.get('wz09_asset_placement'):
            _position,_rotation,scale=actions.asset_binding_pose(root)
            if root.get('wz09_action_element') and any(abs(v-1)>1e-5 for v in scale):
                raise ValueError('Legacy action on an asset: create a new placement before binding with scale')
        elif any(abs(v-1) > 1e-5 for v in root.matrix_world.to_scale()):
            raise ValueError('Apply object scale before binding')
        if root.get('wz09_action_element') and record['kind'] in ACTION_KINDS:
            existing_action = json.loads(root.get('wz09_action_json',''))
            if existing_action.get('kind') != ACTION_KINDS[record['kind']]:
                raise ValueError('Existing action type does not match the selected prefab')
        expected_marker = {'player_spawn':'player_spawn','loadout_drop':'loadout_drop',
            'objective':'mission','redeploy':'poi','loot_crate':'loot_crate','buy_station':'buy_station'}.get(record['kind'])
        if root.get('wz09_type') in MARKERS and expected_marker and root.get('wz09_type') != expected_marker:
            raise ValueError('Existing gameplay marker does not match the selected prefab')
        actual_asset = root if root.get('wz09_asset_placement') else getattr(root,'parent',None)
        target_refs = dict(asset_placement_id=actual_asset.get('wz09_asset_placement_id')
            if actual_asset is not None and actual_asset.get('wz09_asset_placement') else None,
            marker_id=root.get('wz09_id') if root.get('wz09_type') in MARKERS else None,
            action_element_id=root.get('wz09_action_id') if root.get('wz09_action_element') else None)
        for field,actual in target_refs.items():
            if record[field] is not None and record[field] != actual:
                raise ValueError('Prefab reference differs from selected root: '+field)
    helper_transaction={}
    old_product_id=context.scene.get('wz09_prefab_product_id')
    try:
        if record['kind'] in ACTION_KINDS and (root is None or not root.get('wz09_action_element')):
            root = actions.create_action(context,_action_record(record),bind_object=root,
                _source_verified=_source_verified,_helper_transaction=helper_transaction)
        elif root is not None and root.get('wz09_asset_placement'):
            marker_kind={'player_spawn':'player_spawn','redeploy':'poi'}.get(record['kind'])
            root,state=actions.create_asset_helper(context,root,name='Prefab · '+LABELS[record['kind']],marker_kind=marker_kind)
            helper_transaction.update(root=root,state=state)
        elif root is None:
            marker_kind = {'player_spawn':'player_spawn','loadout_drop':'loadout_drop','objective':'mission','redeploy':'poi'}.get(record['kind'])
            if marker_kind:
                previous = context.scene.wz09_marker_choice
                try:
                    context.scene.wz09_marker_choice = marker_kind
                    if bpy.ops.wz09.add_marker() != {'FINISHED'}:
                        raise ValueError('Existing marker operator did not finish')
                    root = context.active_object
                finally:
                    context.scene.wz09_marker_choice = previous
            else:
                root = bpy.data.objects.new('Prefab · '+LABELS[record['kind']],None)
                context.scene.collection.objects.link(root)
                root.parent = owner
                root.matrix_parent_inverse = Matrix.Identity(4)
                root.empty_display_type = 'SPHERE' if record['kind'] == 'audio_emitter' else 'CUBE'
                root.empty_display_size = record['parameters']['interaction_radius_m']
                root['wz09_preview_only'] = True
                root['wz09_editor_overlay'] = True
                root['wz09_preview_id'] = str(uuid.uuid4())
            root.matrix_world = Matrix.LocRotScale(Vector(record['position_m']),
                Euler([math.radians(v) for v in record['rotation_deg']],'XYZ').to_quaternion(),Vector((1,1,1)))
        actual_owner = resolve_owner_map(root)
        if actual_owner is None or str(actual_owner['wz09_id']) != record['map_instance_id']:
            raise ValueError('Created prefab has no matching target map')
        record['asset_placement_id'] = str(root['wz09_asset_placement_id']) if root.get('wz09_asset_placement') else None
        parent = getattr(root,'parent',None)
        if parent is not None and parent.get('wz09_asset_placement'):
            record['asset_placement_id'] = str(parent['wz09_asset_placement_id'])
        record['marker_id'] = str(root['wz09_id']) if root.get('wz09_type') in MARKERS else None
        record['action_element_id'] = str(root['wz09_action_id']) if root.get('wz09_action_element') else None
        record['position_m'] = list(root.matrix_world.translation)
        record['rotation_deg'] = [math.degrees(v) for v in root.matrix_world.to_euler('XYZ')]
        record = core.validate_records([record])[0]
        _ensure_product_id(context.scene)
        _store(root,record)
        _wire_prefab(context,root,record,bpy)
        if _existing_ids is not None:
            _existing_ids.add(record['id'])
        for obj in context.selected_objects:
            obj.select_set(False)
        root.select_set(True)
        context.view_layer.objects.active = root
        return root
    except Exception:
        if helper_transaction:
            actions.rollback_asset_helper(context,helper_transaction['root'],helper_transaction['state'])
        if old_product_id is None and 'wz09_prefab_product_id' in context.scene:
            del context.scene['wz09_prefab_product_id']
        raise


def clone_record(source, *, map_instance_id, asset_placement_id, position=(0,0,0), rotation=(0,0,0)):
    """Independent explicit profile; no copied IDs, bindings or native ordinal."""
    source = core.validate_records([source])[0]
    record = copy.deepcopy(source)
    record.update(id=str(uuid.uuid4()),map_instance_id=map_instance_id,
        asset_placement_id=asset_placement_id,marker_id=None,action_element_id=None,
        position_m=list(position),rotation_deg=list(rotation))
    if map_instance_id != source['map_instance_id']:
        record['parameters']['region_ref'] = ''
    record['evidence']['source_record'] = 'authored_prefab_from_'+source['id']
    return core.validate_records([record])[0]


def clone_for_asset(context, source_prefab, target_placement):
    """Use after existing asset placement; source is an explicit record/root/ID."""
    if not target_placement.get('wz09_asset_placement'):
        raise ValueError('Target must be an existing asset placement')
    if isinstance(source_prefab,str):
        source_prefab = next((o for o in context.scene.objects if o.get('wz09_prefab_id') == source_prefab),None)
        if source_prefab is None:
            raise ValueError('Selected source prefab no longer exists')
    source = source_prefab if isinstance(source_prefab,dict) and 'parameters' in source_prefab else _raw(source_prefab)
    owner = resolve_owner_map(target_placement)
    if owner is None:
        raise ValueError('Asset placement has no target map')
    record = clone_record(source,map_instance_id=str(owner['wz09_id']),
        asset_placement_id=str(target_placement['wz09_asset_placement_id']),
        position=list(target_placement.matrix_world.translation),
        rotation=[math.degrees(v) for v in target_placement.matrix_world.to_euler('XYZ')])
    return create_prefab(context,record,bind_object=target_placement)


def templates_for_asset(scene, asset_id):
    """Existing roots only. Multiple matches deliberately require a user choice."""
    placements = {str(o.get('wz09_asset_placement_id')):o for o in scene.objects if o.get('wz09_asset_placement')}
    result = []
    for obj in scene.objects:
        if not obj.get('wz09_prefab'):
            continue
        record = _raw(obj)
        placement = placements.get(record['asset_placement_id'])
        if placement is not None and placement.get('wz09_asset_id') == asset_id:
            result.append(record)
    return sorted(result,key=lambda r:r['id'])


def native_vehicle_record(action, semantics, *, semantics_sha256):
    """Pure exact source proof. Model names cannot qualify a vehicle blueprint."""
    action = actions.validate_records([action])[0]
    evidence = action['evidence']
    pointer = evidence['source_record'].partition(';')[0].strip()
    match = re.fullmatch(r'script_models\[(\d+)\]',pointer)
    if evidence['status'] != 'source_bound' or not match or action['kind'] != 'destructible':
        return None
    if evidence['source_sha256'] != semantics_sha256:
        raise ValueError('Native action references another semantic metadata SHA-256')
    index = int(match.group(1))
    if index >= len(semantics.get('script_models',[])):
        raise ValueError('Native vehicle evidence pointer is outside its source array')
    source = semantics['script_models'][index]
    native_type = source.get('destructible_type')
    if not isinstance(native_type,str) or not native_type.startswith('vehicle_'):
        return None
    if action['behavior_ref'] != 'iw4:destructible/'+native_type:
        raise ValueError('Vehicle profile differs from its exact native destruction type')
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:native-vehicle-prefab:'+action['id']))
    record = core._record_template('vehicle',action['map_instance_id'],identity,action['position_m'])
    record.update(rotation_deg=list(action['rotation_deg']),asset_placement_id=action['asset_placement_id'],
        marker_id=action['marker_id'],action_element_id=action['id'],evidence=copy.deepcopy(evidence))
    params = record['parameters']
    params.update(damage_profile=action['behavior_ref'],max_health=action['health'],
        interaction_radius_m=action['radius_m'],enabled=action['enabled'])
    if source.get('script_exploder'):
        params['explosion_event'] = 'iw4:script_exploder/'+str(source['script_exploder'])
    if source.get('script_fxid'):
        params['fx_ref'] = 'iw4:fx/'+str(source['script_fxid'])
    # Collider/driveability stay explicitly unverified even with a visible mesh.
    return core.validate_records([record])[0]


def attach_native_vehicles(context, *, all_maps=False):
    """Attach all exact eligible native source actions, never import new models."""
    try:
        from . import native_semantics as native
    except ImportError:
        import native_semantics as native
    scene = context.scene
    selected_owner = resolve_owner_map(context.active_object)
    if not all_maps and selected_owner is None:
        raise ValueError('Select a map or an object from that map')
    inventory = json.loads(scene.get('wz09_native_semantics_index_json','{}'))
    cache,pairs = {},[]
    existing = {o.get('wz09_prefab_id') for o in scene.objects if o.get('wz09_prefab')}
    skipped = 0
    for obj in scene.objects:
        if not obj.get('wz09_action_element'):
            continue
        owner = resolve_owner_map(obj)
        if owner is None or not all_maps and owner != selected_owner:
            continue
        action = json.loads(obj.get('wz09_action_json',''))
        pointer = action.get('evidence',{}).get('source_record','').partition(';')[0].strip()
        if not re.fullmatch(r'script_models\[\d+\]',pointer) or action.get('evidence',{}).get('status') != 'source_bound':
            continue
        mid = str(owner['wz09_id'])
        source = inventory.get(mid)
        if source is None:
            raise ValueError('Native semantic source binding missing for map '+mid)
        if Path(action['evidence']['source_path']).resolve() != Path(source['path']).resolve():
            raise ValueError('Native action source path differs from its bound semantic inventory')
        key = (source['path'],source['sha256'])
        if key not in cache:
            if native.file_sha256(source['path']) != source['sha256']:
                raise ValueError('Native semantic metadata SHA-256 changed')
            data = native.read_semantics(source['path'])
            if data['source_map'] != source['source_map'] or data['source_sha256'] != source['original_ff_sha256']:
                raise ValueError('Native semantic inventory differs from original FF/map')
            cache[key] = data
        data = cache[key]
        if owner.get('wz09_source_map') != data['source_map']:
            raise ValueError('Native vehicle belongs to another source map')
        if action.get('id') != obj.get('wz09_action_id') or action.get('map_instance_id') != mid:
            raise ValueError('Native vehicle action ID/owner differs from its root')
        record = native_vehicle_record(action,data,semantics_sha256=source['sha256'])
        if record is None:
            continue
        if obj.get('wz09_prefab') or record['id'] in existing:
            skipped += 1
            continue
        pairs.append((obj,record))
    # Complete source validation precedes mutations. Existing native AE JSON,
    # transforms, original entity IDs and mesh data are never rewritten.
    if pairs:
        _ensure_product_id(scene)
    for obj,record in pairs:
        _store(obj,record)
        existing.add(record['id'])
    return dict(attached=len(pairs),existing_skipped=skipped,verified_source_files=len(cache),
                runtime_integrated=False,collision_verified=False)


def _settings_record(settings, context, previous=None):
    owner = resolve_owner_map(context.active_object)
    if owner is None:
        raise ValueError('Select a target map first')
    if previous is not None and previous['kind'] != settings.kind:
        raise ValueError('Changing type would alter references; create a new prefab')
    record = copy.deepcopy(previous) if previous is not None else core.new_record(settings.kind,
        map_instance_id=str(owner['wz09_id']),position=list(context.scene.cursor.location))
    params = {}
    for key,default in core.parameters_for_kind(settings.kind).items():
        value = getattr(settings,key)
        if key in {'max_health','restock_s'}:
            value = value if getattr(settings,'override_'+key) else None
        elif key == 'destroyed_model_asset_id':
            value = value.strip() or None
        elif isinstance(default,list):
            value = list(value)
        params[key] = value
    if previous is not None and params != previous['parameters']:
        record['evidence'] = dict(audio.unverified_evidence(),source_record='authored_prefab_override_from_'+record['id'])
    record['parameters'] = params
    return core.validate_records([record])[0]


def _load_settings(settings, record):
    if settings.kind != record['kind']:
        settings.kind = record['kind']
    for key,value in record['parameters'].items():
        if key in {'max_health','restock_s'}:
            setattr(settings,'override_'+key,value is not None)
            setattr(settings,key,value if value is not None else 100 if key == 'max_health' else 60)
        else:
            setattr(settings,key,'' if value is None else value)


def draw(layout, context):
    """Compact typed fields with existing action state and explicit missing gates."""
    settings = context.scene.wz09_prefab_settings
    icon_values = None
    try:
        import bpy
        icon_values = bpy.types.UILayout.bl_rna.functions['prop'].parameters['icon'].enum_items.keys()
    except (AttributeError,KeyError):
        pass
    icon = lambda value: value if icon_values is None or value in icon_values else 'INFO'
    layout.prop(settings,'kind',text='Prefab')
    defaults = core.parameters_for_kind(settings.kind)
    for key in defaults:
        if key == 'driveable':
            layout.label(text='Vehicle physics / networking unverified',icon='INFO')
            continue
        if key in {'max_health','restock_s'}:
            layout.prop(settings,'override_'+key)
            if getattr(settings,'override_'+key):
                layout.prop(settings,key)
        else:
            layout.prop(settings,key)
    row = layout.row(align=True)
    row.operator('wz09.prefab_add',text='Am Cursor',icon='ADD')
    row.operator('wz09.prefab_add',text='Bind to selection',icon='LINKED').bind_selected = True
    row = layout.row(align=True)
    row.operator('wz09.prefab_load',text='Read selection',icon='EYEDROPPER')
    row.operator('wz09.prefab_apply',text='Apply',icon='CHECKMARK')
    obj = _target(context)
    if obj is not None and obj.get('wz09_prefab'):
        try:
            record = _raw(obj)
            box = layout.box()
            box.label(text=LABELS[record['kind']]+' · '+record['id'][:8],icon=icon(ICONS[record['kind']]))
            box.label(text='Gameplay blueprint - runtime unverified',icon='INFO')
            for dependency in core.missing_dependencies(record):
                box.label(text='Fehlt: '+dependency,icon='ERROR')
            if obj.get('wz09_action_element'):
                action = json.loads(obj.get('wz09_action_json','{}'))
                box.label(text='Aktion: '+actions.authoring_status(action),icon='INFO')
            box.operator('wz09.prefab_detach',text='Remove prefab link only',icon='UNLINKED')
        except (ValueError,KeyError) as exc:
            layout.label(text=str(exc)[:100],icon='ERROR')
    row = layout.row(align=True)
    row.operator('wz09.prefab_native_vehicles',text='Original vehicles: map',icon='MOD_EXPLODE')
    row.operator('wz09.prefab_native_vehicles',text='All maps',icon='MOD_EXPLODE').all_maps=True
    try:
        from .authoring_list import filter_page
    except ImportError:
        from authoring_list import filter_page
    records = []
    for item in context.scene.objects:
        if item.get('wz09_prefab'):
            try:
                record = _raw(item)
                records.append(dict(record,name=item.name,behavior_ref=_action_profile(record)))
            except (ValueError,KeyError) as exc:
                layout.label(text=(item.name+': '+str(exc))[:100],icon='ERROR')
    owner = resolve_owner_map(context.active_object)
    layout.prop(settings,'list_search',text='',icon='VIEWZOOM')
    row = layout.row(align=True)
    row.prop(settings,'list_owner_only')
    row.prop(settings,'list_page')
    page = filter_page(records,query=settings.list_search,page=settings.list_page,
        owner_map_id=str(owner['wz09_id']) if owner is not None and settings.list_owner_only else None)
    layout.label(text=f"{page['page']}/{page['pages']} · {page['total']} Prefabs")
    for record in page['items']:
        op = layout.operator('wz09.prefab_select',text=record['name'],icon=icon(ICONS[record['kind']]))
        op.prefab_id = record['id']
    layout.label(text='A model alone does not enable gameplay logic',icon='INFO')


def register():
    import bpy
    from bpy.props import (BoolProperty,EnumProperty,FloatProperty,FloatVectorProperty,IntProperty,
                           PointerProperty,StringProperty)

    def kind_changed(settings,context):
        _load_settings(settings,dict(kind=settings.kind,parameters=core.parameters_for_kind(settings.kind)))

    # Build one typed control per existing core parameter, not a second schema.
    annotations = {'kind':EnumProperty(name='Typ',items=[(k,LABELS[k],'Explizites Editorprofil')
        for k in core.KINDS],default='loot_crate',update=kind_changed)}
    annotations.update(list_search=StringProperty(name='Search prefabs',update=lambda s,c:setattr(s,'list_page',1)),
        list_owner_only=BoolProperty(name='Selected map only',default=True,update=lambda s,c:setattr(s,'list_page',1)),
        list_page=IntProperty(name='Page',default=1,min=1,max=100000))
    combined = {}
    for kind in core.KINDS:
        combined.update(core.parameters_for_kind(kind))
    names = {'interaction_radius_m':'Interaktionsradius','max_health':'Lebenspunkte',
        'restock_s':'Restock (s)','override_max_health':'Set health explicitly',
        'override_restock_s':'Set restock explicitly','collider_profile':'Colliderprofil',
        'damage_profile':'Schadensprofil','explosion_event':'Explosionsereignis','fx_ref':'FX-Referenz',
        'audio_ref':'Audio / Zone / Alias','animation_ref':'Animationsreferenz','region_ref':'Mapregion',
        'destroyed_model_asset_id':'Destruction variant (asset ID)','enabled':'Enabled',
        'dimensions_m':'Trigger-Abmessungen','loot_table':'Loot-Tabelle','shop_profile':'Shopprofil'}
    for key,default in combined.items():
        label = names.get(key,key.replace('_',' ').capitalize())
        if key == 'shape':
            prop = EnumProperty(name='Shape',items=[('box','Box',''),('cylinder','Zylinder',''),('sphere','Sphere','')],default='box')
        elif key in {'max_health','restock_s'}:
            prop = FloatProperty(name=label,default=100 if key == 'max_health' else 60,
                                 min=.000001 if key == 'max_health' else 0)
            annotations['override_'+key] = BoolProperty(name=names['override_'+key],default=False)
        elif isinstance(default,bool):
            prop = BoolProperty(name=label,default=default)
        elif isinstance(default,list):
            prop = FloatVectorProperty(name=label,size=3,default=default,min=.001,subtype='XYZ')
        elif isinstance(default,(float,int)):
            prop = FloatProperty(name=label,default=default,min=.001 if key in {'interaction_radius_m','outer_radius_m'} else 0,
                                 max=1 if key == 'volume' else 10000000)
        else:
            prop = StringProperty(name=label,default=default or '')
        annotations[key] = prop
    Settings = type('WZ09_PrefabSettings',(bpy.types.PropertyGroup,),{'__annotations__':annotations,'__module__':__name__})

    class WZ09_OT_prefab_add(bpy.types.Operator):
        bl_idname='wz09.prefab_add';bl_label='Create Gameplay Prefab';bl_options={'REGISTER','UNDO'}
        bind_selected:BoolProperty(default=False)
        def execute(self,context):
            try:
                target = _target(context) if self.bind_selected else None
                if self.bind_selected and target is None:
                    raise ValueError('Select an asset placement or gameplay marker')
                create_prefab(context,_settings_record(context.scene.wz09_prefab_settings,context),bind_object=target)
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_prefab_load(bpy.types.Operator):
        bl_idname='wz09.prefab_load';bl_label='Read Selected Prefab'
        def execute(self,context):
            try:
                obj = _target(context)
                if obj is None or not obj.get('wz09_prefab'):
                    raise ValueError('Select a prefab first')
                _load_settings(context.scene.wz09_prefab_settings,_object_record(obj))
                return {'FINISHED'}
            except (ValueError,OSError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_prefab_apply(bpy.types.Operator):
        bl_idname='wz09.prefab_apply';bl_label='Apply Prefab Settings';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:
                obj = _target(context)
                if obj is None or not obj.get('wz09_prefab'):
                    raise ValueError('Select a prefab first')
                if any(abs(v-1) > 1e-5 for v in obj.matrix_world.to_scale()):
                    raise ValueError('Apply object scale before applying settings')
                record = _settings_record(context.scene.wz09_prefab_settings,context,previous=_object_record(obj))
                _verify_variants(context.scene,[record])
                _store(obj,record)
                _wire_prefab(context,obj,record,bpy)
                return {'FINISHED'}
            except (ValueError,OSError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_prefab_detach(bpy.types.Operator):
        bl_idname='wz09.prefab_detach';bl_label='Detach Prefab Metadata';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            obj = _target(context)
            if obj is None or not obj.get('wz09_prefab'):
                self.report({'ERROR'},'Select a prefab first');return {'CANCELLED'}
            for key in ('wz09_prefab','wz09_prefab_id','wz09_prefab_kind','wz09_prefab_json'):
                if key in obj:
                    del obj[key]
            for child in [c for c in obj.children if c.get('wz09_prefab_contour')]:
                data=child.data
                bpy.data.objects.remove(child,do_unlink=True)
                if data and data.users == 0:
                    bpy.data.meshes.remove(data)
            return {'FINISHED'}

    class WZ09_OT_prefab_native_vehicles(bpy.types.Operator):
        bl_idname='wz09.prefab_native_vehicles';bl_label='Attach Proven Native Vehicle Profiles';bl_options={'REGISTER','UNDO'}
        all_maps:BoolProperty(name='All loaded maps',default=False)
        def execute(self,context):
            try:
                result = attach_native_vehicles(context,all_maps=self.all_maps)
                self.report({'INFO'},f"{result['attached']} belegt markiert · {result['existing_skipped']} available - runtime unverified")
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_prefab_select(bpy.types.Operator):
        bl_idname='wz09.prefab_select';bl_label='Select Gameplay Prefab'
        prefab_id:StringProperty()
        def execute(self,context):
            obj = next((o for o in context.scene.objects if o.get('wz09_prefab_id') == self.prefab_id),None)
            if obj is None:
                self.report({'ERROR'},'Prefab no longer exists');return {'CANCELLED'}
            try:
                record = _object_record(obj)
                for selected in context.selected_objects:
                    selected.select_set(False)
                obj.hide_set(False);obj.select_set(True)
                context.view_layer.objects.active = obj
                if bpy.ops.view3d.view_selected.poll():
                    bpy.ops.view3d.view_selected(use_all_regions=False)
                _load_settings(context.scene.wz09_prefab_settings,record)
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_PT_prefabs(bpy.types.Panel):
        bl_label='Gameplay Prefabs';bl_idname='WZ09_PT_prefabs';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_options={'DEFAULT_CLOSED'}
        @classmethod
        def poll(cls,context):
            return getattr(context.scene,'wz09_workspace','') in {'ACTIONS','ASSETS','PLACE','BUILD','WORLD'}
        def draw(self,context):
            draw(self.layout,context)

    _CLASSES[:] = [Settings,WZ09_OT_prefab_add,WZ09_OT_prefab_load,WZ09_OT_prefab_apply,
                  WZ09_OT_prefab_detach,WZ09_OT_prefab_native_vehicles,WZ09_OT_prefab_select,WZ09_PT_prefabs]
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_prefab_settings = PointerProperty(type=Settings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_prefab_settings'):
        del bpy.types.Scene.wz09_prefab_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
