"""Strict cinematic authoring data over the existing timed aircraft markers.

Route bindings carry references, never a second route geometry database.
Events are declarative runtime requests; validation never executes a script,
FX, sound, animation or player-control handoff.
"""
import copy
import hashlib
import json
import math
import uuid
try:from .authoring_helpers import aircraft_routes
except ImportError:from authoring_helpers import aircraft_routes

EVENT_KINDS=('audio','fx','animation','player_handoff','screen_fade','aircraft_insertion')
PARAMETERS={'audio':{'volume':1.,'fade_in_s':0.,'fade_out_s':0.,'loop':False},
            'fx':{'scale':1.},'animation':{'speed':1.,'loop':False},
            'player_handoff':{'camera_blend_s':.5},
            'screen_fade':{'color':[0.,0.,0.],'from_alpha':0.,'to_alpha':1.,'duration_s':1.},
            'aircraft_insertion':{'aircraft_placement_id':'','route_id':'','route_elapsed_s':0.,
                'jump_open_s':0.,'jump_close_s':0.,'spawn_offset_m':[0.,0.,0.],
                'camera_id':'','camera_blend_s':.5}}
AXES=('FORWARD_X','FORWARD_Y','FORWARD_Z','TRACK_NEGATIVE_X','TRACK_NEGATIVE_Y','TRACK_NEGATIVE_Z')
SEQUENCE_KEYS=('kind','version','units','id','name','fps','duration_s','frame_start','cameras',
               'route_bindings','cuts','events','source_routes_sha256','runtime_integrated',
               'physical_scale_verified','clearance_verified')


def number(value,name,minimum=None,maximum=None):
    try:valid=type(value) in (int,float) and math.isfinite(value)
    except OverflowError:valid=False
    if not valid:raise ValueError(name+': finite number required')
    if minimum is not None and value<minimum or maximum is not None and value>maximum:raise ValueError(name+': outside supported bounds')
    return float(value)


def text(value,name,empty=False):
    if not isinstance(value,str) or len(value)>512 or not empty and not value.strip():raise ValueError(name+': valid text required')
    return value


def vector(value,name):
    if not isinstance(value,(list,tuple)) or len(value)!=3:raise ValueError(name+': three coordinates required')
    return [number(x,name,-1e8,1e8) for x in value]


def _keys(record,required,where):
    if not isinstance(record,dict) or set(record)!=set(required):raise ValueError(where+': missing or unknown fields')


def route_fingerprint(markers,route_ids=()):
    """Hash route positions at the canonical world's seven-decimal precision.

    Blender's raw float32 translations and scene_io.v3's exported coordinates
    describe the same route. Normalize only that serialization roundoff (and
    signed zero); IDs, order, timing and meaningful position edits remain bound.
    """
    wanted=set(route_ids)
    records=[{'id':m['id'],'kind':'aircraft_keyframe',
              'position_m':[round(c,7) or 0.0 for c in vector(m['position_m'],'route position')],
              'properties':{k:m.get('properties',{}).get(k) for k in ('route_id','sequence','time_s')}}
             for m in markers if m.get('kind')=='aircraft_keyframe' and m.get('properties',{}).get('route_id') in wanted]
    aircraft_routes(records)
    return hashlib.sha256(json.dumps(sorted(records,key=lambda x:x['id']),sort_keys=True,
                                   separators=(',',':'),allow_nan=False).encode()).hexdigest()


def route_sample(markers,route_id,elapsed_s):
    """Sample existing route in elapsed seconds, returning design speed/heading."""
    time=number(elapsed_s,'elapsed_s',0,86400)
    route=next((r for r in aircraft_routes(markers)['routes'] if r['route_id']==route_id),None)
    if route is None or not route['complete_polyline']:raise ValueError('A complete existing timed route is required')
    times=route['times_s'];points=[vector(p,'route.position') for p in route['positions_m']]
    absolute=min(times[-1],times[0]+time)
    index=next((i for i in range(len(times)-1) if absolute<=times[i+1]),len(times)-2)
    factor=max(0.,min(1.,(absolute-times[index])/(times[index+1]-times[index])))
    position=[a+(b-a)*factor for a,b in zip(points[index],points[index+1])]
    velocity=[(b-a)/(times[index+1]-times[index]) for a,b in zip(points[index],points[index+1])]
    speed=math.hypot(*velocity)
    if not math.isfinite(speed):raise ValueError('Route times produce a nonfinite design velocity')
    return {'position_m':position,'velocity_m_s':velocity,'speed_m_s':speed,
            'route_id':route_id,'keyframe_ids':route['keyframe_ids'][index:index+2],
            'runtime_verified':False,'clearance_verified':False}


def new_sequence(name='Match Intro',fps=24,duration_s=30,frame_start=1):
    return {'kind':'wz09_cinematic_timeline','version':1,'units':'editor_design_meters','id':str(uuid.uuid4()),'name':name,
            'fps':fps,'duration_s':duration_s,'frame_start':frame_start,'cameras':[],
            'route_bindings':[],'cuts':[],'events':[],
            'source_routes_sha256':route_fingerprint([],[]),'runtime_integrated':False,
            'physical_scale_verified':False,'clearance_verified':False}


def new_event(kind,time_s,*,target_id=None,reference=''):
    if kind not in EVENT_KINDS:raise ValueError('Unsupported cinematic event kind')
    return {'id':str(uuid.uuid4()),'kind':kind,'time_s':time_s,'target_id':target_id,
            'reference':reference,'parameters':copy.deepcopy(PARAMETERS[kind])}


def world_ids(world):
    result=set()
    groups=[world.get(k,[]) for k in ('maps','markers','asset_placements','audio_zones','action_elements','geometry')]
    groups += [world.get('nav',{}).get(k,[]) for k in ('nodes','edges')]
    groups += [world.get('composition',{}).get(k,[]) for k in ('regions','terrain_tiles','water_features','scatter_sets')]
    for group in groups:
        for record in group:
            if isinstance(record.get('id'),str):result.add(record['id'])
    return result


def frame_at(document,time_s):
    time=number(time_s,'time_s',0,document['duration_s'])
    return document['frame_start']+int(math.floor(time*document['fps']+.5))


def validate_document(document,world):
    """Validate strict sidecar fields, finite data and existing world references."""
    _keys(document,SEQUENCE_KEYS,'cinematic')
    if document['kind']!='wz09_cinematic_timeline' or type(document['version']) is not int or document['version']!=1:
        raise ValueError('Unsupported cinematic document version')
    if document['units']!='editor_design_meters':raise ValueError('Cinematic coordinates require explicit editor design units')
    for key in ('runtime_integrated','physical_scale_verified','clearance_verified'):
        if document[key] is not False:raise ValueError('Cinematic '+key+' must remain false')
    text(document['name'],'sequence name')
    number(document['fps'],'fps',1,240);number(document['duration_s'],'duration_s',.001,86400)
    if type(document['frame_start']) is not int or not 0<=document['frame_start']<=1048574:
        raise ValueError('frame_start outside Blender timeline bounds')
    if frame_at(document,document['duration_s'])>1048574:raise ValueError('Cinematic exceeds Blender frame budget')
    used=world_ids(world)
    def identity(record,where):
        uid=text(record.get('id'),where+'.id')
        if uid in used:raise ValueError(where+': duplicate global ID')
        used.add(uid)
    identity(document,'sequence')
    for key in ('cameras','route_bindings','cuts','events'):
        if not isinstance(document[key],list) or len(document[key])>10000:raise ValueError('Cinematic list exceeds record budget')
    camera_ids=set()
    for camera in document['cameras']:
        _keys(camera,('id','name','position_m','rotation_deg','transform_space','lens_mm','clip_start_m','clip_end_m','keyframes'),'camera')
        identity(camera,'camera');camera_ids.add(camera['id']);text(camera['name'],'camera name')
        vector(camera['position_m'],'camera position');vector(camera['rotation_deg'],'camera rotation')
        if camera['transform_space'] not in {'world','route_offset'}:raise ValueError('Unknown camera transform space')
        number(camera['lens_mm'],'lens_mm',1,1000)
        number(camera['clip_start_m'],'clip_start_m',.001,1e7)
        number(camera['clip_end_m'],'clip_end_m',camera['clip_start_m']+.001,1e8)
        if not isinstance(camera['keyframes'],list) or len(camera['keyframes'])>100000:raise ValueError('Camera keyframe budget exceeded')
        frames=set()
        for keyframe in camera['keyframes']:
            _keys(keyframe,('frame','data_path','array_index','value','interpolation','handle_left','handle_right'),'camera keyframe')
            number(keyframe['frame'],'keyframe frame',document['frame_start'],frame_at(document,document['duration_s']))
            if keyframe['data_path'] not in {'location','rotation_euler','rotation_quaternion','lens','offset_factor'}:raise ValueError('Unsupported camera keyframe channel')
            maximum=3 if keyframe['data_path']=='rotation_quaternion' else 2 if keyframe['data_path'] in {'location','rotation_euler'} else 0
            if type(keyframe['array_index']) is not int or not 0<=keyframe['array_index']<=maximum:raise ValueError('Invalid keyframe array index')
            number(keyframe['value'],'keyframe value',-1e8,1e8)
            for handle in ('handle_left','handle_right'):
                if not isinstance(keyframe[handle],list) or len(keyframe[handle])!=2:raise ValueError('Keyframe handles need frame/value')
                for value in keyframe[handle]:number(value,'keyframe handle',-1e8,1e8)
            if keyframe['interpolation'] not in {'CONSTANT','LINEAR','BEZIER'}:raise ValueError('Unsupported keyframe interpolation')
            stamp=(keyframe['frame'],keyframe['data_path'],keyframe['array_index'])
            if stamp in frames:raise ValueError('Duplicate camera keyframe channel/time')
            frames.add(stamp)
    asset_ids={a['id'] for a in world.get('asset_placements',[])}
    routes={r['route_id']:r for r in aircraft_routes(world.get('markers',[]))['routes']}
    targets=set();route_ids=[]
    for binding in document['route_bindings']:
        _keys(binding,('id','route_id','target_id','target_kind','time_offset_s','follow_axis','up_axis','orient'),'route binding')
        identity(binding,'route binding');route=text(binding['route_id'],'route_id')
        if route not in routes or not routes[route]['complete_polyline']:raise ValueError('Cinematic binding references missing/incomplete route')
        if binding['target_kind'] not in {'camera','asset'}:raise ValueError('Route target kind must be camera or asset')
        allowed=camera_ids if binding['target_kind']=='camera' else asset_ids
        if binding['target_id'] not in allowed:raise ValueError('Cinematic route target is missing')
        if binding['target_id'] in targets:raise ValueError('A cinematic target cannot follow two routes')
        targets.add(binding['target_id']);route_ids.append(route)
        number(binding['time_offset_s'],'time_offset_s',0,document['duration_s'])
        if routes[route]['duration_s']+binding['time_offset_s']>document['duration_s']+1e-7:raise ValueError('Route extends beyond sequence duration')
        if binding['follow_axis'] not in AXES or binding['up_axis'] not in {'UP_X','UP_Y','UP_Z'} or type(binding['orient']) is not bool:
            raise ValueError('Invalid Follow Path orientation settings')
        if binding['orient'] and binding['follow_axis'].split('_')[-1]==binding['up_axis'][-1]:
            raise ValueError('Follow and up axes must differ')
    if document['source_routes_sha256']!=route_fingerprint(world.get('markers',[]),route_ids):
        raise ValueError('Cinematic routes changed; rebuild the Blender timeline')
    for camera in document['cameras']:
        if (camera['transform_space']=='route_offset')!=(camera['id'] in targets):
            raise ValueError('Camera transform space differs from route binding')
    cut_frames=set()
    for cut in document['cuts']:
        _keys(cut,('id','time_s','camera_id'),'camera cut');identity(cut,'camera cut')
        frame=frame_at(document,cut['time_s'])
        if cut['camera_id'] not in camera_ids:raise ValueError('Camera cut references missing camera')
        if frame in cut_frames:raise ValueError('Two camera cuts collapse to the same frame')
        cut_frames.add(frame)
    marker_ids={m['id']:m for m in world.get('markers',[])}
    audio_ids={a['id'] for a in world.get('audio_zones',[])}
    action_ids={a['id'] for a in world.get('action_elements',[])}
    fades=[];insertions=[]
    for event in document['events']:
        _keys(event,('id','kind','time_s','target_id','reference','parameters'),'event');identity(event,'event')
        if event['kind'] not in EVENT_KINDS:raise ValueError('Unsupported cinematic event')
        frame_at(document,event['time_s']);text(event['reference'],'event reference',empty=True)
        target=event['target_id'];kind=event['kind']
        if target is not None and target not in marker_ids.keys()|audio_ids|asset_ids|action_ids|camera_ids:
            raise ValueError('Cinematic event references missing target')
        if kind=='player_handoff' and (target not in marker_ids or marker_ids[target]['kind']!='player_spawn'):
            raise ValueError('Player handoff requires an existing player_spawn marker')
        if kind=='audio' and not event['reference'] and target not in audio_ids:raise ValueError('Audio event needs an alias or existing audio zone')
        if kind in {'fx','animation'} and not event['reference']:raise ValueError('FX/animation event requires an explicit existing profile reference')
        _keys(event['parameters'],PARAMETERS[kind],'event parameters')
        if kind=='screen_fade':
            if target is not None or event['reference']!='':raise ValueError('Screen fade is a presentation event without target/reference')
            p=event['parameters']
            if not isinstance(p['color'],list) or len(p['color'])!=3:raise ValueError('Screen fade needs an RGB color')
            for value in p['color']:number(value,'fade color',0,1)
            number(p['from_alpha'],'from_alpha',0,1);number(p['to_alpha'],'to_alpha',0,1)
            number(p['duration_s'],'fade duration_s',.001,86400)
            if event['time_s']+p['duration_s']>document['duration_s']+1e-7:raise ValueError('Screen fade extends beyond sequence')
            if frame_at(document,event['time_s'])==frame_at(document,event['time_s']+p['duration_s']):
                raise ValueError('Screen fade endpoints collapse at selected FPS')
            fades.append(event);continue
        if kind=='aircraft_insertion':
            if target not in marker_ids or marker_ids[target]['kind']!='player_spawn':
                raise ValueError('Aircraft insertion requires an existing player_spawn marker')
            if event['reference']!='':raise ValueError('Aircraft insertion uses typed references, not an executable profile')
            p=event['parameters']
            text(p['aircraft_placement_id'],'aircraft_placement_id');text(p['camera_id'],'insertion camera_id')
            if p['aircraft_placement_id'] not in asset_ids:raise ValueError('Aircraft insertion placement is missing')
            if p['camera_id'] not in camera_ids:raise ValueError('Aircraft insertion camera is missing')
            text(p['route_id'],'insertion route_id');vector(p['spawn_offset_m'],'spawn_offset_m')
            matching=[b for b in document['route_bindings'] if b['target_kind']=='asset' and b['target_id']==p['aircraft_placement_id'] and b['route_id']==p['route_id']]
            if len(matching)!=1:raise ValueError('Aircraft insertion requires the aircraft existing route binding')
            binding=matching[0];route=routes[p['route_id']]
            number(p['route_elapsed_s'],'route_elapsed_s',0,route['duration_s'])
            if abs(p['route_elapsed_s']-(event['time_s']-binding['time_offset_s']))>1e-6:
                raise ValueError('Aircraft insertion route time differs from continuous aircraft binding')
            remaining=min(route['duration_s']-p['route_elapsed_s'],document['duration_s']-event['time_s'])
            number(p['jump_open_s'],'jump_open_s',0,remaining)
            number(p['jump_close_s'],'jump_close_s',p['jump_open_s'],remaining)
            if p['jump_close_s']<=p['jump_open_s']:raise ValueError('Aircraft insertion needs a nonempty jump window')
            number(p['camera_blend_s'],'camera_blend_s',0,remaining)
            insertions.append(event);continue
        for key,value in event['parameters'].items():
            if key=='loop':
                if type(value) is not bool:raise ValueError('Event loop must be boolean')
            else:number(value,'event '+key,0 if key!='speed' and key!='scale' else .001,1 if key=='volume' else 10000)
    previous=None
    for fade in sorted(fades,key=lambda e:e['time_s']):
        if previous is not None:
            end=previous['time_s']+previous['parameters']['duration_s']
            if fade['time_s']<end-1e-7:raise ValueError('Screen fades overlap')
            if abs(fade['parameters']['from_alpha']-previous['parameters']['to_alpha'])>1e-7:
                raise ValueError('Screen fade alpha is discontinuous')
            if previous['parameters']['to_alpha']>0 and fade['parameters']['color']!=previous['parameters']['color']:
                raise ValueError('Screen fade changes color while opaque')
        previous=fade
    if len(insertions)>1:raise ValueError('Only one aircraft insertion handoff is supported per sequence')
    return document


def screen_fade_sample(document,time_s):
    """Deterministic absolute timeline opacity; never accumulates frame deltas.

    Fade end values hold through cuts and gaps. Calling at arbitrary frames or
    in reverse produces exactly the same result. Validate the document first.
    """
    time=number(time_s,'fade sample time_s',0,document['duration_s'])
    result={'color':[0.,0.,0.],'alpha':0.}
    for event in sorted((e for e in document['events'] if e['kind']=='screen_fade'),key=lambda e:e['time_s']):
        if time<event['time_s']:break
        p=event['parameters'];factor=min(1.,max(0.,(time-event['time_s'])/p['duration_s']))
        result={'color':list(p['color']),'alpha':p['from_alpha']+(p['to_alpha']-p['from_alpha'])*factor}
    return result


def insertion_sample(document,markers,time_s):
    """Describe the continuing ride and possible jump; executes no gameplay."""
    time=number(time_s,'insertion sample time_s',0,document['duration_s'])
    events=[e for e in document['events'] if e['kind']=='aircraft_insertion' and e['time_s']<=time]
    if not events:return None
    event=max(events,key=lambda e:e['time_s']);p=event['parameters'];elapsed=time-event['time_s']
    sample=route_sample(markers,p['route_id'],p['route_elapsed_s']+elapsed)
    return dict(sample,spawn_marker_id=event['target_id'],aircraft_placement_id=p['aircraft_placement_id'],
                camera_id=p['camera_id'],spawn_offset_m=list(p['spawn_offset_m']),
                jump_available=p['jump_open_s']<=elapsed<p['jump_close_s'],
                runtime_integrated=False,player_control_transferred=False)


def timeline_plan(document,world):
    validate_document(document,world)
    routes={r['route_id']:r for r in aircraft_routes(world.get('markers',[]))['routes']}
    follows=[]
    for binding in document['route_bindings']:
        route=routes[binding['route_id']];positions=route['positions_m']
        distances=[0.]
        for a,b in zip(positions,positions[1:]):distances.append(distances[-1]+math.dist(a,b))
        if distances[-1]<=1e-8:raise ValueError('Follow Path needs nonzero route length')
        samples=[]
        for time,distance in zip(route['times_s'],distances):
            time=time-route['times_s'][0]+binding['time_offset_s']
            samples.append({'frame':frame_at(document,time),'offset_factor':distance/distances[-1]})
        if len({s['frame'] for s in samples})!=len(samples):raise ValueError('Timed route keyframes collapse at selected FPS')
        follows.append(dict(binding,samples=samples))
    return {'frame_start':document['frame_start'],'frame_end':frame_at(document,document['duration_s']),
            'follows':follows,'cuts':[dict(cut,frame=frame_at(document,cut['time_s'])) for cut in document['cuts']],
            'events':[dict(event,frame=frame_at(document,event['time_s'])) for event in document['events']],
            'runtime_integrated':False,'clearance_verified':False}
