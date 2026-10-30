"""Small authoring helpers; route metadata is editor data, not flight simulation."""
import math


def resolve_owner_map(obj):
    seen=set()
    while obj is not None:
        key=obj.as_pointer() if hasattr(obj,'as_pointer') else id(obj)
        if key in seen:raise ValueError('Map parent hierarchy contains a cycle')
        seen.add(key)
        if obj.get('wz09_type')=='map_instance':return obj
        obj=getattr(obj,'parent',None)
    return None


def aircraft_routes(markers):
    """Check persistent route IDs, sequence and explicit relative times in seconds.

    Legacy generic aircraft markers are retained but reported as untimed. No
    velocity, terrain clearance, camera or gameplay behavior is inferred.
    """
    groups={};untimed=[]
    for marker in markers:
        if marker.get('kind')!='aircraft_keyframe':continue
        props=marker.get('properties',{})
        fields=('route_id','sequence','time_s')
        if not any(name in props for name in fields):
            untimed.append(marker['id']);continue
        if not all(name in props for name in fields):
            raise ValueError(f'Aircraft keyframe {marker["id"]}: route_id/sequence/time_s required together')
        route=props['route_id'];sequence=props['sequence'];time=props['time_s']
        if not isinstance(route,str) or not route.strip():raise ValueError('Aircraft route ID must be nonempty')
        if type(sequence) is not int or sequence<0:raise ValueError('Aircraft sequence must be a nonnegative integer')
        if type(time) not in (int,float) or not math.isfinite(time) or time<0:
            raise ValueError('Aircraft keyframe time must be finite, nonnegative seconds')
        groups.setdefault(route,[]).append(marker)
    result=[]
    for route,frames in sorted(groups.items()):
        frames=sorted(frames,key=lambda item:item['properties']['sequence'])
        orders=[item['properties']['sequence'] for item in frames]
        times=[item['properties']['time_s'] for item in frames]
        if len(set(orders))!=len(orders):raise ValueError(f'Aircraft route {route}: duplicate sequence')
        if any(a>=b for a,b in zip(times,times[1:])):
            raise ValueError(f'Aircraft route {route}: times must increase with sequence')
        result.append({'route_id':route,'keyframe_ids':[item['id'] for item in frames],
            'positions_m':[item['position_m'] for item in frames],'times_s':times,
            'duration_s':times[-1]-times[0],'complete_polyline':len(frames)>=2})
    return {'routes':result,'untimed_keyframes':untimed,'runtime_verified':False}


def next_aircraft_keyframe(objects,route_id,interval_s):
    if not isinstance(route_id,str) or not route_id.strip():raise ValueError('Set an aircraft route ID')
    if type(interval_s) not in (int,float) or not math.isfinite(interval_s) or interval_s<=0:
        raise ValueError('Aircraft interval must be positive seconds')
    frames=[obj for obj in objects if obj.get('wz09_type')=='aircraft_keyframe'
            and obj.get('wz09_route_id')==route_id]
    if not frames:return {'route_id':route_id,'sequence':0,'time_s':0.0}
    sequence=max(int(obj.get('wz09_route_sequence',0)) for obj in frames)+1
    time=max(float(obj.get('wz09_route_time_s',0)) for obj in frames)+interval_s
    return {'route_id':route_id,'sequence':sequence,'time_s':time}
