"""Native Blender camera/Timeline authoring over existing aircraft routes.

Register explicitly from the host add-on; importing this module never starts
playback or creates objects. Export is a separate strictly validated sidecar.
"""
import copy
import json
import math
from pathlib import Path
import uuid
try:from . import cinematic_core as core
except ImportError:import cinematic_core as core

_CLASSES=[]
_SETTINGS_HANDLERS=[]


def collect_world(scene):
    """Lightweight references for authoring; no native mesh or source reimport."""
    world={'maps':[],'markers':[],'asset_placements':[],'audio_zones':[],'action_elements':[]}
    for obj in scene.objects:
        kind=obj.get('wz09_type')
        if kind=='map_instance':world['maps'].append({'id':str(obj['wz09_id'])})
        elif kind in {'aircraft_keyframe','poi','player_spawn','loot_crate','buy_station','mission','loadout_drop'}:
            props=json.loads(obj.get('wz09_properties_json','{}'))
            if kind=='aircraft_keyframe':
                for field,key in (('route_id','wz09_route_id'),('sequence','wz09_route_sequence'),('time_s','wz09_route_time_s')):
                    if key in obj:props[field]=obj[key]
            world['markers'].append({'id':str(obj['wz09_id']),'kind':kind,
                'position_m':list(obj.matrix_world.translation),
                'rotation_deg':[math.degrees(x) for x in obj.matrix_world.to_euler('XYZ')],'properties':props})
        if obj.get('wz09_asset_placement'):world['asset_placements'].append({'id':str(obj['wz09_asset_placement_id'])})
        if obj.get('wz09_audio_zone'):world['audio_zones'].append(json.loads(obj['wz09_audio_json']))
        if obj.get('wz09_action_element'):world['action_elements'].append(json.loads(obj['wz09_action_json']))
    return world


def _stored(scene):
    raw=scene.get('wz09_cinematics_json')
    if not raw:raise ValueError('Create a cinematic sequence first')
    return json.loads(raw)


def load_settings(scene,document=None):
    """Copy accepted sequence metadata to RNA without scanning the world.

    Call after a save, registration or file load. Drawing never invokes this:
    typed values remain untouched until the user applies or explicitly loads.
    """
    settings=getattr(scene,'wz09_cinematic_settings',None)
    if settings is None:return False
    document=_stored(scene) if document is None else document
    if (not isinstance(document,dict) or document.get('kind')!='wz09_cinematic_timeline'
            or type(document.get('version')) is not int or document.get('version')!=1):
        raise ValueError('Unsupported cinematic settings document')
    name=core.text(document['name'],'sequence name')
    fps=core.number(document['fps'],'fps',1,240)
    duration=core.number(document['duration_s'],'duration_s',.001,86400)
    # Validate all fields before changing any property.
    settings.name=name;settings.fps=fps;settings.duration_s=duration
    if 'wz09_cinematic_settings_error' in scene:del scene['wz09_cinematic_settings_error']
    return True


def _sync_existing_settings(scenes):
    for scene in scenes:
        if not scene.get('wz09_cinematics_json'):continue
        try:load_settings(scene)
        except (ValueError,TypeError,KeyError) as exc:
            scene['wz09_cinematic_settings_error']=str(exc)[:300]


def _load_settings_post(*_args):
    import bpy
    _sync_existing_settings(bpy.data.scenes)


def _save(scene,document,dirty=True):
    encoded=json.dumps(document,sort_keys=True,allow_nan=False)
    load_settings(scene,document)
    scene['wz09_cinematics_json']=encoded
    scene['wz09_cinematic_dirty']=dirty


def create_sequence(ctx):
    if ctx.scene.get('wz09_cinematics_json'):raise ValueError('This scene already has a sequence; apply sequence settings or use a new scene')
    s=ctx.scene.wz09_cinematic_settings
    document=core.new_sequence(s.name,s.fps,s.duration_s,ctx.scene.frame_start)
    core.validate_document(document,collect_world(ctx.scene));_save(ctx.scene,document)
    return document


def create_camera(ctx):
    import bpy
    scene=ctx.scene;doc=_stored(scene)
    data=bpy.data.cameras.new('WZ09 Cinematic Camera')
    obj=bpy.data.objects.new('Cinematic - camera',data);scene.collection.objects.link(obj)
    obj['wz09_cinematic_camera_id']=str(uuid.uuid4());obj['wz09_cinematic_sequence_id']=doc['id']
    obj.location=scene.cursor.location;data.lens=35.;data.clip_end=20000.
    view=getattr(ctx,'region_data',None) or getattr(getattr(ctx,'space_data',None),'region_3d',None)
    if view is not None:obj.matrix_world=view.view_matrix.inverted()
    obj.show_name=True
    for old in ctx.selected_objects:old.select_set(False)
    obj.select_set(True);ctx.view_layer.objects.active=obj
    if scene.camera is None:scene.camera=obj
    ctx.view_layer.update();scene['wz09_cinematic_dirty']=True
    return obj


def _fcurves(owner):
    animation=getattr(owner,'animation_data',None);action=getattr(animation,'action',None)
    if action is None:return []
    result=[];seen=set()
    for curve in getattr(action,'fcurves',()):
        pointer=curve.as_pointer()
        if pointer not in seen:seen.add(pointer);result.append(curve)
    slot=getattr(animation,'action_slot',None);handle=getattr(slot,'handle',None)
    for layer in getattr(action,'layers',()):
        for strip in layer.strips:
            for bag in getattr(strip,'channelbags',()):
                if handle is not None and bag.slot_handle!=handle:continue
                for curve in bag.fcurves:
                    pointer=curve.as_pointer()
                    if pointer not in seen:seen.add(pointer);result.append(curve)
    return result


def camera_record(obj,bound=False):
    if obj.parent is not None:raise ValueError('Unparent cinematic cameras before export; route binding remains intact')
    keys=[]
    for owner in (obj,obj.data):
        for curve in _fcurves(owner):
            path=curve.data_path
            # The route's timed source markers are authoritative for this
            # generated channel; keep user camera transforms/lens as real keys.
            if bound and path.startswith('constraints["WZ09 Route ') and path.endswith('.offset_factor'):continue
            if path.endswith('.offset_factor'):path='offset_factor'
            if path not in {'location','rotation_euler','rotation_quaternion','lens','offset_factor'}:
                raise ValueError('Camera channel requires explicit support: '+path)
            for point in curve.keyframe_points:
                keys.append({'frame':float(point.co[0]),'data_path':path,'array_index':curve.array_index,
                    'value':float(point.co[1]),'interpolation':point.interpolation,
                    'handle_left':list(point.handle_left),'handle_right':list(point.handle_right)})
    matrix=obj.matrix_basis if bound else obj.matrix_world
    return {'id':str(obj['wz09_cinematic_camera_id']),'name':obj.name,
            'position_m':list(matrix.translation),'rotation_deg':[math.degrees(x) for x in matrix.to_euler('XYZ')],
            'transform_space':'route_offset' if bound else 'world','lens_mm':float(obj.data.lens),
            'clip_start_m':float(obj.data.clip_start),'clip_end_m':float(obj.data.clip_end),
            'keyframes':sorted(keys,key=lambda k:(k['frame'],k['data_path'],k['array_index']))}


def _capture(scene,world=None):
    document=_stored(scene);bound={b['target_id'] for b in document['route_bindings']}
    document['cameras']=[camera_record(obj,str(obj['wz09_cinematic_camera_id']) in bound) for obj in scene.objects
                         if obj.type=='CAMERA' and obj.get('wz09_cinematic_sequence_id')==document['id']]
    return document,world if world is not None else collect_world(scene)


def add_camera_cut(ctx,time_s=None):
    doc,world=_capture(ctx.scene)
    obj=ctx.active_object
    if obj is None or obj.type!='CAMERA' or obj.get('wz09_cinematic_sequence_id')!=doc['id']:
        raise ValueError('Select a camera from this sequence')
    time=max(0.,(ctx.scene.frame_current-doc['frame_start'])/doc['fps']) if time_s is None else time_s
    cut={'id':str(uuid.uuid4()),'time_s':time,'camera_id':str(obj['wz09_cinematic_camera_id'])}
    doc['cuts'].append(cut);core.validate_document(doc,world);_save(ctx.scene,doc)
    return cut


def bind_selected_to_route(ctx):
    doc,world=_capture(ctx.scene);obj=ctx.active_object;s=ctx.scene.wz09_cinematic_settings
    if obj is None:raise ValueError('Select a camera or placed aircraft instance')
    if obj.type=='CAMERA' and obj.get('wz09_cinematic_sequence_id')==doc['id']:
        target,kind=str(obj['wz09_cinematic_camera_id']),'camera'
    elif obj.get('wz09_asset_placement'):target,kind=str(obj['wz09_asset_placement_id']),'asset'
    else:raise ValueError('Bind only sequence cameras or authored asset placements to routes; original visuals are retained')
    if any(b['target_id']==target for b in doc['route_bindings']):raise ValueError('Selection already has a route binding')
    binding={'id':str(uuid.uuid4()),'route_id':s.route_id,'target_id':target,'target_kind':kind,
             'time_offset_s':s.route_offset_s,'follow_axis':'TRACK_NEGATIVE_Z' if kind=='camera' else 'FORWARD_X',
             'up_axis':'UP_Y' if kind=='camera' else 'UP_Z','orient':True}
    doc['route_bindings'].append(binding)
    for camera in doc['cameras']:
        if camera['id']==target:camera['transform_space']='route_offset'
    doc['source_routes_sha256']=core.route_fingerprint(world['markers'],[b['route_id'] for b in doc['route_bindings']])
    core.validate_document(doc,world);_save(ctx.scene,doc);return binding


def add_event(ctx):
    doc,world=_capture(ctx.scene);s=ctx.scene.wz09_cinematic_settings
    time=max(0.,(ctx.scene.frame_current-doc['frame_start'])/doc['fps'])
    target=s.event_target_id.strip() or None
    event=core.new_event(s.event_kind,time,target_id=target,reference=s.event_reference.strip())
    for key in event['parameters']:
        value=getattr(s,'event_'+key)
        event['parameters'][key]=list(value) if key in {'color','spawn_offset_m'} else value
    doc['events'].append(event);core.validate_document(doc,world);_save(ctx.scene,doc);return event


def target_from_selection(ctx):
    obj=ctx.active_object
    if obj is None:raise ValueError('Select an existing marker, audio emitter or authored placement')
    for key in ('wz09_audio_id','wz09_action_id','wz09_asset_placement_id','wz09_cinematic_camera_id','wz09_id'):
        if obj.get(key):ctx.scene.wz09_cinematic_settings.event_target_id=str(obj[key]);return str(obj[key])
    raise ValueError('Selection has no persistent editor ID')


def route_from_selection(ctx):
    obj=ctx.active_object
    if obj is None or obj.get('wz09_type')!='aircraft_keyframe' or not obj.get('wz09_route_id'):
        raise ValueError('Select an existing timed flight route marker')
    ctx.scene.wz09_cinematic_settings.route_id=str(obj['wz09_route_id'])


def unbind_selected(ctx):
    """Explicitly detach an owned preview; preserve its current world pose."""
    doc=_stored(ctx.scene);obj=ctx.active_object
    if obj is None:raise ValueError('Select a bound camera or authored placement')
    uid=obj.get('wz09_cinematic_camera_id') or obj.get('wz09_asset_placement_id')
    records=[b for b in doc['route_bindings'] if b['target_id']==uid]
    if len(records)!=1:raise ValueError('Selection has no unique cinematic route binding')
    name='WZ09 Route '+records[0]['id'];constraint=obj.constraints.get(name)
    if constraint is not None and constraint.type!='FOLLOW_PATH':raise ValueError('Bindung besitzt fremden Constrainttyp')
    action=getattr(getattr(obj,'animation_data',None),'action',None)
    if action is not None and action.get('wz09_cinematic_sequence_id')!=doc['id']:
        raise ValueError('Do not change an unrelated action; restore the original assignment')
    pose=obj.matrix_world.copy()
    if action is not None:
        obj.animation_data.action=action.copy()
        for curve in _fcurves(obj):
            if curve.data_path=='constraints["'+name+'"].offset_factor':curve.keyframe_points.clear()
    if constraint is not None:obj.constraints.remove(constraint)
    obj.matrix_world=pose
    doc['route_bindings']=[b for b in doc['route_bindings'] if b['id']!=records[0]['id']]
    doc['source_routes_sha256']=core.route_fingerprint(collect_world(ctx.scene)['markers'],[b['route_id'] for b in doc['route_bindings']])
    _save(ctx.scene,doc)


def remove_record(ctx,uid):
    """Remove only a declared cut/event; owned native markers rebuild on demand."""
    doc=_stored(ctx.scene)
    records=doc['cuts']+doc['events']
    if sum(r['id']==uid for r in records)!=1:raise ValueError('Cut/event ID missing or duplicated')
    for group in ('cuts','events'):doc[group]=[r for r in doc[group] if r['id']!=uid]
    _save(ctx.scene,doc)


def _target_lookup(scene):
    result={}
    for obj in scene.objects:
        for key in ('wz09_cinematic_camera_id','wz09_asset_placement_id'):
            if key=='wz09_asset_placement_id' and not obj.get('wz09_asset_placement'):continue
            if obj.get(key):
                uid=str(obj[key])
                if uid in result:raise ValueError('Duplicate cinematic target ID')
                result[uid]=obj
    return result


def build_timeline(ctx):
    import bpy
    from .aircraft_workflow import build_route_preview
    scene=ctx.scene;doc,world=_capture(scene)
    doc['source_routes_sha256']=core.route_fingerprint(world['markers'],[b['route_id'] for b in doc['route_bindings']])
    plan=core.timeline_plan(doc,world);targets=_target_lookup(scene)
    for binding in doc['route_bindings']:
        obj=targets[binding['target_id']];animation=obj.animation_data;action=getattr(animation,'action',None)
        if obj.library or getattr(obj,'override_library',None):raise ValueError('Linked source data cannot receive a cinematic route')
        if (action is not None and action.get('wz09_cinematic_sequence_id')!=doc['id']
                and obj.get('wz09_cinematic_sequence_id')!=doc['id']):
            raise ValueError('Target has a different action; use an authored preview copy')
        if animation is not None and (animation.drivers or animation.nla_tracks):
            raise ValueError('Routenziel besitzt NLA/Driver; eigene Vorschaukopie verwenden')
        names={'WZ09 Route '+b['id'] for b in doc['route_bindings'] if b['target_id']==binding['target_id']}
        if any(c.name not in names or c.type!='FOLLOW_PATH' for c in obj.constraints):
            raise ValueError('Target has other constraints; use an authored preview copy')
    old_markers=json.loads(scene.get('wz09_cinematic_timeline_markers_json','{}'))
    for name in old_markers.values():
        if scene.timeline_markers.get(name) is None:raise ValueError('A cinematic timeline marker was renamed/deleted; undo or restore its assignment first')
    curves={route:build_route_preview(ctx,route,markers=world['markers']) for route in sorted({b['route_id'] for b in doc['route_bindings']})}
    scene.render.fps=int(round(doc['fps']));scene.render.fps_base=scene.render.fps/doc['fps']
    scene.frame_start=plan['frame_start'];scene.frame_end=plan['frame_end']
    for follow in plan['follows']:
        obj=targets[follow['target_id']];name='WZ09 Route '+follow['id']
        constraint=obj.constraints.get(name)
        if constraint is None:constraint=obj.constraints.new('FOLLOW_PATH');constraint.name=name
        if constraint.type!='FOLLOW_PATH':raise ValueError('Saved cinematic binding is not Follow Path')
        constraint.target=curves[follow['route_id']];constraint.target.data.use_path=True
        constraint.use_fixed_location=True;constraint.use_curve_follow=follow['orient']
        constraint.forward_axis=follow['follow_axis'];constraint.up_axis=follow['up_axis']
        obj.animation_data_create()
        previous=obj.animation_data.action
        action=previous.copy() if previous is not None else bpy.data.actions.new('Cinematic route · '+follow['id'][:8])
        action['wz09_cinematic_sequence_id']=doc['id']
        obj.animation_data.action=action
        for curve in _fcurves(obj):
            if curve.data_path==constraint.path_from_id('offset_factor'):curve.keyframe_points.clear()
        if previous is None:obj.location=(0,0,0);obj.rotation_mode='XYZ';obj.rotation_euler=(0,0,0)
        for sample in follow['samples']:
            constraint.offset_factor=sample['offset_factor'];constraint.keyframe_insert(data_path='offset_factor',frame=sample['frame'])
        for curve in _fcurves(obj):
            if curve.data_path==constraint.path_from_id('offset_factor'):
                for point in curve.keyframe_points:point.interpolation='LINEAR'
    for name in old_markers.values():scene.timeline_markers.remove(scene.timeline_markers[name])
    markers={}
    for cut in plan['cuts']:
        marker=scene.timeline_markers.new('WZ09 cut '+cut['id'],frame=cut['frame'])
        marker.camera=targets[cut['camera_id']];markers[cut['id']]=marker.name
    for event in plan['events']:
        marker=scene.timeline_markers.new('WZ09 '+event['kind']+' '+event['id'],frame=event['frame'])
        markers[event['id']]=marker.name
    scene['wz09_cinematic_timeline_markers_json']=json.dumps(markers,sort_keys=True)
    _save(scene,doc,False);scene.frame_set(plan['frame_start']);ctx.view_layer.update()
    if scene.get('wz09_cinematic_presentation_json'):
        rebuild_presentation(ctx,document=doc,world=world)
    return plan


def rebuild_presentation(ctx,document=None,world=None):
    """Keep explicit clouds/settings when rebuilding the owned native preview."""
    from . import cinematic_presentation
    config=json.loads(ctx.scene.get('wz09_cinematic_presentation_json','{}'))
    return cinematic_presentation.build_presentation(ctx,document=document,world=world,
        cloud_bounds=config.get('cloud_bounds'),render_poi=config.get('render_poi',True),
        cloud_density=config.get('cloud_density',.015),cloud_noise_scale=config.get('cloud_noise_scale',.004),
        poi_camera_ids=config.get('poi_camera_ids'))


def sync_timeline(ctx):
    doc=_stored(ctx.scene);mapping=json.loads(ctx.scene.get('wz09_cinematic_timeline_markers_json','{}'))
    for record in doc['cuts']+doc['events']:
        marker=ctx.scene.timeline_markers.get(mapping.get(record['id'],''))
        if marker is None:raise ValueError('Timeline marker missing; undo or rebuild the timeline')
        record['time_s']=(marker.frame-doc['frame_start'])/doc['fps']
        if 'camera_id' in record:
            if marker.camera is None or not marker.camera.get('wz09_cinematic_camera_id'):raise ValueError('Camera cut requires a sequence camera')
            record['camera_id']=str(marker.camera['wz09_cinematic_camera_id'])
    doc['cameras']=_capture(ctx.scene)[0]['cameras'];world_data=collect_world(ctx.scene)
    core.validate_document(doc,world_data);_save(ctx.scene,doc,False);return doc


def serialize_scene(scene,world=None):
    if not scene.get('wz09_cinematics_json'):return None
    if scene.get('wz09_cinematic_dirty'):raise ValueError('Cinematic changed: rebuild the timeline before export')
    doc,world=_capture(scene,world);core.validate_document(doc,world)
    mapping=json.loads(scene.get('wz09_cinematic_timeline_markers_json','{}'))
    for record in doc['cuts']+doc['events']:
        marker=scene.timeline_markers.get(mapping.get(record['id'],''))
        if marker is None or marker.frame!=core.frame_at(doc,record['time_s']):
            raise ValueError('Timeline changed: apply timeline edits')
        if 'camera_id' in record and (marker.camera is None or marker.camera.get('wz09_cinematic_camera_id')!=record['camera_id']):
            raise ValueError('Camera cut changed: apply timeline edits')
    _check_route_preview(scene,doc,world)
    if scene.get('wz09_cinematic_presentation_json'):
        from . import cinematic_presentation
        cinematic_presentation.validate_scene(scene,doc)
    return doc


def _check_route_preview(scene,document,world):
    """Fail closed if generated native path/keys were edited apart from sources."""
    targets=_target_lookup(scene)
    routes={r['route_id']:r for r in core.aircraft_routes(world.get('markers',[]))['routes']}
    for follow in core.timeline_plan(document,world)['follows']:
        obj=targets[follow['target_id']];constraint=obj.constraints.get('WZ09 Route '+follow['id'])
        if (constraint is None or constraint.type!='FOLLOW_PATH' or constraint.target is None
                or constraint.target.get('wz09_route_id')!=follow['route_id'] or not constraint.use_fixed_location
                or constraint.use_curve_follow!=follow['orient'] or constraint.forward_axis!=follow['follow_axis']
                or constraint.up_axis!=follow['up_axis']):raise ValueError('Follow Path changed/missing: rebuild the timeline')
        path=constraint.target
        if len(path.data.splines)!=1 or path.data.splines[0].type!='POLY':raise ValueError('Route curve changed: rebuild from source markers')
        actual=[list((path.matrix_world @ point.co.to_3d())) for point in path.data.splines[0].points]
        expected=routes[follow['route_id']]['positions_m']
        if len(actual)!=len(expected) or any(math.dist(a,b)>1e-5 for a,b in zip(actual,expected)):
            raise ValueError('Route curve differs from source markers')
        curves=[c for c in _fcurves(obj) if c.data_path==constraint.path_from_id('offset_factor')]
        if len(curves)!=1 or len(curves[0].keyframe_points)!=len(follow['samples']):raise ValueError('Route keyframes missing/changed: rebuild the timeline')
        keys=sorted(curves[0].keyframe_points,key=lambda p:p.co[0])
        if any(abs(point.co[0]-sample['frame'])>1e-5 or abs(point.co[1]-sample['offset_factor'])>1e-5
               or point.interpolation!='LINEAR' for point,sample in zip(keys,follow['samples'])):
            raise ValueError('Route timing changed: edit source markers and rebuild the timeline')


def export_manifest(ctx,path=None):
    from .scene_io import atomic_write,safe_output_path
    root=Path(ctx.scene.wz09_project_root).resolve()
    if not (root/'AGENTS.md').is_file():raise ValueError('Set the standalone editor project path')
    path=safe_output_path(path or ctx.scene.wz09_cinematic_settings.export_path,root=root)
    doc=serialize_scene(ctx.scene)
    if doc is None:raise ValueError('No cinematic sequence exists')
    atomic_write(path,doc,root=root);return path


def register():
    import bpy
    from bpy.props import StringProperty,FloatProperty,FloatVectorProperty,EnumProperty,PointerProperty,BoolProperty
    class WZ09_CinematicSettings(bpy.types.PropertyGroup):
        name:StringProperty(name='Sequence name',default='Match Intro')
        fps:FloatProperty(name='Frames per second',default=24,min=1,max=240)
        duration_s:FloatProperty(name='Duration (s)',default=30,min=.001,max=86400)
        route_id:StringProperty(name='Existing flight route')
        route_offset_s:FloatProperty(name='Route start (s)',default=0,min=0)
        event_kind:EnumProperty(name='Event',items=[('audio','Sound',''),('fx','FX / Flares',''),('animation','Animation / Heckklappe',''),('player_handoff','Player handoff',''),('screen_fade','Bildblende',''),('aircraft_insertion','Flugzeug → Absprungentwurf','')])
        event_target_id:StringProperty(name='Target ID',description='Existing audio / asset / spawn marker; resolved by ID, not object name')
        event_reference:StringProperty(name='Alias / FX / animation profile')
        event_volume:FloatProperty(name='Volume',default=1,min=0,max=1)
        event_fade_in_s:FloatProperty(name='Fade in (s)',default=0,min=0,max=60)
        event_fade_out_s:FloatProperty(name='Fade out (s)',default=0,min=0,max=60)
        event_loop:BoolProperty(name='Repeat',default=False)
        event_scale:FloatProperty(name='FX size',default=1,min=.001,max=10000)
        event_speed:FloatProperty(name='Animation speed',default=1,min=.001,max=10000)
        event_camera_blend_s:FloatProperty(name='Camera transition (s)',default=.5,min=0,max=60)
        event_color:FloatVectorProperty(name='Fade color',size=3,subtype='COLOR',default=(0,0,0),min=0,max=1)
        event_from_alpha:FloatProperty(name='Starting opacity',default=0,min=0,max=1)
        event_to_alpha:FloatProperty(name='Target opacity',default=1,min=0,max=1)
        event_duration_s:FloatProperty(name='Fade duration (s)',default=1,min=.001,max=86400)
        event_aircraft_placement_id:StringProperty(name='Aircraft placement ID')
        event_route_id:StringProperty(name='Continuing route ID')
        event_route_elapsed_s:FloatProperty(name='Flight route time at handoff',default=0,min=0,max=86400)
        event_jump_open_s:FloatProperty(name='Jump start after handoff (s)',default=0,min=0,max=86400)
        event_jump_close_s:FloatProperty(name='Jump end after handoff (s)',default=1,min=0,max=86400)
        event_spawn_offset_m:FloatVectorProperty(name='Spawn offset in aircraft',size=3,subtype='TRANSLATION')
        event_camera_id:StringProperty(name='Follow camera ID')
        export_path:StringProperty(name='Timeline JSON',default='exports/cinematics.timeline.json',subtype='FILE_PATH')
    class WZ09_OT_cinematic(bpy.types.Operator):
        bl_idname='wz09.cinematic';bl_label='Edit cinematic';bl_options={'REGISTER','UNDO'}
        action:EnumProperty(items=[(v,v,'') for v in ('NEW','CAMERA','CUT','BIND','EVENT','BUILD','SYNC','EXPORT','APPLY','TARGET','REMOVE','ROUTE','UNBIND','PRESENTATION')])
        element_id:StringProperty()
        def execute(self,ctx):
            try:
                functions={'NEW':create_sequence,'CAMERA':create_camera,'CUT':add_camera_cut,
                           'BIND':bind_selected_to_route,'EVENT':add_event,'BUILD':build_timeline,
                           'SYNC':sync_timeline,'EXPORT':export_manifest,'TARGET':target_from_selection,
                           'ROUTE':route_from_selection,'UNBIND':unbind_selected,'PRESENTATION':rebuild_presentation}
                if self.action=='APPLY':
                    doc=_stored(ctx.scene);s=ctx.scene.wz09_cinematic_settings
                    doc.update(name=s.name,fps=s.fps,duration_s=s.duration_s)
                    core.validate_document(doc,collect_world(ctx.scene));_save(ctx.scene,doc)
                elif self.action=='REMOVE':remove_record(ctx,self.element_id)
                else:functions[self.action](ctx)
                return {'FINISHED'}
            except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    class WZ09_PT_cinematic(bpy.types.Panel):
        bl_idname='WZ09_PT_cinematic';bl_label='Cinematics & Match Intro';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
        @classmethod
        def poll(cls,ctx):return getattr(ctx.scene,'wz09_workspace','') in {'CINEMATICS','STUDIO'}
        def draw(self,ctx):
            a=self.layout;s=ctx.scene.wz09_cinematic_settings
            for prop in ('name','fps','duration_s'):a.prop(s,prop)
            a.operator('wz09.cinematic',text='Create sequence',icon='ADD').action='NEW'
            if not ctx.scene.get('wz09_cinematics_json'):return
            a.operator('wz09.cinematic',text='Apply sequence settings',icon='CHECKMARK').action='APPLY'
            a.label(text='Timeline cursor determines cut and event timing',icon='TIME')
            a.operator('wz09.cinematic',text='Camera from view / at cursor',icon='CAMERA_DATA').action='CAMERA'
            if ctx.active_object and ctx.active_object.type=='CAMERA':
                a.prop(ctx.active_object.data,'lens');a.operator('wz09.cinematic',text='Camera cut here',icon='MARKER_HLT').action='CUT'
            a.prop(s,'route_id');a.prop(s,'route_offset_s')
            a.operator('wz09.cinematic',text='Route from selected marker',icon='EYEDROPPER').action='ROUTE'
            active=ctx.active_object
            if active and active.get('wz09_type')=='aircraft_keyframe':
                for prop,label in (('wz09_route_sequence','Reihenfolge'),('wz09_route_time_s','Zeit (s)')):
                    if prop in active:a.prop(active,'["'+prop+'"]',text=label)
                a.prop(active,'location',text='Route position / height')
            a.operator('wz09.cinematic',text='Bind selection to existing route',icon='CON_FOLLOWPATH').action='BIND'
            a.operator('wz09.cinematic',text='Unbind selection from route',icon='UNLINKED').action='UNBIND'
            box=a.box();box.prop(s,'event_kind');box.prop(s,'event_target_id');box.prop(s,'event_reference')
            box.operator('wz09.cinematic',text='Target from selection',icon='EYEDROPPER').action='TARGET'
            for key in core.PARAMETERS[s.event_kind]:box.prop(s,'event_'+key)
            box.operator('wz09.cinematic',text='Event am Timeline-Cursor',icon='MARKER').action='EVENT'
            doc=_stored(ctx.scene)
            for item in sorted(doc['cuts']+doc['events'],key=lambda r:r['time_s']):
                row=a.row(align=True);row.label(text=f"{item['time_s']:.2f} s · {item.get('kind','Kameraschnitt')}")
                op=row.operator('wz09.cinematic',text='',icon='X');op.action='REMOVE';op.element_id=item['id']
            a.operator('wz09.cinematic',text='Build Blender timeline',icon='ACTION').action='BUILD'
            a.operator('wz09.cinematic',text='Rebuild fades and aerial labels',icon='RENDER_STILL').action='PRESENTATION'
            a.operator('wz09.cinematic',text='Apply timeline edits',icon='FILE_REFRESH').action='SYNC'
            a.operator('screen.animation_play',text='Start / stop preview',icon='PLAY')
            a.prop(s,'export_path');a.operator('wz09.cinematic',text='Export timeline separately',icon='EXPORT').action='EXPORT'
            a.label(text='FX / sound / player handoff: declarative events',icon='INFO')
            a.label(text='Runtime and flight clearance unverified',icon='INFO')
    _CLASSES[:]=[WZ09_CinematicSettings,WZ09_OT_cinematic,WZ09_PT_cinematic]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_cinematic_settings=PointerProperty(type=WZ09_CinematicSettings)
    handler=bpy.app.handlers.persistent(_load_settings_post)
    if handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(handler);_SETTINGS_HANDLERS.append(handler)
    _sync_existing_settings(bpy.data.scenes)


def unregister():
    import bpy
    for handler in _SETTINGS_HANDLERS:
        if handler in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(handler)
    _SETTINGS_HANDLERS.clear()
    if hasattr(bpy.types.Scene,'wz09_cinematic_settings'):del bpy.types.Scene.wz09_cinematic_settings
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
