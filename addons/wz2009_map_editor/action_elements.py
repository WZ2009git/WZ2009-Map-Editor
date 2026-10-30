"""Explicit action profiles on existing placements/markers, without game logic.

The game already owns destruction, loot and shop execution. This module authors
stable bindings and diagnostic contours. A model name never grants a behavior.
"""
import copy
import json
import math
import re
import uuid
try:
    from .audio_zones import (_number, _vector, _text, _encoded, _object_record,
                              unverified_evidence, validate_evidence)
    from .authoring_helpers import resolve_owner_map
except ImportError:
    from audio_zones import (_number, _vector, _text, _encoded, _object_record,
                             unverified_evidence, validate_evidence)
    from authoring_helpers import resolve_owner_map

KINDS = {'destructible', 'loot_crate', 'buy_station', 'trigger'}
ICONS = {'destructible': 'MOD_EXPLODE', 'loot_crate': 'PACKAGE', 'buy_station': 'CART', 'trigger': 'OUTLINER_OB_EMPTY'}
_CLASSES = []


def new_record(kind='loot_crate', *, position=(0, 0, 0), map_instance_id=None):
    return {'id': str(uuid.uuid4()), 'map_instance_id': map_instance_id, 'kind': kind,
            'position_m': list(position), 'rotation_deg': [0, 0, 0], 'radius_m': 2.5,
            'behavior_ref': '', 'marker_id': None, 'asset_placement_id': None,
            'evidence': unverified_evidence(), 'dependencies': [], 'missing_dependencies': [],
            'health': None, 'explosion_radius_m': 0.0, 'enabled': True,
            'runtime_integrated': False}


def validate_records(records, map_ids=None, all_ids=None, *, verify_sources=False,
                     marker_ids=None, asset_placement_ids=None):
    if not isinstance(records, list):
        raise ValueError('action_elements must be a list')
    seen, cache, result = set(all_ids or ()), {}, []
    for value in records:
        if not isinstance(value, dict):
            raise ValueError('Action record must be an object')
        r = copy.deepcopy(value)
        required = set(new_record())
        if set(r) != required:
            raise ValueError('Action record fields differ: '+', '.join(sorted(set(r)^required)))
        uid = _text(r['id'], 'Action ID')
        if uid in seen:
            raise ValueError('Duplicate action ID: '+uid)
        seen.add(uid)
        if r['kind'] not in KINDS:
            raise ValueError('Unsupported action kind')
        if r['map_instance_id'] is not None:
            owner = _text(r['map_instance_id'], 'Action map ID')
            if map_ids is not None and owner not in map_ids:
                raise ValueError('Action owner map is missing: '+owner)
        r['position_m'] = _vector(r['position_m'], 'Action position')
        r['rotation_deg'] = _vector(r['rotation_deg'], 'Action rotation')
        r['radius_m'] = _number(r['radius_m'], 'Action radius', .000001)
        r['explosion_radius_m'] = _number(r['explosion_radius_m'], 'Explosion radius', 0)
        if r['health'] is not None:
            r['health'] = _number(r['health'], 'Action health', .000001)
        _text(r['behavior_ref'], 'Action behavior reference', empty=True)
        for field, allowed in (('marker_id', marker_ids), ('asset_placement_id', asset_placement_ids)):
            if r[field] is not None:
                _text(r[field], field)
                if allowed is not None and r[field] not in allowed:
                    raise ValueError('Action reference missing: '+field+' '+r[field])
        for field in ('dependencies', 'missing_dependencies'):
            if not isinstance(r[field], list) or any(not isinstance(v, str) or not v.strip() for v in r[field]):
                raise ValueError(field+' must contain nonempty dependency references')
            if len(set(r[field])) != len(r[field]):
                raise ValueError('Duplicate action '+field)
            r[field] = sorted(r[field])
        if not set(r['missing_dependencies']) <= set(r['dependencies']):
            raise ValueError('Missing dependencies must also be declared dependencies')
        if type(r['enabled']) is not bool or r['runtime_integrated'] is not False:
            raise ValueError('Actions must keep runtime_integrated false and boolean enabled')
        r['evidence'] = validate_evidence(r['evidence'], verify_sources=verify_sources, cache=cache)
        result.append(r)
    return result


def authoring_status(record):
    r = validate_records([record])[0]
    if not r['enabled']:
        return 'disabled'
    if r['missing_dependencies']:
        return 'dependencies_missing'
    if not r['behavior_ref']:
        return 'behavior_missing'
    if r['evidence']['status'] != 'source_bound':
        return 'source_unverified'
    return 'adapter_missing'


def contour_geometry(record, segments=48):
    """Use/trigger radius plus explicit destruction effect radius, wire only."""
    r = validate_records([record])[0]
    if type(segments) is not int or not 8 <= segments <= 512:
        raise ValueError('Action contour segments must be between 8 and 512')
    vertices, edges = [], []
    def circle(radius, plane):
        start = len(vertices)
        for i in range(segments):
            a = 2*math.pi*i/segments
            x, y = radius*math.cos(a), radius*math.sin(a)
            vertices.append((x, y, 0) if plane == 'XY' else (x, 0, y))
            edges.append((start+i, start+(i+1)%segments))
    circle(r['radius_m'], 'XY')
    if r['kind'] in {'trigger', 'destructible'}:
        circle(r['radius_m'], 'XZ')
    if r['explosion_radius_m'] > 0:
        circle(r['explosion_radius_m'], 'XY'); circle(r['explosion_radius_m'], 'XZ')
    # Type glyph geometry stays visible when viewport colors are disabled.
    extent = min(r['radius_m']*.35, 1.0)
    start = len(vertices)
    vertices.extend([(-extent, -extent, .1), (extent, -extent, .1),
                     (extent, extent, .1), (-extent, extent, .1)])
    edges.extend((start+i, start+(i+1)%4) for i in range(4))
    if r['kind'] == 'buy_station':
        vertices.extend([(0, -extent, .1), (0, extent, .1)])
        edges.append((start+4, start+5))
    elif r['kind'] == 'destructible':
        edges.extend([(start, start+2), (start+1, start+3)])
    return vertices, edges


def serialize_scene(scene):
    records = []
    map_ids = {str(o.get('wz09_id')) for o in scene.objects if o.get('wz09_type') == 'map_instance'}
    marker_ids = {str(o.get('wz09_id')) for o in scene.objects if o.get('wz09_type') in {'loot_crate', 'buy_station', 'poi', 'player_spawn', 'loadout_drop', 'mission', 'aircraft_keyframe'}}
    placement_ids = {str(o.get('wz09_asset_placement_id')) for o in scene.objects if o.get('wz09_asset_placement')}
    for obj in scene.objects:
        if not obj.get('wz09_action_element'):
            continue
        validate_asset_helper(obj)
        r, scale = _object_record(obj, 'wz09_action_json')
        if r['id'] != obj.get('wz09_action_id'):
            raise ValueError('Action ID differs from stored record')
        if max(scale)-min(scale) > 1e-5:
            raise ValueError('Action radius needs uniform scale; edit its radius instead')
        r['radius_m'] *= scale[0]; r['explosion_radius_m'] *= scale[0]
        owner = resolve_owner_map(obj)
        r['map_instance_id'] = str(owner['wz09_id']) if owner is not None else None
        records.append(r)
    return validate_records(records, map_ids, verify_sources=True,
                            marker_ids=marker_ids, asset_placement_ids=placement_ids)


def _record_for_settings(obj):
    """Read only this selected action; complete source checks belong to export."""
    r,scale=_object_record(obj,'wz09_action_json')
    r=validate_records([r],verify_sources=False)[0]
    if r['id']!=obj.get('wz09_action_id'):
        raise ValueError('Action ID differs from stored record')
    if max(scale)-min(scale)>1e-5:
        raise ValueError('Action radius needs uniform scale; edit its radius instead')
    r['radius_m']*=scale[0];r['explosion_radius_m']*=scale[0]
    owner=resolve_owner_map(obj)
    r['map_instance_id']=str(owner['wz09_id']) if owner is not None else None
    return validate_records([r],verify_sources=False)[0]


def asset_binding_pose(asset):
    """Read-only TRS preflight using existing audio/action matrix checks."""
    from types import SimpleNamespace
    if not math.isfinite(float(asset.matrix_world.determinant())):
        raise ValueError('Asset transform determinant must be finite')
    proxy=SimpleNamespace(name=getattr(asset,'name','asset'),matrix_world=asset.matrix_world,
                          get=lambda _key,_default=None:'{}')
    pose,scale=_object_record(proxy,'pose')
    return (_vector(pose['position_m'],'Asset position'),
            _vector(pose['rotation_deg'],'Asset rotation'),scale)


def validate_asset_helper(root):
    """New helpers keep authored world dimensions despite later asset scaling."""
    if not root.get('wz09_world_size_helper'):return
    asset=root.parent
    if (asset is None or not asset.get('wz09_asset_placement')
            or root.get('wz09_world_size_asset_id')!=asset.get('wz09_asset_placement_id')):
        raise ValueError('World-size helper has a missing/different asset binding')
    asset_binding_pose(asset)
    constraints=list(root.constraints)
    if len(constraints)!=1:raise ValueError('World-size helper constraint is missing or changed')
    limit=constraints[0]
    if (limit.type!='LIMIT_SCALE' or limit.owner_space!='WORLD' or limit.mute
            or not math.isfinite(limit.influence) or abs(limit.influence-1)>1e-6 or not limit.use_transform_limit
            or any(not getattr(limit,'use_'+bound+'_'+axis) or not math.isfinite(getattr(limit,bound+'_'+axis))
                   or abs(getattr(limit,bound+'_'+axis)-1)>1e-6
                   for bound in ('min','max') for axis in 'xyz')):
        raise ValueError('World-size helper constraint is missing or changed')
    _position,_rotation,scale=asset_binding_pose(root)
    if any(abs(v-1)>1e-5 for v in scale):
        raise ValueError('World-size helper has not evaluated to unit world scale')


def create_asset_helper(context,asset,*,name='Asset gameplay bounds',marker_kind=None):
    """Keep asset TRS intact; native WORLD Limit Scale fixes profile dimensions.

    The returned state allows callers to roll back only this binding transaction.
    Source meshes/materials are never touched. Legacy native roots use no helper.
    """
    import bpy
    from mathutils import Matrix,Euler,Vector
    if not asset.get('wz09_asset_placement'):raise ValueError('Expected an authored asset placement')
    position,rotation,_scale=asset_binding_pose(asset)
    owner=resolve_owner_map(asset)
    if owner is None:raise ValueError('Asset helper needs an existing owner map')
    if marker_kind not in {None,'loot_crate','buy_station','player_spawn','poi'}:
        raise ValueError('Unsupported asset helper marker kind')
    previous_marker=asset.get('wz09_asset_marker_id');root=None
    if marker_kind and previous_marker:
        root=next((o for o in context.scene.objects if o.get('wz09_id')==previous_marker),None)
        if root is None or root.get('wz09_type')!=marker_kind or root.parent is not asset:
            raise ValueError('Placement marker does not match asset helper type')
        if root.get('wz09_action_element') or root.get('wz09_prefab'):
            raise ValueError('Placement marker already has gameplay bindings')
        position,rotation,_scale=asset_binding_pose(root)
        if root.constraints:raise ValueError('Existing asset marker has unsupported constraints')
    created=root is None
    state={'created':created,'asset':asset,'previous_marker':previous_marker}
    if created:
        root=bpy.data.objects.new(name,None);context.scene.collection.objects.link(root)
    else:
        state.update(properties=dict(root.items()),parent=root.parent,
                     matrix_world=root.matrix_world.copy(),parent_inverse=root.matrix_parent_inverse.copy(),
                     display={key:getattr(root,key) for key in
                              ('empty_display_type','empty_display_size','show_name','show_in_front') if hasattr(root,key)})
    state['constraints']=tuple(root.constraints);state['children']=tuple(root.children)
    try:
        root.parent=asset;root.matrix_parent_inverse=Matrix.Identity(4)
        root.matrix_world=Matrix.LocRotScale(Vector(position),Euler([math.radians(v) for v in rotation],'XYZ').to_quaternion(),Vector((1,1,1)))
        limit=root.constraints.new(type='LIMIT_SCALE');limit.name='Authored world size'
        limit.owner_space='WORLD';limit.influence=1.;limit.mute=False;limit.use_transform_limit=True
        for axis in 'xyz':
            for bound in ('min','max'):
                setattr(limit,'use_'+bound+'_'+axis,True);setattr(limit,bound+'_'+axis,1.)
        root['wz09_world_size_helper']=True;root['wz09_world_size_asset_id']=str(asset['wz09_asset_placement_id'])
        root['wz09_preview_only']=True
        if not root.get('wz09_preview_id'):root['wz09_preview_id']=str(uuid.uuid4())
        if marker_kind:
            root['wz09_type']=marker_kind
            if not root.get('wz09_id'):root['wz09_id']=str(uuid.uuid4())
            root['wz09_profile']='default';properties=json.loads(root.get('wz09_properties_json','{}'))
            properties.update(asset_placement_id=str(asset['wz09_asset_placement_id']),
                              editor_owner_map=str(owner['wz09_id']),runtime_integrated=False)
            root['wz09_properties_json']=_encoded(properties);asset['wz09_asset_marker_id']=root['wz09_id']
            if marker_kind in {'loot_crate','buy_station'}:
                root['wz09_loot_table' if marker_kind=='loot_crate' else 'wz09_shop_catalog']='standard'
        else:root['wz09_editor_overlay']=True
        root.empty_display_type='CUBE';root.empty_display_size=.75;root.show_name=True;root.show_in_front=True
        validate_asset_helper(root)
        return root,state
    except Exception:
        rollback_asset_helper(context,root,state)
        raise


def rollback_asset_helper(context,root,state):
    """Undo this helper transaction without deleting source/placement objects."""
    import bpy
    asset=state['asset'];old=state['previous_marker']
    if old is None:
        if 'wz09_asset_marker_id' in asset and asset.get('wz09_asset_marker_id')==root.get('wz09_id'):
            del asset['wz09_asset_marker_id']
    else:asset['wz09_asset_marker_id']=old
    for child in list(root.children):
        if any((child.as_pointer() if hasattr(child,'as_pointer') else id(child))==
               (old_child.as_pointer() if hasattr(old_child,'as_pointer') else id(old_child)) for old_child in state['children']):continue
        if child.get('wz09_action_contour') or child.get('wz09_prefab_contour'):
            data=child.data;bpy.data.objects.remove(child,do_unlink=True)
            if data is not None and data.users==0:bpy.data.meshes.remove(data)
    if state['created']:bpy.data.objects.remove(root,do_unlink=True)
    else:
        for constraint in list(root.constraints):
            if not any(constraint is previous for previous in state['constraints']):root.constraints.remove(constraint)
        root.parent=state['parent'];root.matrix_parent_inverse=state['parent_inverse'];root.matrix_world=state['matrix_world']
        for key in list(root.keys()):del root[key]
        for key,value in state['properties'].items():root[key]=value
        for key,value in state['display'].items():setattr(root,key,value)


def _wire(context, root, record, bpy, *, replace_existing=True):
    vertices, edges = contour_geometry(record)
    old = [c for c in root.children if c.get('wz09_action_contour')] if replace_existing else []
    mesh = bpy.data.meshes.new('WZ Action Contour')
    mesh.from_pydata(vertices, edges, []); mesh.update()
    obj = bpy.data.objects.new('Action · '+record['kind']+' · radius', mesh)
    context.scene.collection.objects.link(obj); obj.parent = root
    obj['wz09_action_contour'] = True; obj['wz09_action_id'] = record['id']
    obj['wz09_preview_only'] = True
    obj['wz09_preview_id'] = str(uuid.uuid4())
    obj.hide_render = True; obj.hide_select = True; obj.show_in_front = True
    obj.display_type = 'WIRE'
    obj.color = {'destructible': (1, .25, .05, 1), 'loot_crate': (.25, 1, .35, 1),
                 'buy_station': (1, .7, .05, 1), 'trigger': (.65, .2, 1, 1)}[record['kind']]
    for child in old:
        data = child.data
        bpy.data.objects.remove(child, do_unlink=True)
        if data and data.users == 0: bpy.data.meshes.remove(data)


def _target(context,*,draw=False):
    obj = context.active_object
    while obj is not None:
        if obj.get('wz09_asset_placement') and not obj.get('wz09_action_element'):
            linked = obj.get('wz09_asset_marker_id')
            if draw:
                from .authoring_overlay import browser_records
                rows,_error,_pending=browser_records(context.scene,'action')
                marker=next((row['_object'] for row in rows if linked and row.get('marker_id')==linked
                    and row.get('asset_placement_id')==obj.get('wz09_asset_placement_id')),None)
            else:
                marker = next((c for c in obj.children if linked and c.get('wz09_id') == linked
                               and c.get('wz09_action_element')), None)
            if marker is not None:
                return marker
            helper=next((c for c in obj.children if c.get('wz09_world_size_helper') and c.get('wz09_action_element')),None)
            if helper is not None:return helper
        if obj.get('wz09_action_element') or obj.get('wz09_asset_placement') or obj.get('wz09_type') in {'loot_crate', 'buy_station'}:
            return obj
        if obj.get('wz09_type') == 'map_instance':
            return None
        obj = obj.parent
    return None


def create_action(context, record, *, bind_object=None, _existing_ids=None,
                  _source_verified=False, _owner=None, _helper_transaction=None):
    """Tag a placement/marker or create a new explicit action marker at cursor."""
    import bpy
    from mathutils import Matrix, Euler, Vector
    r = validate_records([record], verify_sources=not _source_verified)[0]
    if getattr(context, 'mode', 'OBJECT') != 'OBJECT':
        raise ValueError('Switch to Object Mode before creating an action')
    duplicate=(r['id'] in _existing_ids if _existing_ids is not None else
               any(o.get('wz09_action_element') and o.get('wz09_action_id') == r['id'] for o in context.scene.objects))
    if duplicate:
        raise ValueError('Action ID already exists; import not duplicated')
    root, created, linked_asset = bind_object, False, None
    helper_state=None
    if root is not None:
        actual_asset=root if root.get('wz09_asset_placement') else root.parent
        actual_placement=(str(actual_asset['wz09_asset_placement_id'])
                          if actual_asset is not None and actual_asset.get('wz09_asset_placement') else None)
        actual_marker=(str(root.get('wz09_id')) if root.get('wz09_type') in {'loot_crate','buy_station'}
                       else root.get('wz09_asset_marker_id'))
        if r['asset_placement_id'] is not None and r['asset_placement_id']!=actual_placement:
            raise ValueError('Imported action placement conflicts with selected object')
        if r['marker_id'] is not None and r['marker_id']!=actual_marker:
            raise ValueError('Imported action marker conflicts with selected object')
    if root is not None and root.get('wz09_action_element'):
        raise ValueError('Selection already has an action; use Read selection / Apply settings')
    if root is not None and root.get('wz09_type') in {'loot_crate', 'buy_station'} and root.get('wz09_type') != r['kind']:
        raise ValueError('Existing marker kind does not match action; choose matching type')
    if root is not None and root.get('wz09_asset_placement'):
        asset = root
        marker_kind=r['kind'] if r['kind'] in {'loot_crate','buy_station'} else None
        root,helper_state=create_asset_helper(context,asset,name='Action · '+r['kind'],marker_kind=marker_kind)
        linked_asset,created=asset,helper_state['created']
    if root is not None:
        if not root.get('wz09_asset_placement') and not root.get('wz09_world_size_helper') and root.get('wz09_type') not in {'loot_crate', 'buy_station'}:
            raise ValueError('Bind only an authored placement or gameplay marker')
        owner = resolve_owner_map(root)
        r['map_instance_id'] = str(owner['wz09_id']) if owner else None
        if root.get('wz09_asset_placement'):
            r['asset_placement_id'] = str(root['wz09_asset_placement_id'])
            marker_id = root.get('wz09_asset_marker_id')
            if marker_id:
                marker = next((o for o in context.scene.objects if o.get('wz09_id') == marker_id), None)
                if marker is None or marker.get('wz09_type') != r['kind']:
                    if r['kind'] in {'loot_crate', 'buy_station'}:
                        raise ValueError('Placement marker does not match action type')
                elif r['kind'] in {'loot_crate', 'buy_station'}:
                    r['marker_id'] = marker_id
        else:
            r['marker_id'] = str(root['wz09_id']) if root.get('wz09_type') in {'loot_crate','buy_station'} else None
            parent = root.parent
            if parent is not None and parent.get('wz09_asset_placement'):
                r['asset_placement_id'] = str(parent['wz09_asset_placement_id'])
        r['position_m'] = list(root.matrix_world.translation)
        r['rotation_deg'] = [math.degrees(v) for v in root.matrix_world.to_euler('XYZ')]
    else:
        owner = _owner if _owner is not None else next((o for o in context.scene.objects if o.get('wz09_type') == 'map_instance' and o.get('wz09_id') == r['map_instance_id']), None)
        if owner is not None and (owner.get('wz09_type')!='map_instance' or str(owner.get('wz09_id'))!=r['map_instance_id']):
            raise ValueError('Action owner map does not match record')
        if r['map_instance_id'] is not None and owner is None:
            raise ValueError('Action owner map is missing')
        root = bpy.data.objects.new('Action · '+r['kind'], None); created = True
        context.scene.collection.objects.link(root)
        root.empty_display_type = 'CUBE' if r['kind'] in {'loot_crate', 'buy_station'} else 'SPHERE'
        root.empty_display_size = .75; root.show_name = True; root.show_in_front = True
        root['wz09_preview_only'] = True
        root['wz09_preview_id'] = str(uuid.uuid4())
        if owner is not None:
            root.parent = owner; root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_world = Matrix.LocRotScale(Vector(r['position_m']), Euler([math.radians(v) for v in r['rotation_deg']], 'XYZ').to_quaternion(), Vector((1, 1, 1)))
        if r['kind'] in {'loot_crate', 'buy_station'}:
            root['wz09_type'] = r['kind']; root['wz09_id'] = r['marker_id'] or str(uuid.uuid4())
            root['wz09_profile'] = 'default'; root['wz09_properties_json'] = '{}'
            root['wz09_loot_table' if r['kind'] == 'loot_crate' else 'wz09_shop_catalog'] = 'standard'
            r['marker_id'] = root['wz09_id']
    try:
        _wire(context, root, r, bpy,replace_existing=not created)
        root['wz09_action_element'] = True; root['wz09_action_id'] = r['id']
        root['wz09_action_json'] = _encoded(r)
    except Exception:
        if helper_state is not None:
            rollback_asset_helper(context,root,helper_state)
        elif created:
            if linked_asset is not None and linked_asset.get('wz09_asset_marker_id') == root.get('wz09_id'):
                del linked_asset['wz09_asset_marker_id']
            bpy.data.objects.remove(root, do_unlink=True)
        raise
    for obj in context.selected_objects: obj.select_set(False)
    root.select_set(True); context.view_layer.objects.active = root
    if _existing_ids is not None: _existing_ids.add(r['id'])
    if _helper_transaction is not None and helper_state is not None:
        _helper_transaction.update(root=root,state=helper_state)
    return root


def _settings_record(settings, context, previous=None):
    owner = resolve_owner_map(context.active_object)
    r = copy.deepcopy(previous) if previous else new_record(settings.kind, position=list(context.scene.cursor.location),
        map_instance_id=str(owner['wz09_id']) if owner else None)
    if previous and previous['kind'] != settings.kind:
        raise ValueError('Changing an existing action type would break its bindings; create a separate action')
    if previous and previous['behavior_ref'] != settings.behavior_ref:
        r['evidence'] = unverified_evidence()
    for field in ('kind', 'behavior_ref', 'radius_m', 'explosion_radius_m', 'enabled'):
        r[field] = getattr(settings, field)
    r['health'] = settings.health if settings.override_health else None
    r['dependencies'] = [v.strip() for v in re.split('[;\n]', settings.dependencies) if v.strip()]
    r['missing_dependencies'] = [v.strip() for v in re.split('[;\n]', settings.missing_dependencies) if v.strip()]
    return validate_records([r])[0]


def register():
    import bpy
    from bpy.props import StringProperty, EnumProperty, FloatProperty, BoolProperty, PointerProperty, IntProperty
    from bpy_extras.io_utils import ImportHelper
    from pathlib import Path

    class WZ09_ActionSettings(bpy.types.PropertyGroup):
        kind: EnumProperty(name='Action type', items=[('destructible', 'Destructible / vehicle', 'Explicit native destruction binding, never inferred from a mesh name'), ('loot_crate', 'Loot crate', 'Reuse the existing Royale crate behavior'), ('buy_station', 'Buy station', 'Reuse the existing shop behavior'), ('trigger', 'Trigger / action volume', 'Explicit referenced trigger behavior')], default='loot_crate')
        behavior_ref: StringProperty(name='Behavior / profile reference', description='Opaque reference to an existing runtime behavior; editor never executes scripts')
        radius_m: FloatProperty(name='Use / trigger radius', min=.001, default=2.5, unit='LENGTH')
        explosion_radius_m: FloatProperty(name='Explosion preview radius', min=0, default=0, unit='LENGTH')
        override_health: BoolProperty(name='Explicit health override', default=False)
        health: FloatProperty(name='Health', default=100, min=.001)
        dependencies: StringProperty(name='Dependencies (; separated)', description='Model, wreck, sound, FX, collision or profile references; separate with semicolons')
        missing_dependencies: StringProperty(name='Missing dependencies (; separated)')
        enabled: BoolProperty(name='Enabled', default=True)
        list_search: StringProperty(name='Search action elements',update=lambda self,ctx:setattr(self,'list_page',1))
        list_owner_only: BoolProperty(name='Selected map only',default=True,update=lambda self,ctx:setattr(self,'list_page',1))
        list_kind: EnumProperty(name='List',items=[('ALL','All types','')]+[(k,k,'') for k in KINDS],
            update=lambda self,ctx:setattr(self,'list_page',1))
        list_page: IntProperty(name='Page',default=1,min=1,max=10000)

    class WZ09_OT_action_add(bpy.types.Operator):
        bl_idname = 'wz09.action_add'; bl_label = 'Create Action at Cursor'; bl_options = {'REGISTER', 'UNDO'}
        bind_selected: BoolProperty(default=False)
        def execute(self, context):
            try:
                target = _target(context) if self.bind_selected else None
                if self.bind_selected and target is None:
                    raise ValueError('Select an authored asset placement or loot/shop marker to bind')
                create_action(context, _settings_record(context.scene.wz09_action_settings, context), bind_object=target)
                return {'FINISHED'}
            except (ValueError, RuntimeError, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_action_load(bpy.types.Operator):
        bl_idname = 'wz09.action_load'; bl_label = 'Read Selected Action Settings'
        def execute(self, context):
            try:
                obj = _target(context)
                if obj is None or not obj.get('wz09_action_element'): raise ValueError('Select an action first')
                r = _record_for_settings(obj)
                settings = context.scene.wz09_action_settings
                for field in ('kind', 'behavior_ref', 'radius_m', 'explosion_radius_m', 'enabled'):
                    setattr(settings, field, r[field])
                settings.override_health = r['health'] is not None
                settings.health = r['health'] or 100
                settings.dependencies = '; '.join(r['dependencies'])
                settings.missing_dependencies = '; '.join(r['missing_dependencies'])
                return {'FINISHED'}
            except (ValueError, StopIteration, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_action_apply(bpy.types.Operator):
        bl_idname = 'wz09.action_apply'; bl_label = 'Apply to Selected Action'; bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                obj = _target(context)
                if obj is None or not obj.get('wz09_action_element'): raise ValueError('Select an action first')
                r = _settings_record(context.scene.wz09_action_settings, context, previous=next(r for r in serialize_scene(context.scene) if r['id'] == obj['wz09_action_id']))
                if any(abs(v-1) > 1e-5 for v in obj.matrix_world.to_scale()):
                    raise ValueError('Apply object scale before updating an existing bound action')
                _wire(context, obj, r, bpy)
                obj['wz09_action_json'] = _encoded(r)
                return {'FINISHED'}
            except (ValueError, RuntimeError, StopIteration, OSError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_action_import(bpy.types.Operator, ImportHelper):
        bl_idname = 'wz09.action_import'; bl_label = 'Import Action Semantics Sidecar'; bl_options = {'REGISTER', 'UNDO'}
        filename_ext = '.json'
        filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
        def execute(self, context):
            created = []
            try:
                if Path(self.filepath).stat().st_size > 16*1024*1024: raise ValueError('Action sidecar exceeds 16 MiB limit')
                payload = Path(self.filepath).read_bytes()
                if len(payload) > 16*1024*1024: raise ValueError('Action sidecar exceeds 16 MiB limit')
                doc = json.loads(payload.decode('utf-8'))
                if doc.get('kind') != 'wz09_native_action_sidecar' or doc.get('version') != 1:
                    raise ValueError('Expected wz09_native_action_sidecar version 1')
                marker_ids = {o.get('wz09_id') for o in context.scene.objects if o.get('wz09_type') in {'loot_crate', 'buy_station'}}
                placement_ids = {o.get('wz09_asset_placement_id') for o in context.scene.objects if o.get('wz09_asset_placement')}
                map_ids = {o.get('wz09_id') for o in context.scene.objects if o.get('wz09_type') == 'map_instance'}
                records = validate_records(doc.get('records'), map_ids, verify_sources=True,
                                           marker_ids=marker_ids, asset_placement_ids=placement_ids)
                existing = {o.get('wz09_action_id') for o in context.scene.objects if o.get('wz09_action_element')}
                if existing.intersection(r['id'] for r in records): raise ValueError('Sidecar contains existing action IDs')
                # Validate all target bindings before making any scene changes.
                pairs = []
                for r in records:
                    target = next((o for o in context.scene.objects if r['marker_id'] and o.get('wz09_id') == r['marker_id']), None)
                    if target is None and r['asset_placement_id']:
                        target = next((o for o in context.scene.objects if o.get('wz09_asset_placement') and o.get('wz09_asset_placement_id') == r['asset_placement_id']), None)
                    if target is not None and target.get('wz09_action_element'):
                        raise ValueError('Sidecar target already has an action')
                    pairs.append((r, target))
                if len({id(target) for _, target in pairs if target is not None}) != sum(target is not None for _, target in pairs):
                    raise ValueError('Multiple imported actions bind the same object')
                for r, target in pairs:
                    root = create_action(context, r, bind_object=target)
                    created.append((root, target is None or root is not target, target))
                self.report({'WARNING'}, f'{len(created)} action profiles imported; native dependencies/runtime untested')
                return {'FINISHED'}
            except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
                for root, was_new, target in reversed(created):
                    for child in [c for c in root.children if c.get('wz09_action_contour')]:
                        data = child.data
                        bpy.data.objects.remove(child, do_unlink=True)
                        if data and data.users == 0: bpy.data.meshes.remove(data)
                    if was_new:
                        if target is not None and target.get('wz09_asset_marker_id') == root.get('wz09_id'):
                            del target['wz09_asset_marker_id']
                        bpy.data.objects.remove(root, do_unlink=True)
                    else:
                        for key in ('wz09_action_element', 'wz09_action_id', 'wz09_action_json'):
                            if key in root: del root[key]
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_action_select(bpy.types.Operator):
        bl_idname = 'wz09.action_select'; bl_label = 'Select Action Element'
        action_id: StringProperty()
        def execute(self, context):
            obj = next((o for o in context.scene.objects if o.get('wz09_action_element') and o.get('wz09_action_id') == self.action_id), None)
            if obj is None:
                self.report({'ERROR'}, 'Action no longer exists'); return {'CANCELLED'}
            for selected in context.selected_objects: selected.select_set(False)
            obj.hide_set(False); obj.select_set(True); context.view_layer.objects.active = obj
            return bpy.ops.wz09.action_load()

    class WZ09_OT_action_detach(bpy.types.Operator):
        bl_idname = 'wz09.action_detach'; bl_label = 'Detach Action Profile'; bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            obj = _target(context)
            if obj is None or not obj.get('wz09_action_element'):
                self.report({'ERROR'}, 'Select an action first'); return {'CANCELLED'}
            for child in [c for c in obj.children if c.get('wz09_action_contour')]:
                data = child.data
                bpy.data.objects.remove(child, do_unlink=True)
                if data and data.users == 0: bpy.data.meshes.remove(data)
            for key in ('wz09_action_element', 'wz09_action_id', 'wz09_action_json'):
                if key in obj: del obj[key]
            self.report({'INFO'}, 'Action profile removed; visual and gameplay marker retained')
            return {'FINISHED'}

    class WZ09_PT_actions(bpy.types.Panel):
        bl_label = 'Interactive elements'; bl_idname = 'WZ09_PT_actions'
        bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = 'WZ2009'; bl_parent_id = 'WZ09_PT_main'
        @classmethod
        def poll(cls, context): return getattr(context.scene, 'wz09_workspace', '') in {'ACTIONS', 'PLACE'}
        def draw(self, context):
            layout, s = self.layout, context.scene.wz09_action_settings
            from .authoring_overlay import draw_contour_focus,browser_records
            draw_contour_focus(layout,context)
            from .editor_ux import icon
            layout.prop(s, 'kind', icon=icon(ICONS[s.kind]))
            layout.prop(s, 'behavior_ref'); layout.prop(s, 'radius_m')
            if s.kind == 'destructible':
                layout.prop(s, 'explosion_radius_m'); layout.prop(s, 'override_health')
                if s.override_health: layout.prop(s, 'health')
            layout.prop(s, 'enabled')
            layout.prop(s, 'dependencies'); layout.prop(s, 'missing_dependencies')
            layout.operator('wz09.action_add',text='Create action at cursor', icon='ADD')
            layout.operator('wz09.action_add', text='Bind to selection', icon='LINKED').bind_selected = True
            row = layout.row(align=True)
            row.operator('wz09.action_load', text='Read selection', icon='EYEDROPPER')
            row.operator('wz09.action_apply', text='Apply', icon='CHECKMARK')
            obj = _target(context,draw=True)
            if obj is not None and obj.get('wz09_action_element'):
                r = json.loads(obj.get('wz09_action_json', '{}'))
                box = layout.box(); box.label(text='Selected: '+obj.name, icon=icon(ICONS.get(r.get('kind'), 'INFO')))
                box.label(text='State: '+authoring_status(r).replace('_', ' '), icon='INFO')
                box.label(text='Runtime adapter / gameplay untested')
                box.prop(obj, 'location', text='Local position')
                if r.get('asset_placement_id'): box.label(text='Visual placement linked', icon='LINKED')
                if r.get('marker_id'): box.label(text='Existing gameplay marker linked', icon='LINKED')
            from .authoring_list import filter_page
            owner=resolve_owner_map(context.active_object)
            records,index_error,index_pending=browser_records(context.scene,'action')
            layout.operator('wz09.authoring_index_refresh',text='Refresh list',icon='FILE_REFRESH')
            if index_pending:layout.label(text='List updates outside viewport drawing',icon='TIME')
            if index_error:layout.label(text=index_error[:240],icon='ERROR')
            layout.prop(s,'list_search',text='',icon='VIEWZOOM')
            row=layout.row(align=True);row.prop(s,'list_owner_only');row.prop(s,'list_kind',text='')
            page=filter_page(records,query=s.list_search,
                owner_map_id=str(owner['wz09_id']) if owner is not None and s.list_owner_only else None,
                kind=s.list_kind,page=s.list_page)
            row=layout.row(align=True);row.prop(s,'list_page');row.label(text=f"{page['page']}/{page['pages']} · {page['total']} Treffer")
            for raw in page['items']:
                item=raw['_object']
                op = layout.operator('wz09.action_select', text=item.name, icon=icon(ICONS.get(raw.get('kind'), 'INFO')), depress=item is obj)
                op.action_id = str(item['wz09_action_id'])
            if obj is not None and obj.get('wz09_action_element'):
                layout.operator('wz09.action_detach', icon='UNLINKED')
            layout.operator('wz09.action_import', icon='IMPORT')
            layout.label(text='Model names never imply destruction', icon='INFO')

    _CLASSES[:] = [WZ09_ActionSettings, WZ09_OT_action_add, WZ09_OT_action_load,
                   WZ09_OT_action_apply, WZ09_OT_action_import, WZ09_OT_action_select,
                   WZ09_OT_action_detach, WZ09_PT_actions]
    for cls in _CLASSES: bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_action_settings = PointerProperty(type=WZ09_ActionSettings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene, 'wz09_action_settings'): del bpy.types.Scene.wz09_action_settings
    for cls in reversed(_CLASSES): bpy.utils.unregister_class(cls)
    _CLASSES.clear()
