"""Typed editor prefab configurations referencing existing actions/assets.

These are declarative blueprints. A visible model and a configured profile do
not grant native collision, damage execution, networking or runtime validation.
"""
import copy
import uuid
try:
    from .audio_zones import _number,_vector,_text,validate_evidence,unverified_evidence
except ImportError:
    from audio_zones import _number,_vector,_text,validate_evidence,unverified_evidence

KINDS=('vehicle','loot_crate','buy_station','loadout_drop','player_spawn','redeploy',
       'objective','trigger','door','destructible','audio_emitter','vfx_emitter')
_COMMON={'enabled':True,'interaction_radius_m':2.5,'region_ref':'','audio_ref':'','animation_ref':''}
_PARAMS={
    'vehicle':{'collider_profile':'','damage_profile':'','explosion_event':'','fx_ref':'',
               'destroyed_model_asset_id':None,'max_health':None,'driveable':False},
    'loot_crate':{'crate_type':'custom','loot_table':'standard','opened':False,'restock_s':None},
    'buy_station':{'shop_profile':'standard'},
    'loadout_drop':{'loadout_profile':'','squad_only':False},
    'player_spawn':{'team_ref':'','spawn_profile':''},
    'redeploy':{'redeploy_profile':'','single_use':False},
    'objective':{'objective_ref':'','event_ref':''},
    'trigger':{'event_ref':'','shape':'box','dimensions_m':[4.,4.,4.],'single_use':False},
    'door':{'door_profile':'','collider_profile':'','opened':False,'locked':False,'event_ref':''},
    'destructible':{'collider_profile':'','damage_profile':'','explosion_event':'','fx_ref':'',
                    'destroyed_model_asset_id':None,'max_health':None},
    'audio_emitter':{'sound_bus':'ambient','volume':1.,'inner_radius_m':3.,'outer_radius_m':20.},
    'vfx_emitter':{'fx_ref':'','looping':False,'event_ref':''},
}


def parameters_for_kind(kind):
    if kind not in KINDS:raise ValueError('Unsupported prefab kind')
    return copy.deepcopy(dict(_COMMON,**_PARAMS[kind]))


def _record_template(kind,map_instance_id,identity,position=(0,0,0)):
    return {'id':identity,'kind':kind,'map_instance_id':map_instance_id,
        'asset_placement_id':None,'marker_id':None,'action_element_id':None,
        'position_m':list(position),'rotation_deg':[0.,0.,0.],
        'parameters':parameters_for_kind(kind),
        'evidence':unverified_evidence(),'runtime_integrated':False,'collision_verified':False,
        'status':'gameplay_blueprint'}


def new_record(kind,*,map_instance_id,position=(0,0,0)):
    return _record_template(kind,map_instance_id,str(uuid.uuid4()),position)


def missing_dependencies(record):
    params=record['parameters'];kind=record['kind'];missing=[]
    if kind in {'vehicle','door','destructible'} and not params['collider_profile']:missing.append('native_collider_profile')
    required={'vehicle':['damage_profile'],'destructible':['damage_profile'],'loot_crate':['loot_table'],
        'buy_station':['shop_profile'],'loadout_drop':['loadout_profile'],'player_spawn':['spawn_profile'],
        'redeploy':['redeploy_profile'],'objective':['objective_ref'],'trigger':['event_ref'],
        'door':['door_profile'],'audio_emitter':['audio_ref'],'vfx_emitter':['fx_ref']}[kind]
    missing.extend(key for key in required if not params.get(key))
    missing.append('runtime_adapter_unverified')
    return missing


def validate_records(records,*,maps=None,placements=None,markers=None,actions=None,all_ids=None,verify_sources=False):
    if not isinstance(records,list):raise ValueError('Prefabs must be an array')
    seen=set(all_ids or ());result=[];cache={}
    for raw in records:
        if not isinstance(raw,dict) or raw.get('kind') not in KINDS:raise ValueError('Unknown prefab kind')
        record=copy.deepcopy(raw);expected=_record_template(record['kind'],'validation-only','validation-only')
        if set(record)!=set(expected):raise ValueError('Prefab fields differ from version 1')
        identity=_text(record['id'],'Prefab ID')
        if identity in seen:raise ValueError('Duplicate prefab/global ID: '+identity)
        seen.add(identity)
        owner=_text(record['map_instance_id'],'Prefab map ID')
        if maps is not None and owner not in maps:raise ValueError('Prefab map is missing')
        for key,values in (('asset_placement_id',placements),('marker_id',markers),('action_element_id',actions)):
            ref=record[key]
            if ref is not None:
                _text(ref,key)
                if values is not None and ref not in values:raise ValueError('Prefab reference missing: '+key)
                linked=values[ref] if isinstance(values,dict) else None
                if isinstance(linked,dict):
                    linked_owner=linked.get('owner_map',linked.get('map_instance_id',linked.get('properties',{}).get('editor_owner_map')))
                    if linked_owner is not None and linked_owner!=owner:raise ValueError('Prefab reference belongs to another map')
        record['position_m']=_vector(record['position_m'],'Prefab position')
        record['rotation_deg']=_vector(record['rotation_deg'],'Prefab rotation')
        params=record['parameters']
        if not isinstance(params,dict) or set(params)!=set(expected['parameters']):raise ValueError('Prefab parameter fields differ for '+record['kind'])
        for key,value in params.items():
            default=expected['parameters'][key]
            if key in {'destroyed_model_asset_id'}:
                if value is not None:_text(value,key)
            elif key in {'max_health','restock_s'}:
                if value is not None:params[key]=_number(value,key,.000001 if key=='max_health' else 0)
            elif isinstance(default,bool):
                if type(value) is not bool:raise ValueError('Prefab '+key+' must be boolean')
            elif isinstance(default,str):_text(value,key,empty=True)
            elif isinstance(default,list):
                params[key]=_vector(value,key)
                if min(params[key])<=0:raise ValueError('Prefab dimensions must be positive')
            else:params[key]=_number(value,key,0)
        if params['interaction_radius_m']<=0:raise ValueError('Interaction radius must be positive')
        if 'shape' in params and params['shape'] not in {'box','cylinder','sphere'}:raise ValueError('Unsupported trigger shape')
        if 'volume' in params and params['volume']>1:raise ValueError('Audio volume exceeds one')
        if 'inner_radius_m' in params and not 0<=params['inner_radius_m']<params['outer_radius_m']:
            raise ValueError('Audio inner radius must be less than outer radius')
        if params.get('driveable') is True:raise ValueError('Driveable vehicles are not verified by this editor')
        if record['status'] not in {'visual_only','gameplay_blueprint'} or record['runtime_integrated'] is not False or record['collision_verified'] is not False:
            raise ValueError('Prefab cannot claim runtime_verified/native collision without runtime evidence')
        record['evidence']=validate_evidence(record['evidence'],verify_sources=verify_sources,cache=cache)
        result.append(record)
    return result
