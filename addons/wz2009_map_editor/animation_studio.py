"""Animation authoring on Blender Actions, pose bones, NLA and native importers.

No decoder, playback engine or game execution lives here. Imported source Actions
stay separate; every authoring operation targets an explicitly editable copy.
"""
import copy
import datetime
import json
import math
import os
from pathlib import Path
import uuid

try:from . import animation_core as core
except ImportError:import animation_core as core

_CLASSES=[]
def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def fps(scene):return scene.render.fps/scene.render.fps_base
def _root(scene):
    import bpy
    value=str(getattr(scene,'wz09_project_root','')).strip()
    root=Path(bpy.path.abspath(value)).resolve() if value else Path(__file__).resolve().parents[2]
    if not (root/'AGENTS.md').is_file():raise ValueError('Select the standalone editor project folder')
    return root
def _active_rig(context):
    settings=getattr(context.scene,'wz09_animation_settings',None)
    if settings is not None and settings.rig is not None and settings.rig.name in context.scene.objects:return settings.rig
    obj=context.active_object
    while obj is not None:
        if obj.type=='ARMATURE':return obj
        for modifier in obj.modifiers:
            if modifier.type=='ARMATURE' and modifier.object is not None:return modifier.object
        obj=obj.parent
    raise ValueError('Select an armature rig or its mesh')
def _curves(action):
    layers=getattr(action,'layers',None)
    if layers is not None:
        for layer in layers:
            for strip in layer.strips:
                for bag in getattr(strip,'channelbags',[]):
                    yield from bag.fcurves
    else:yield from getattr(action,'fcurves',[])
def action_fingerprint(action):
    curves=[]
    for curve in _curves(action):
        modifiers=[]
        for modifier in curve.modifiers:
            properties={'type':modifier.type}
            for prop in modifier.bl_rna.properties:
                key=prop.identifier
                if prop.is_readonly or key=='rna_type' or prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:continue
                value=getattr(modifier,key)
                properties[key]=list(value) if prop.is_array else value
            if hasattr(modifier,'control_points'):
                properties['control_points']=[{'frame':float(point.frame),'min':float(point.min),'max':float(point.max)} for point in modifier.control_points]
            modifiers.append(properties)
        curves.append({'path':curve.data_path,'index':curve.array_index,'extrapolation':curve.extrapolation,
            'muted':bool(curve.mute),'modifiers':modifiers,
            'keys':[[float(p.co.x),float(p.co.y),p.interpolation,list(p.handle_left),list(p.handle_right),p.handle_left_type,p.handle_right_type] for p in curve.keyframe_points]})
    return core.fingerprint(sorted(curves,key=lambda v:(v['path'],v['index'])))
def _assign(rig,action,slot_identifier=None):
    data=rig.animation_data_create();data.action=action
    if hasattr(action,'slots'):
        slots=list(data.action_suitable_slots)
        if slot_identifier:slots=[slot for slot in slots if slot.identifier==slot_identifier]
        if len(slots)==1:data.action_slot=slots[0]
        elif len(slots)>1 and data.action_slot not in slots:raise ValueError('Multiple action slots: assign a unique rig slot in the Blender Action Editor')
        elif not slots and len(action.slots)>0:raise ValueError('No compatible object action slot for this rig')
    data.use_nla=False
def _record(action):
    if not action or 'wz09_animation_json' not in action:raise ValueError('Register the clip in Animation Studio first')
    return json.loads(action['wz09_animation_json'])
def _save_record(action,record):
    record['channels_fingerprint']=action_fingerprint(action)
    if record.get('verification',{}).get('clip_fingerprint')!=record['channels_fingerprint']:
        record['blender_verified']=False;record.pop('verification',None)
    action['wz09_animation_json']=encoded(record);action.use_fake_user=True
def _current_action(rig):
    action=rig.animation_data.action if rig.animation_data else None
    if action is None:raise ValueError('Activate a clip from the library')
    return action


def register_rig(rig,source=None,fresh=False):
    if rig.type!='ARMATURE' or not rig.data.bones:raise ValueError('Only an existing armature skeleton can be registered')
    if fresh and rig.data.users>1:rig.data=rig.data.copy()
    if fresh or 'wz09_rig_id' not in rig:rig['wz09_rig_id']=core.identifier()
    uid=rig['wz09_rig_id']
    for bone in rig.data.bones:
        if fresh or 'wz09_bone_id' not in bone:bone['wz09_bone_id']=core.stable_id(uid,bone.name)
    record=_existing_rig_record(rig)
    if source is not None:record['source']=source
    rig['wz09_rig_json']=encoded(record);return record


def _existing_rig_record(rig):
    """Snapshot existing identities/bind data; never allocate or write during IO."""
    if rig.type!='ARMATURE' or not rig.get('wz09_rig_id'):raise ValueError('Register the rig explicitly first')
    bones=[]
    for bone in rig.data.bones:
        if not bone.get('wz09_bone_id'):raise ValueError('Register the rig to assign stable IDs to new bones first')
        if bone.parent and not bone.parent.get('wz09_bone_id'):raise ValueError('Parent bone has no stable ID yet')
        bones.append({'id':str(bone['wz09_bone_id']),'name':bone.name,'parent_id':str(bone.parent['wz09_bone_id']) if bone.parent else None,
            'rest_matrix_local':[float(v) for row in bone.matrix_local for v in row]})
    previous=json.loads(rig.get('wz09_rig_json','{}'))
    record={'id':str(rig['wz09_rig_id']),'name':rig.name,'bones':bones,'source':previous.get('source'),
        'rest_fingerprint':core.rig_rest_fingerprint(bones),'runtime_integrated':False}
    core.validate_skeleton(bones);return record


def register_action(rig,action,scene,role='edit',source=None,events=None):
    rig_record=register_rig(rig)
    if 'wz09_animation_json' in action:return _record(action)
    start,end=map(float,action.frame_range)
    record=core.new_clip(rig_record['id'],action.name,start,end,fps(scene),role=role,source=source,events=events or [])
    if rig.animation_data and rig.animation_data.action==action and hasattr(rig.animation_data,'action_slot'):
        slot=rig.animation_data.action_slot
        if slot:record['slot_identifier']=slot.identifier
    _save_record(action,record)
    if role=='source':action['wz09_source_fingerprint']=record['channels_fingerprint']
    return record


def working_copy(rig,scene):
    source=_current_action(rig);record=register_action(rig,source,scene)
    if record['rig_id']!=rig['wz09_rig_id']:
        import bpy
        owners=[obj for obj in bpy.data.objects if obj.type=='ARMATURE' and obj.get('wz09_rig_id')==record['rig_id']]
        if len(owners)!=1 or register_rig(owners[0])['rest_fingerprint']!=register_rig(rig)['rest_fingerprint']:
            raise ValueError('Different rig / bind pose: use an explicit bone map and retarget')
    if record['role']=='source' and action_fingerprint(source)!=source.get('wz09_source_fingerprint'):
        raise ValueError('Source action changed externally; import the original into a new rig version')
    action=source.copy();action.name=record['name']+' · Edit';result=core.copied_clip(record,action.name,rig_id=rig['wz09_rig_id'])
    _assign(rig,action,record.get('slot_identifier'));_save_record(action,result);_event_markers(action,result)
    return action


def _editable(rig,scene):
    action=_current_action(rig);record=register_action(rig,action,scene)
    return working_copy(rig,scene) if record['role']=='source' or record['rig_id']!=rig.get('wz09_rig_id') else action


def _event_markers(action,record):
    # Marker labels are derived views; stable Event IDs live in the JSON.
    for marker in list(action.pose_markers):
        if marker.name.startswith('WZ09 · '):action.pose_markers.remove(marker)
    for event in record['events']:
        marker=action.pose_markers.new('WZ09 · '+event['id'][:8]+' · '+event['name'])
        marker.frame=round(event['frame'])


def import_capture(context,path):
    import bpy
    report,capture=core.read_capture(path);before={obj.as_pointer() for obj in bpy.data.objects};before_actions={a.as_pointer() for a in bpy.data.actions}
    old_selection=list(context.selected_objects);old_active=context.active_object
    old_fps,old_fps_base=context.scene.render.fps,context.scene.render.fps_base
    try:
        # glTF converts seconds to frame keys using the destination scene FPS.
        # Choose the captured rate before importing, rather than changing the
        # playback speed of already imported native keys afterward.
        native_fps=capture['clip']['fps'];context.scene.render.fps=max(1,round(native_fps))
        context.scene.render.fps_base=context.scene.render.fps/native_fps
        bpy.ops.import_scene.gltf(filepath=report['preview_bundle']['reference_path'],import_pack_images=True)
        added=[obj for obj in bpy.data.objects if obj.as_pointer() not in before]
        rigs=[obj for obj in added if obj.type=='ARMATURE']
        if len(rigs)!=1:raise ValueError('Native rig export must provide exactly one armature')
        rig=rigs[0];native_names={b['name'] for b in capture['bones']}
        if not native_names.issubset({b.name for b in rig.data.bones}):raise ValueError('Importiertes Rig verliert originale Bone-Namen')
        rig['wz09_native_capture_path']=str(Path(path).resolve());rig['wz09_native_capture_sha256']=core.file_sha256(path)
        rig['wz09_native_initial_world']=[float(v) for row in rig.matrix_world for v in row]
        reference={'kind':'native_capture','path':str(Path(path).resolve()),'sha256':core.file_sha256(path)}
        register_rig(rig,reference)
        action=rig.animation_data.action if rig.animation_data else None
        if action is None and rig.animation_data:
            candidates=[strip.action for track in rig.animation_data.nla_tracks for strip in track.strips if strip.action]
            if len({a.as_pointer() for a in candidates})==1:action=candidates[0]
        if action is None:raise ValueError('The glTF importer did not create a real rig action')
        _assign(rig,action)
        start,end=map(float,action.frame_range);events=[]
        for event in capture['clip'].get('events',[]):
            frame=start+event['time_s']*fps(context.scene)
            # Native f32 seconds and imported f32 frame keys round differently.
            # Clamp boundary roundoff only; retain exact source time in payload.
            if start-1e-4<=frame<=end+1e-4:frame=min(end,max(start,frame))
            events.append({'id':core.identifier(),'name':event['name'],'type':'notify',
                'frame':frame,'payload':{'native_normalized_time':event['normalized_time'],'native_time_s':event['time_s']}})
        record=register_action(rig,action,context.scene,'source',reference,events)
        record.update(looping=capture['clip']['looping'],source_fps=capture['clip']['fps'],native_clip=capture['clip']['name'],
            unmapped_native_tracks=capture['clip'].get('unmapped_tracks',[]),unmapped_animated_tracks=capture['clip'].get('unmapped_animated_tracks',[]))
        _save_record(action,record);action['wz09_source_fingerprint']=action_fingerprint(action);_event_markers(action,record)
        for obj in added:obj['wz09_animation_part']=True;obj['wz09_preview_only']=True;obj['wz09_rig_owner_id']=rig['wz09_rig_id']
        for obj in context.selected_objects:obj.select_set(False)
        rig.select_set(True);context.view_layer.objects.active=rig
        working=working_copy(rig,context.scene)
        edited=_record(working);scene=context.scene
        scene.frame_start=math.floor(edited['frame_start']);scene.frame_end=math.ceil(edited['frame_end'])
        scene.frame_preview_start=scene.frame_start;scene.frame_preview_end=scene.frame_end;scene.use_preview_range=False
        scene.frame_set(scene.frame_start)
        if hasattr(scene,'wz09_animation_settings'):
            settings=scene.wz09_animation_settings;settings.rig=rig;settings.search=''
            settings.clip=edited['id'];settings.bake_start=scene.frame_start;settings.bake_end=scene.frame_end
        for pose_bone in rig.pose.bones:
            selectable=pose_bone if hasattr(pose_bone,'select') else pose_bone.bone
            selectable.select=False
        selected=next((bone for bone in rig.data.bones if bone.parent is None),rig.data.bones[0])
        pose_bone=rig.pose.bones[selected.name]
        selectable=pose_bone if hasattr(pose_bone,'select') else selected
        selectable.select=True;rig.data.bones.active=selected
        return {'rig_id':rig['wz09_rig_id'],'source_clip_id':record['id'],'edit_clip_id':_record(working)['id'],'bones':len(native_names)}
    except Exception:
        context.scene.render.fps=old_fps;context.scene.render.fps_base=old_fps_base
        for obj in list(bpy.data.objects):
            if obj.as_pointer() not in before:bpy.data.objects.remove(obj,do_unlink=True)
        for action in list(bpy.data.actions):
            if action.as_pointer() not in before_actions:bpy.data.actions.remove(action)
        for obj in old_selection:
            if obj.name in context.view_layer.objects:obj.select_set(True)
        if old_active and old_active.name in context.view_layer.objects:context.view_layer.objects.active=old_active
        raise


def _capture_view_framing(points,lens=50.,aspect=1.):
    """Finite sphere-fit plan for actual imported geometry, not world bounds."""
    points=[tuple(core.number(v,'Capture viewport coordinate') for v in point) for point in points]
    if not points or any(len(point)!=3 for point in points):raise ValueError('Capture viewport requires actual three-dimensional bounds')
    lens=core.number(lens,'Viewport lens',1e-6);aspect=core.number(aspect,'Viewport aspect',1e-6)
    center=[(min(p[i] for p in points)+max(p[i] for p in points))*.5 for i in range(3)]
    radius=max(.05,max(math.sqrt(sum((p[i]-center[i])**2 for i in range(3))) for p in points))
    distance=radius*1.2*max(1.,aspect)/math.sin(math.atan(16./lens))
    if not math.isfinite(distance):raise ValueError('Capture viewport extent is outside the finite range')
    return {'center':center,'radius':radius,'distance':distance,'clip_start':max(.001,radius*.001),'clip_end':max(1000.,distance+radius*4)}


def _fit_imported_capture(context,rig):
    """Optional GUI-only view adjustment; importing data needs no 3D context."""
    import bpy
    if bpy.app.background or context.screen is None:return False
    from mathutils import Vector
    area=context.area if context.area and context.area.type=='VIEW_3D' else next((area for area in context.screen.areas if area.type=='VIEW_3D'),None)
    if area is None:return False
    space=area.spaces.active;view=space.region_3d
    if view is None:return False
    context.view_layer.update();depsgraph=context.evaluated_depsgraph_get()
    meshes=[obj for obj in context.scene.objects if obj.type=='MESH' and any(mod.type=='ARMATURE' and mod.object==rig for mod in obj.modifiers)]
    points=[]
    for obj in meshes:
        evaluated=obj.evaluated_get(depsgraph)
        if all(tuple(corner)==(-1.,-1.,-1.) for corner in evaluated.bound_box):continue
        points.extend(tuple(evaluated.matrix_world@Vector(corner)) for corner in evaluated.bound_box)
    if not points:
        evaluated=rig.evaluated_get(depsgraph)
        points=[tuple(evaluated.matrix_world@point) for bone in evaluated.pose.bones for point in (bone.head,bone.tail)]
    region=next((region for region in area.regions if region.type=='WINDOW'),None)
    plan=_capture_view_framing(points,space.lens,region.width/max(1,region.height) if region else 1.)
    if space.local_view is not None:
        for obj in [rig,*meshes]:obj.local_view_set(space,True)
    view.view_perspective='PERSP';view.view_location=plan['center'];view.view_distance=plan['distance']
    view.view_rotation=Vector((1.,-1.,.45)).to_track_quat('Z','Y')
    space.clip_start=plan['clip_start'];space.clip_end=plan['clip_end'];area.tag_redraw()
    return True


def serialize_scene(scene):
    import bpy
    rigs=[_existing_rig_record(obj) for obj in scene.objects if obj.type=='ARMATURE' and 'wz09_rig_id' in obj]
    rig_ids={rig['id'] for rig in rigs};clips=[]
    for action in bpy.data.actions:
        if 'wz09_animation_json' not in action:continue
        record=_record(action)
        if record['rig_id'] not in rig_ids:continue
        current=action_fingerprint(action)
        if record['role']=='source' and current!=action.get('wz09_source_fingerprint'):raise ValueError('Original source action has changed: '+record['name'])
        if record['channels_fingerprint']!=current:
            record['channels_fingerprint']=current;record['blender_verified']=False;record.pop('verification',None)
        record['name']=action.name;clips.append(record)
    nla=[]
    for obj in scene.objects:
        if obj.type!='ARMATURE' or obj.get('wz09_rig_id') not in rig_ids or obj.animation_data is None:continue
        tracks=[]
        for track in obj.animation_data.nla_tracks:
            strips=[]
            for strip in track.strips:
                if strip.action is None:raise ValueError('NLA strip without an action is not a supported animation clip')
                clip_record=_record(strip.action)
                controlled=_controlled_nla_influence(strip)
                strips.append({'clip_id':clip_record['id'],'name':strip.name,'frame_start':float(strip.frame_start),'frame_end':float(strip.frame_end),
                    'action_frame_start':float(strip.action_frame_start),'action_frame_end':float(strip.action_frame_end),'repeat':float(strip.repeat),
                    'scale':float(strip.scale),'influence':float(strip.influence if controlled is None else controlled),'blend_type':strip.blend_type,'extrapolation':strip.extrapolation,'muted':bool(strip.mute)})
                strips[-1].update(reverse=bool(strip.use_reverse),animated_influence=bool(strip.use_animated_influence),
                    controlled_influence=controlled is not None,animated_time=bool(strip.use_animated_time))
            tracks.append({'name':track.name,'muted':bool(track.mute),'solo':bool(track.is_solo),'strips':strips})
        nla.append({'rig_id':obj['wz09_rig_id'],'enabled':bool(obj.animation_data.use_nla),'tracks':tracks})
    result={'version':1,'rigs':rigs,'clips':clips,'nla':nla,'bone_maps':json.loads(scene.get('wz09_bone_maps_json','[]')),
        'attachments':json.loads(scene.get('wz09_animation_attachments_json','[]'))}
    if 'wz09_animation_exports_json' in scene:result['exports']=_current_exports(scene,clips,rig_ids)
    core.validate_library(result);return result


def _current_exports(scene,clips,rig_ids):
    """Exclude stale channel versions without changing immutable scene history."""
    records=core.array(json.loads(scene.get('wz09_animation_exports_json','[]')),'Animation export history')
    current={clip['id']:clip for clip in clips};result=[]
    for record in records:
        core.object_fields(record,('id','rig_id','clip_id','channels_fingerprint','source','object_ids'),label='Animation export history binding')
        clip=current.get(record['clip_id'])
        if record['rig_id'] in rig_ids and clip is not None and clip['rig_id']==record['rig_id'] and clip['channels_fingerprint']==record['channels_fingerprint']:
            core.source_reference(record['source'],verify=False)
            source=Path(record['source']['path'])
            if source.is_file():
                manifest=json.loads(source.read_text(encoding='utf-8'));snapshots=manifest.get('animation_studio',{}).get('clips',[])
                matches=[item for item in snapshots if item.get('id')==clip['id']]
                if len(matches)==1 and core.clip_export_fingerprint(matches[0])!=core.clip_export_fingerprint(clip):continue
            result.append(record)
    return result


def add_event(context):
    settings=context.scene.wz09_animation_settings;rig=_active_rig(context);action=_editable(rig,context.scene);record=_record(action)
    try:payload=json.loads(settings.event_payload)
    except json.JSONDecodeError as exc:raise ValueError('Event payload is not valid JSON') from exc
    event={'id':core.identifier(),'name':settings.event_name,'frame':float(context.scene.frame_current),'type':settings.event_type,'payload':payload}
    core.validate_event(event,record['frame_start'],record['frame_end']);record['events'].append(event)
    _save_record(action,record);_event_markers(action,record);return event


def _controlled_nla_influence(strip):
    """Return a proven constant control value; reject varying unbaked controls."""
    if not strip.use_animated_influence:return None
    curves=[curve for curve in strip.fcurves if curve.data_path=='influence']
    if len(curves)!=1:raise ValueError('NLA influence has no unique constant control curve; bake it visually')
    curve=curves[0]
    if (curve.mute or curve.modifiers or curve.driver is not None or curve.sampled_points
            or curve.extrapolation!='CONSTANT' or len(curve.keyframe_points)!=1):
        raise ValueError('Visually bake time-dependent NLA control curves first')
    value=core.number(float(curve.keyframe_points[0].co[1]),'NLA controlled influence',0.)
    if value>1:raise ValueError('NLA influence outside 0..1')
    return value


def set_nla_influence(strip,value):
    """Author an actual constant Blender influence curve, not a transient value."""
    value=core.number(value,'NLA influence',0.)
    if value>1:raise ValueError('NLA influence outside 0..1')
    if strip.use_animated_influence:_controlled_nla_influence(strip)
    elif any(curve.data_path=='influence' for curve in strip.fcurves):
        raise ValueError('Edit the existing disabled influence curve explicitly in the NLA editor')
    # Blender otherwise recomputes an automatic 1.0 influence on each update.
    # Enabling this flag creates its native control curve and first key itself.
    strip.influence=value;strip.use_animated_influence=True
    curve=next(curve for curve in strip.fcurves if curve.data_path=='influence')
    curve.extrapolation='CONSTANT';curve.keyframe_points[0].co[1]=value;curve.update()
    strip.influence=value
    _controlled_nla_influence(strip)


def add_nla(context):
    rig=_active_rig(context);action=_editable(rig,context.scene);settings=context.scene.wz09_animation_settings
    data=rig.animation_data_create();slot=getattr(data,'action_slot',None)
    track=data.nla_tracks.new();track.name='WZ09 · '+action.name
    strip=track.strips.new(action.name,context.scene.frame_current,action)
    if slot is not None and hasattr(strip,'action_slot'):strip.action_slot=slot
    strip.blend_type=settings.blend_mode;strip.repeat=settings.repeat;set_nla_influence(strip,settings.influence)
    data.action=None;data.use_nla=True;return strip


def bake_action(context,retarget=False):
    import bpy
    from mathutils import Matrix
    settings=context.scene.wz09_animation_settings;source=_active_rig(context);target=settings.target_rig if retarget else source
    if target is None or target.type!='ARMATURE':raise ValueError('Select a real target rig')
    source_record=register_rig(source);target_record=register_rig(target)
    action=_current_action(source) if source.animation_data and source.animation_data.action else None
    source_clip=register_action(source,action,context.scene) if action else None
    start,end=settings.bake_start,settings.bake_end
    if end<start or end-start>10000:raise ValueError('Bake range must be ordered and no longer than 10000 frames')
    mapping=None
    if retarget:
        maps=json.loads(context.scene.get('wz09_bone_maps_json','[]'))
        matches=[m for m in maps if m['source_rig_id']==source_record['id'] and m['target_rig_id']==target_record['id']]
        if len(matches)!=1:raise ValueError('Import exactly one explicitly verified bone map for these two rigs')
        mapping=matches[0];core.validate_bone_map(mapping,{source_record['id']:source_record,target_record['id']:target_record})
    old_frame=context.scene.frame_current;old_subframe=context.scene.frame_subframe
    old_target_action=target.animation_data.action if target.animation_data else None
    old_nla=target.animation_data.use_nla if target.animation_data else True
    old_slot=getattr(target.animation_data,'action_slot',None) if target.animation_data else None
    constraints=[(None,constraint) for constraint in target.constraints]+[(bone,constraint) for bone in target.pose.bones for constraint in bone.constraints]
    live_constraints=[(bone,constraint) for bone,constraint in constraints if not constraint.mute]
    if live_constraints and not settings.mute_baked_constraints:raise ValueError('Explicitly mute active target constraints after visual baking')
    original_target_world=target.matrix_world.copy()
    source_by_id={b['id']:b['name'] for b in source_record['bones']};target_by_id={b['id']:b['name'] for b in target_record['bones']}
    samples=[];object_samples=[]
    try:
        # Capture everything before assigning the new Action. The source Action,
        # constraints and NLA remain untouched during this evaluation pass.
        for frame in range(start,end+1):
            context.scene.frame_set(frame);context.view_layer.update()
            if mapping:
                values={}
                for entry in mapping['entries']:
                    left=source.pose.bones[source_by_id[entry['source_bone_id']]];right=target.pose.bones[target_by_id[entry['target_bone_id']]]
                    # Blender's own anim_utils visual bake captures LOCAL here,
                    # while every evaluated parent is still on the source frame.
                    # This also respects inherit-scale/local-location bone flags.
                    delta=source.convert_space(pose_bone=left,matrix=left.matrix,from_space='POSE',to_space='LOCAL')
                    delta.translation*=mapping['translation_scale']
                    values[right.name]=delta
                samples.append(values)
                object_samples.append(source.matrix_world.copy())
            else:
                samples.append({bone.name:source.convert_space(pose_bone=bone,matrix=bone.matrix,from_space='POSE',to_space='LOCAL') for bone in source.pose.bones})
                object_samples.append(source.matrix_world.copy())
        constraint_snapshot=[]
        for bone,constraint in live_constraints:
            constraint_snapshot.append({'bone_id':str(bone.bone['wz09_bone_id']) if bone else None,'constraint_name':constraint.name,'mute':False})
            constraint.mute=True
        if constraint_snapshot:target['wz09_baked_constraint_restore_json']=encoded(constraint_snapshot)
        result=bpy.data.actions.new(('Retarget · ' if retarget else 'Bake · ')+(source_clip['name'] if source_clip else source.name))
        _assign(target,result)
        previous_rotations={};previous_object_rotation=None
        for index,frame in enumerate(range(start,end+1)):
            context.scene.frame_set(frame)
            for bone in target.pose.bones:
                bone.rotation_mode='QUATERNION'
                bone.matrix_basis=samples[index].get(bone.name,Matrix.Identity(4))
                quat=bone.rotation_quaternion.copy()
                if bone.name in previous_rotations:quat.make_compatible(previous_rotations[bone.name])
                bone.rotation_quaternion=quat;previous_rotations[bone.name]=quat.copy()
                for channel in ('location','rotation_quaternion','scale'):bone.keyframe_insert(data_path=channel,frame=frame,group=bone.name)
            if (mapping and mapping['root_motion']=='copy_object') or (not mapping and settings.bake_object_motion):
                target.rotation_mode='QUATERNION';target.matrix_world=object_samples[index]
                quat=target.rotation_quaternion.copy()
                if previous_object_rotation is not None:quat.make_compatible(previous_object_rotation)
                target.rotation_quaternion=quat;previous_object_rotation=quat.copy()
                for channel in ('location','rotation_quaternion','scale'):target.keyframe_insert(data_path=channel,frame=frame)
        for curve in _curves(result):
            for point in curve.keyframe_points:point.interpolation='LINEAR'
        record=core.new_clip(target_record['id'],result.name,start,end,fps(context.scene),role='retarget' if retarget else 'baked',
            source_clip_id=source_clip['id'] if source_clip else None,root_motion='authored_object' if (mapping and mapping['root_motion']=='copy_object') or (not mapping and settings.bake_object_motion) else 'in_place')
        if source_clip:
            for event in source_clip['events']:
                if start<=event['frame']<=end:
                    derived=copy.deepcopy(event);derived.update(id=core.identifier(),source_event_id=event['id']);record['events'].append(derived)
        if mapping:record['bone_map_id']=mapping['id']
        _save_record(result,record);_event_markers(result,record);return result
    except Exception:
        for bone,constraint in live_constraints:constraint.mute=False
        target.matrix_world=original_target_world
        if old_target_action is not None:_assign(target,old_target_action,getattr(old_slot,'identifier',None))
        elif target.animation_data:target.animation_data.action=None
        if target.animation_data:target.animation_data.use_nla=old_nla
        raise
    finally:context.scene.frame_set(old_frame,subframe=old_subframe)


def bone_map_template(context,path):
    source=_active_rig(context);target=context.scene.wz09_animation_settings.target_rig
    if target is None:raise ValueError('Select a different target rig')
    left=register_rig(source);right=register_rig(target);by_name={b['name']:b for b in right['bones']}
    result={'id':core.identifier(),'source_rig_id':left['id'],'target_rig_id':right['id'],'mode':'rest_relative',
        'source_rest_fingerprint':left['rest_fingerprint'],'target_rest_fingerprint':right['rest_fingerprint'],
        'translation_scale':1.0,'root_motion':'ignore','reviewed':False,'entries':[
            {'source_bone_id':bone['id'],'source_name':bone['name'],'target_bone_id':by_name.get(bone['name'],{}).get('id'),
             'target_name':bone['name'] if bone['name'] in by_name else None} for bone in left['bones']]}
    path=Path(path)
    if path.exists():raise ValueError('Bone map file already exists')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');return result


def import_bone_map(context,path):
    mapping=json.loads(Path(path).read_text(encoding='utf-8'));library=serialize_scene(context.scene)
    core.validate_bone_map(mapping,{rig['id']:rig for rig in library['rigs']})
    mappings=json.loads(context.scene.get('wz09_bone_maps_json','[]'))
    if any(m['id']==mapping['id'] for m in mappings):raise ValueError('This bone map is already imported')
    mappings.append(mapping);context.scene['wz09_bone_maps_json']=encoded(mappings);return mapping


def verify_native_pose(context,rig,action=None,tolerance=.0002):
    """Compare actual evaluated Blender joints with existing native DObj samples."""
    from mathutils import Matrix,Vector
    report,capture=core.read_capture(rig['wz09_native_capture_path'])
    if core.file_sha256(rig['wz09_native_capture_path'])!=rig['wz09_native_capture_sha256']:raise ValueError('Native capture has changed')
    import bpy
    sources=[a for a in bpy.data.actions if 'wz09_animation_json' in a and _record(a)['rig_id']==rig['wz09_rig_id'] and _record(a)['role']=='source']
    if action is None:
        if len(sources)!=1:raise ValueError('Native pose validation requires exactly one original action')
        action=sources[0]
    record=_record(action)
    if action_fingerprint(action)!=action.get('wz09_source_fingerprint'):raise ValueError('Original action has changed')
    old=_current_action(rig) if rig.animation_data and rig.animation_data.action else None
    old_slot=getattr(rig.animation_data,'action_slot',None);old_nla=rig.animation_data.use_nla
    old_frame,old_sub=context.scene.frame_current,context.scene.frame_subframe
    initial=list(rig['wz09_native_initial_world']);initial=Matrix([initial[i:i+4] for i in range(0,16,4)])
    alignment=rig.matrix_world@initial.inverted();maximum=0.;checked=0
    try:
        _assign(rig,action,record.get('slot_identifier'))
        for sample in capture['clip']['pose_reference']:
            frame=record['frame_start']+sample['time_s']*record['fps']
            context.scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame));context.view_layer.update()
            for bone,expected in zip(capture['bones'],sample['bone_heads_editor']):
                actual=(rig.matrix_world@rig.pose.bones[bone['name']].matrix).translation
                maximum=max(maximum,(actual-alignment@Vector(expected)).length);checked+=1
        if maximum>tolerance:raise ValueError(f'Native/Blender-Gelenkposition weicht um {maximum:.6g} ab (Grenze {tolerance})')
        return {'max_native_joint_head_error':maximum,'joint_samples':checked,'tolerance':tolerance,
            'unmapped_animated_tracks':capture['clip'].get('unmapped_animated_tracks',[]),'runtime_integrated':False}
    finally:
        if old is not None:_assign(rig,old,getattr(old_slot,'identifier',None))
        else:rig.animation_data.action=None
        rig.animation_data.use_nla=old_nla;context.scene.frame_set(old_frame,subframe=old_sub)


def validate_current(context):
    rig=_active_rig(context);action=_current_action(rig);record=register_action(rig,action,context.scene)
    if abs(record['fps']-fps(context.scene))>1e-6:raise ValueError('Clip FPS differs from scene FPS; convert the working copy explicitly')
    if not list(_curves(action)):raise ValueError('Clip has no real keyframes yet')
    old_frame,old_sub=context.scene.frame_current,context.scene.frame_subframe
    samples=sorted({record['frame_start'],(record['frame_start']+record['frame_end'])/2,record['frame_end']})
    try:
        for frame in samples:
            context.scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame));context.view_layer.update()
            for bone in rig.pose.bones:
                if any(not math.isfinite(value) for row in bone.matrix for value in row):raise ValueError('Pose contains non-finite bone matrices')
        fingerprint=action_fingerprint(action)
        proof={'kind':'blender_animation_check','clip_id':record['id'],'rig_id':record['rig_id'],'clip_fingerprint':fingerprint,
            'checks':['real_action_keyframes','finite_evaluated_pose_samples','event_range_and_reference_validation'],
            'samples':samples,'runtime_integrated':False,'game_started':False}
        history=core.array(json.loads(context.scene.get('wz09_animation_exports_json','[]')),'Animation export history')
        proof['stale_exports_for_clip']=sum(item.get('clip_id')==record['id'] and item.get('channels_fingerprint')!=fingerprint for item in history)
        if 'wz09_native_capture_path' in rig:proof['native_source_import_check']=verify_native_pose(context,rig)
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
        target=_root(context.scene)/'worklog'/('KRV-21_ANIMATION_CHECK_'+stamp+'.json')
        target.write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
        record.update(channels_fingerprint=fingerprint,blender_verified=True,
            verification={'clip_fingerprint':fingerprint,'source':{'kind':'blender_verification','path':str(target),'sha256':core.file_sha256(target)}})
        _save_record(action,record);serialize_scene(context.scene);return proof
    finally:context.scene.frame_set(old_frame,subframe=old_sub)


def retime_current(context):
    rig=_active_rig(context);action=_editable(rig,context.scene);record=_record(action)
    old=record['fps'];new=fps(context.scene);start=record['frame_start'];ratio=new/old
    for curve in _curves(action):
        for point in curve.keyframe_points:
            for coordinate in (point.co,point.handle_left,point.handle_right):coordinate.x=start+(coordinate.x-start)*ratio
        curve.update()
    record['frame_end']=start+(record['frame_end']-start)*ratio
    record['events']=core.retime_events(record['events'],start,start,old,new);record['fps']=new
    _save_record(action,record);_event_markers(action,record)


def apply_native_root_delta(context):
    rig=_active_rig(context);action=_editable(rig,context.scene);record=_record(action)
    _,capture=core.read_capture(rig['wz09_native_capture_path'])
    samples=capture['clip']['root_delta_samples']
    if record['root_motion']=='object_delta':raise ValueError('Native root delta has already been applied to this clip')
    from mathutils import Vector,Matrix
    baseline=Vector(samples[0]['delta_native']);base=rig.location.copy()
    raw=list(rig['wz09_native_initial_world']);initial=Matrix([raw[i:i+4] for i in range(0,16,4)])
    alignment=(rig.matrix_world@initial.inverted()).to_3x3()
    parent_inverse=rig.parent.matrix_world.inverted().to_3x3() if rig.parent else Matrix.Identity(3)
    for sample in samples:
        rig.location=base+parent_inverse@alignment@((Vector(sample['delta_native'])-baseline)*capture['source_units_to_editor_unit'])
        rig.keyframe_insert(data_path='location',frame=record['frame_start']+sample['time_s']*record['fps'])
    record['root_motion']='object_delta';_save_record(action,record)


def attach_object(context):
    from mathutils import Matrix
    settings=context.scene.wz09_animation_settings;rig=_active_rig(context);obj=settings.attach_object
    if obj is None or obj==rig or obj.type=='ARMATURE':raise ValueError('Select a separate authored object or asset placement')
    if obj.get('wz09_type')=='preview' or obj.get('wz09_asset_part'):raise ValueError('Select an authored asset placement instead of a protected source mesh')
    ancestor=rig
    while ancestor:
        if ancestor==obj:raise ValueError('Bone attachment would create a parent cycle')
        ancestor=ancestor.parent
    rig_record=register_rig(rig);bone=rig.pose.bones.get(settings.attach_bone)
    if bone is None:raise ValueError('Select an existing bone explicitly')
    uid=str(obj.get('wz09_asset_placement_id') or obj.get('wz09_animation_object_id') or core.identifier())
    records=json.loads(context.scene.get('wz09_animation_attachments_json','[]'))
    if any(r['object_id']==uid for r in records):raise ValueError('Object is already registered as a bone attachment')
    obj['wz09_animation_object_id']=uid;matrix=obj.matrix_world.copy()
    obj.parent=rig;obj.parent_type='BONE';obj.parent_bone=bone.name;obj.matrix_world=matrix
    record={'id':core.identifier(),'rig_id':rig_record['id'],'bone_id':str(bone.bone['wz09_bone_id']),'object_id':uid,
        'offset_matrix':[float(v) for row in ((rig.matrix_world@bone.matrix).inverted()@matrix) for v in row]}
    records.append(record);context.scene['wz09_animation_attachments_json']=encoded(records);return record


def _check_export_mesh_membership(document,rig_id,attachment_ids):
    """Reject selected objects leaked in from another Blender scene."""
    nodes=document.get('nodes',[]);parents={};attachments=set(attachment_ids)
    for index,node in enumerate(nodes):
        for child in node.get('children',[]):
            if type(child) is not int or not 0<=child<len(nodes) or child in parents:raise ValueError('Invalid exported animation node hierarchy')
            parents[child]=index
    for index,node in enumerate(nodes):
        if 'mesh' not in node:continue
        current=index;seen=set();allowed=False
        while current is not None:
            if current in seen:raise ValueError('Cycle in exported animation node hierarchy')
            seen.add(current);extras=nodes[current].get('extras',{})
            if (extras.get('wz09_rig_owner_id')==rig_id or extras.get('wz09_rig_id')==rig_id
                    or any(extras.get(key) in attachments for key in ('wz09_animation_object_id','wz09_asset_placement_id'))):
                allowed=True;break
            current=parents.get(current)
        if not allowed:raise ValueError('Animation GLB contains a mesh outside the selected rig/declared attachments')


def _write_editable_library(context,stage,actions,path,rig):
    """Synchronize the real stage ViewLayer before compact partial publication.

    The exclusive sibling-file publication follows build_light_editor.publish_blend.
    No source .blend is saved or replaced, and no whole world snapshot is needed.
    """
    import bpy
    path=Path(path)
    if path.exists():raise FileExistsError('Editable animation version already exists')
    pending=path.with_name('.'+path.stem+'-pending-'+uuid.uuid4().hex+'.blend')
    original_scene=context.scene;window=getattr(context,'window',None)
    original_frame,original_subframe=original_scene.frame_current,original_scene.frame_subframe
    try:
        if window is not None:window.scene=stage
        for layer in stage.view_layers:
            with context.temp_override(scene=stage,view_layer=layer):
                layer.update()
                # PartialWriteContext copies this tree. Its C++ copy routine
                # dereferences the first LayerCollection, so an unsynced newly
                # created Scene must never be passed to libraries.write.
                master=layer.layer_collection
                if master is None or master.collection!=stage.collection or layer.objects.get(rig.name)!=rig:
                    raise ValueError('Export stage has no synchronized master view layer')
        view_layer=stage.view_layers[0];view_layer.objects.active=rig
        rig.select_set(True,view_layer=view_layer);stage.frame_set(stage.frame_start)
        with context.temp_override(scene=stage,view_layer=view_layer):
            view_layer.update()
            bpy.data.libraries.write(str(pending),{stage,*actions},path_remap='RELATIVE_ALL',fake_user=True)
        if not pending.is_file():raise ValueError('Blender did not write an editable animation library')
        os.link(pending,path)
    finally:
        if window is not None:window.scene=original_scene
        original_scene.frame_set(original_frame,subframe=original_subframe)
        context.view_layer.update();pending.unlink(missing_ok=True)


def export_bundle(context,path):
    import bpy
    from .source_bundle import capture_gltf_bundle,_document
    root=_root(context.scene);path=Path(path).resolve()
    if not path.is_relative_to(root):raise ValueError('Save animation products inside the standalone editor project')
    if path.suffix.lower()!='.json':raise ValueError('Animation manifest requires .json')
    glb=path.with_suffix('.glb');blend=path.with_suffix('.blend');events=path.with_name(path.stem+'.events.json')
    if any(p.exists() for p in (path,glb,blend,events)):raise ValueError('Output name already exists; choose a new version name')
    if not path.parent.is_dir():raise ValueError('Select an existing output folder')
    rig=_active_rig(context);action=_current_action(rig);record=register_action(rig,action,context.scene)
    if abs(record['fps']-fps(context.scene))>1e-6:raise ValueError('Convert the clip to scene FPS before export')
    if not list(_curves(action)):raise ValueError('No real clip available for export')
    library=serialize_scene(context.scene)
    def rig_members(owner):
        uid=owner['wz09_rig_id'];objects={obj for obj in context.scene.objects if obj==owner or obj.get('wz09_rig_owner_id')==uid or
            any(m.type=='ARMATURE' and m.object==owner for m in obj.modifiers)}
        for attachment in library['attachments']:
            if attachment['rig_id']!=uid:continue
            matches=[obj for obj in context.scene.objects if attachment['object_id'] in (obj.get('wz09_animation_object_id'),obj.get('wz09_asset_placement_id'))]
            if len(matches)!=1:raise ValueError('Bone attachment requires exactly one existing object with its stable ID')
            objects.add(matches[0]);objects.update(matches[0].children_recursive)
        return objects
    members=rig_members(rig)
    # The editable library keeps the source rigs/actions used by retarget links.
    # The model-animation GLB remains the explicitly selected rig and attachments.
    editable=set()
    for owner in context.scene.objects:
        if owner.type=='ARMATURE' and owner.get('wz09_rig_id') in {r['id'] for r in library['rigs']}:editable.update(rig_members(owner))
    old_selection=list(context.selected_objects);old_active=context.active_object
    stage=None
    try:
        if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:obj.select_set(False)
        for obj in members:obj.select_set(True)
        context.view_layer.objects.active=rig
        rna=bpy.ops.export_scene.gltf.get_rna_type().properties
        params={'filepath':str(glb),'export_format':'GLB','use_selection':True,'export_animations':True,'export_skins':True,'export_yup':True}
        if 'export_extras' in rna:params['export_extras']=True
        if 'use_active_scene' in rna:params['use_active_scene']=True
        if 'export_animation_mode' in rna:params['export_animation_mode']='ACTIVE_ACTIONS'
        elif 'export_nla_strips' in rna:params['export_nla_strips']=False
        if 'export_force_sampling' in rna:params['export_force_sampling']=True
        result=bpy.ops.export_scene.gltf(**params)
        if result!={'FINISHED'}:raise ValueError('The Blender glTF exporter did not export the clip')
        document,_=_document(glb)
        if not document.get('skins') or not document.get('animations'):raise ValueError('Export contains no real skin or animation structure')
        attachment_ids=[a['object_id'] for a in library['attachments'] if a['rig_id']==rig['wz09_rig_id']]
        _check_export_mesh_membership(document,rig['wz09_rig_id'],attachment_ids)
        stage=bpy.data.scenes.new('WZ09_ANIMATION_EXPORT_'+record['id'][:8]);stage.render.fps=context.scene.render.fps;stage.render.fps_base=context.scene.render.fps_base
        stage.frame_start=math.floor(record['frame_start']);stage.frame_end=math.ceil(record['frame_end'])
        for obj in editable:stage.collection.objects.link(obj)
        if hasattr(stage,'wz09_project_root'):stage.wz09_project_root=str(root)
        if hasattr(stage,'wz09_workspace'):stage.wz09_workspace='ANIMATION'
        if hasattr(stage,'wz09_animation_settings'):
            settings=stage.wz09_animation_settings;settings.rig=rig;settings.search='';settings.clip=record['id']
            settings.bake_start=stage.frame_start;settings.bake_end=stage.frame_end
        for key in ('wz09_bone_maps_json','wz09_animation_attachments_json'):
            if key in context.scene:stage[key]=context.scene[key]
        actions={a for a in bpy.data.actions if 'wz09_animation_json' in a and _record(a)['id'] in {c['id'] for c in library['clips']}}
        _write_editable_library(context,stage,actions,blend,rig)
        events.write_text(json.dumps({'version':1,'clip_id':record['id'],'fps':record['fps'],'frame_start':record['frame_start'],
            'events':record['events'],'runtime_integrated':False},indent=2)+'\n',encoding='utf-8')
        output={'kind':'wz09_animation_export','version':1,'export_id':core.identifier(),'selected_rig_id':rig['wz09_rig_id'],'selected_clip_id':record['id'],
            'animation_studio':library,'files':{key:{'path':p.name,'sha256':core.file_sha256(p)} for key,p in [('model_animation',glb),('editable_blend',blend),('events',events)]},
            'selected_attachment_object_ids':attachment_ids,
            'model_animation_bundle':capture_gltf_bundle(glb,include_images=True),'runtime_integrated':False,'game_started':False}
        path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        binding={'id':output['export_id'],'rig_id':rig['wz09_rig_id'],'clip_id':record['id'],'channels_fingerprint':record['channels_fingerprint'],
            'source':{'kind':'blender_export','path':str(path),'sha256':core.file_sha256(path)},'object_ids':output['selected_attachment_object_ids']}
        core.validate_export_record(binding,{item['id']:item for item in library['rigs']},{item['id']:item for item in library['clips']})
        history=core.array(json.loads(context.scene.get('wz09_animation_exports_json','[]')),'Animation export history')
        context.scene['wz09_animation_exports_json']=encoded(history+[binding])
        return output
    finally:
        if stage is not None:bpy.data.scenes.remove(stage)
        for obj in context.selected_objects:obj.select_set(False)
        for obj in old_selection:
            if obj.name in context.view_layer.objects:obj.select_set(True)
        if old_active and old_active.name in context.view_layer.objects:context.view_layer.objects.active=old_active


_ENUM_CACHE={}
def _clip_items(self,context):
    import bpy
    items=[]
    if context is not None:
        try:
            rig=_active_rig(context);uid=str(rig.get('wz09_rig_id',''));query=self.search.casefold()
            for action in bpy.data.actions:
                if 'wz09_animation_json' not in action:continue
                record=_record(action)
                if record['rig_id']==uid and (not query or query in action.name.casefold()):
                    items.append((record['id'],('Source - ' if record['role']=='source' else '')+action.name,record['role']))
        except (ValueError,ReferenceError):pass
    _ENUM_CACHE['clips']=items or [('NONE','No registered clips','')];return _ENUM_CACHE['clips']
def _bone_items(self,context,target=False):
    try:
        rig=self.target_rig if target else _active_rig(context)
        if rig is None:raise ValueError('No rig')
        record=_existing_rig_record(rig);items=[(b['id'],b['name'],'') for b in record['bones']]
    except (ValueError,ReferenceError):items=[]
    key='target_bones' if target else 'source_bones';_ENUM_CACHE[key]=items or [('NONE','No rig','')];return _ENUM_CACHE[key]
def _source_bones(self,context):return _bone_items(self,context)
def _target_bones(self,context):return _bone_items(self,context,True)


def _command(context,operation,uid=''):
    import bpy
    settings=context.scene.wz09_animation_settings
    if operation=='NEW_SCENE':
        root=_root(context.scene);scene=bpy.data.scenes.new('WZ09 Animation Studio')
        if hasattr(scene,'wz09_project_root'):scene.wz09_project_root=str(root)
        scene.render.fps=30
        if context.window:context.window.scene=scene
        if hasattr(scene,'wz09_workspace'):scene.wz09_workspace='ANIMATION'
        return
    if operation=='REGISTER':
        rig=context.active_object
        if rig is None or rig.type!='ARMATURE':raise ValueError('Select an existing armature')
        settings.rig=rig
        duplicate='wz09_rig_id' in rig and any(obj!=rig and obj.type=='ARMATURE' and obj.get('wz09_rig_id')==rig['wz09_rig_id'] for obj in bpy.data.objects)
        register_rig(rig,fresh=duplicate)
        if rig.animation_data and rig.animation_data.action:register_action(rig,rig.animation_data.action,context.scene)
        if rig.animation_data:
            for track in rig.animation_data.nla_tracks:
                for strip in track.strips:
                    if strip.action:register_action(rig,strip.action,context.scene)
        return
    rig=_active_rig(context)
    if operation=='NEW_CLIP':
        register_rig(rig);action=bpy.data.actions.new(settings.clip_name);_assign(rig,action)
        record=core.new_clip(rig['wz09_rig_id'],action.name,settings.bake_start,settings.bake_end,fps(context.scene))
        _save_record(action,record);return
    if operation=='COPY':working_copy(rig,context.scene);return
    if operation=='ACTIVATE':
        matches=[a for a in bpy.data.actions if 'wz09_animation_json' in a and _record(a)['id']==settings.clip]
        if len(matches)!=1:raise ValueError('Select an existing clip')
        record=_record(matches[0]);_assign(rig,matches[0],record.get('slot_identifier'))
        context.scene.frame_start=math.floor(record['frame_start']);context.scene.frame_end=math.ceil(record['frame_end'])
        settings.bake_start=math.floor(record['frame_start']);settings.bake_end=math.ceil(record['frame_end']);return
    if operation in {'PLAY','PAUSE'}:
        if context.screen is None:raise ValueError('Playback is available in the open Blender interface')
        if operation=='PLAY' or context.screen.is_animation_playing:bpy.ops.screen.animation_play()
        return
    if operation=='KEY':
        action=_editable(rig,context.scene);bones=[b for b in rig.pose.bones if getattr(b,'select',getattr(b.bone,'select',False))]
        if not bones:raise ValueError('Select at least one pose bone')
        for bone in bones:
            bone.rotation_mode='QUATERNION'
            for channel in ('location','rotation_quaternion','scale'):bone.keyframe_insert(data_path=channel,frame=context.scene.frame_current,group=bone.name)
        _save_record(action,_record(action));return
    if operation=='NLA':add_nla(context);return
    if operation=='NLA_PREVIEW':rig.animation_data_create().action=None;rig.animation_data.use_nla=True;return
    if operation=='BAKE':bake_action(context);return
    if operation=='RETARGET':bake_action(context,True);return
    if operation=='ADD_EVENT':add_event(context);return
    if operation=='REMOVE_EVENT':
        action=_editable(rig,context.scene);record=_record(action)
        previous=len(record['events']);record['events']=[event for event in record['events'] if event['id']!=uid and event.get('source_event_id')!=uid]
        if len(record['events'])==previous:raise ValueError('Event ID not found')
        _save_record(action,record);_event_markers(action,record);return
    if operation=='SYNC_EVENTS':
        action=_current_action(rig);record=_record(action)
        if record['role']=='source':raise ValueError('Source markers are retained; create a working copy first')
        for event in record['events']:
            label='WZ09 · '+event['id'][:8]+' · '+event['name'];marker=action.pose_markers.get(label)
            if marker is None:raise ValueError('Event marker missing; remove the event through the event list')
            if marker.frame!=round(event['frame']):event['frame']=float(marker.frame)
            core.validate_event(event,record['frame_start'],record['frame_end'])
        _save_record(action,record);return
    if operation=='RETIME':retime_current(context);return
    if operation=='ROOT_DELTA':apply_native_root_delta(context);return
    if operation=='VALIDATE':validate_current(context);return
    if operation=='ATTACH':attach_object(context);return
    if operation=='RESTORE_CONSTRAINTS':
        names={str(b['wz09_bone_id']):b.name for b in rig.data.bones}
        states=json.loads(rig.get('wz09_baked_constraint_restore_json','[]'))
        found=[]
        for state in states:
            owner=rig.pose.bones[names[state['bone_id']]] if state['bone_id'] else rig
            constraint=owner.constraints.get(state['constraint_name'])
            if constraint is None:raise ValueError('Saved constraint changed; undo or inspect manually')
            found.append((constraint,state['mute']))
        for constraint,mute in found:constraint.mute=mute
        if 'wz09_baked_constraint_restore_json' in rig:del rig['wz09_baked_constraint_restore_json']
        return
    if operation in {'MAP_ADD','MAP_PROPOSE','MAP_CLEAR','MAP_COMMIT'}:
        left=register_rig(rig);right=register_rig(settings.target_rig) if settings.target_rig else None
        if right is None or right['id']==left['id']:raise ValueError('Select another target rig')
        draft=json.loads(context.scene.get('wz09_bone_map_draft_json','{}'))
        if operation=='MAP_CLEAR':context.scene['wz09_bone_map_draft_json']='{}';return
        if not draft:draft={'id':core.identifier(),'source_rig_id':left['id'],'target_rig_id':right['id'],
            'source_rest_fingerprint':left['rest_fingerprint'],'target_rest_fingerprint':right['rest_fingerprint'],
            'mode':'rest_relative','translation_scale':settings.translation_scale,'root_motion':settings.retarget_root,'reviewed':False,'entries':[]}
        if (draft['source_rig_id'],draft['target_rig_id'])!=(left['id'],right['id']):raise ValueError('Draft belongs to different rigs; clear it first')
        if operation=='MAP_PROPOSE':
            target_names={b['name']:b['id'] for b in right['bones']}
            draft['entries']=[{'source_bone_id':b['id'],'target_bone_id':target_names[b['name']]} for b in left['bones'] if b['name'] in target_names]
        if operation=='MAP_ADD':
            pair={'source_bone_id':settings.source_bone,'target_bone_id':settings.target_bone}
            draft['entries']=[entry for entry in draft['entries'] if entry['source_bone_id']!=pair['source_bone_id']];draft['entries'].append(pair)
        if operation=='MAP_COMMIT':
            if not settings.mapping_reviewed:raise ValueError('Explicitly verify and confirm bind pose, pairs, root motion and scale factor')
            draft.update(reviewed=True,translation_scale=settings.translation_scale,root_motion=settings.retarget_root,
                source_rest_fingerprint=left['rest_fingerprint'],target_rest_fingerprint=right['rest_fingerprint'])
            core.validate_bone_map(draft,{left['id']:left,right['id']:right})
            records=json.loads(context.scene.get('wz09_bone_maps_json','[]'))
            if any((m['source_rig_id'],m['target_rig_id'])==(left['id'],right['id']) for m in records):raise ValueError('A bone map already exists for this rig pair')
            records.append(draft);context.scene['wz09_bone_maps_json']=encoded(records);context.scene['wz09_bone_map_draft_json']='{}';settings.mapping_reviewed=False;return
        context.scene['wz09_bone_map_draft_json']=encoded(draft);settings.mapping_reviewed=False;return
    if operation in {'ACTION_EDITOR','GRAPH_EDITOR','NLA_EDITOR'}:
        if context.screen is None:raise ValueError('Editor view requires an open Blender interface')
        area=next((a for a in context.screen.areas if a.type in {'DOPESHEET_EDITOR','GRAPH_EDITOR','NLA_EDITOR','TIMELINE'}),context.area)
        area.type='DOPESHEET_EDITOR' if operation=='ACTION_EDITOR' else operation
        if operation=='ACTION_EDITOR':area.spaces.active.mode='ACTION'
        return
    raise ValueError('Unknown Animation Studio operation')


def register():
    import bpy
    from bpy.props import StringProperty,PointerProperty,EnumProperty,IntProperty,FloatProperty,BoolProperty
    from bpy_extras.io_utils import ImportHelper,ExportHelper
    class WZ09AnimationSettings(bpy.types.PropertyGroup):
        rig:PointerProperty(name='Rig',type=bpy.types.Object,poll=lambda self,obj:obj.type=='ARMATURE')
        target_rig:PointerProperty(name='Target rig',type=bpy.types.Object,poll=lambda self,obj:obj.type=='ARMATURE')
        clip:EnumProperty(name='Clip',items=_clip_items)
        search:StringProperty(name='Search clips')
        clip_name:StringProperty(name='New clip',default='Eigene Animation')
        bake_start:IntProperty(name='Von',default=1,min=-100000,max=100000)
        bake_end:IntProperty(name='Bis',default=60,min=-100000,max=100000)
        bake_object_motion:BoolProperty(name='Include object motion in bake',default=True)
        mute_baked_constraints:BoolProperty(name='Mute target constraints after visual baking',default=True)
        event_name:StringProperty(name='Event name',default='notify')
        event_type:EnumProperty(name='Typ',items=[(kind,kind.title(),'') for kind in core.EVENT_TYPES])
        event_payload:StringProperty(name='Payload JSON',default='{}')
        blend_mode:EnumProperty(name='Blend mode',items=[('REPLACE','Replace',''),('ADD','Additive - authored draft',''),('COMBINE','Combine','')])
        influence:FloatProperty(name='Influence',default=1,min=0,max=1)
        repeat:FloatProperty(name='Repeats',default=1,min=.01,max=100)
        source_bone:EnumProperty(name='Source bone',items=_source_bones)
        target_bone:EnumProperty(name='Target bone',items=_target_bones)
        translation_scale:FloatProperty(name='Translation factor',default=1,min=.000001,max=1000)
        retarget_root:EnumProperty(name='Root Motion',items=[('ignore','Objektbewegung ignorieren',''),('copy_object','Objektbewegung kopieren','')])
        mapping_reviewed:BoolProperty(name='Bone pairs / bind pose explicitly verified',default=False)
        attach_object:PointerProperty(name='Authored object',type=bpy.types.Object)
        attach_bone:StringProperty(name='Bone')
    class WZ09_OT_animation_command(bpy.types.Operator):
        bl_idname='wz09.animation_command';bl_label='Animation Studio';bl_options={'REGISTER','UNDO'}
        operation:StringProperty();uid:StringProperty()
        def execute(self,context):
            try:_command(context,self.operation,self.uid);return {'FINISHED'}
            except (ValueError,RuntimeError,OSError,KeyError,TypeError,AttributeError) as exc:self.report({'ERROR'},str(exc)[:250]);return {'CANCELLED'}
    class WZ09_OT_animation_import(bpy.types.Operator,ImportHelper):
        bl_idname='wz09.animation_import';bl_label='Import native rig and original clip';bl_options={'REGISTER','UNDO'}
        filename_ext='.json';filter_glob:StringProperty(default='*.json',options={'HIDDEN'})
        mode:EnumProperty(items=[('CAPTURE','Native Capture',''),('BONE_MAP','Verified bone map','')],default='CAPTURE')
        def execute(self,context):
            try:
                result=import_capture(context,self.filepath) if self.mode=='CAPTURE' else import_bone_map(context,self.filepath)
                if self.mode=='CAPTURE':
                    try:_fit_imported_capture(context,context.scene.wz09_animation_settings.rig)
                    except (ValueError,RuntimeError,AttributeError,ReferenceError) as exc:self.report({'WARNING'},'Imported; press Home to frame the view: '+str(exc)[:120])
                self.report({'INFO'},'Verified animation data imported');return {'FINISHED'}
            except (ValueError,RuntimeError,OSError,KeyError,TypeError) as exc:self.report({'ERROR'},str(exc)[:250]);return {'CANCELLED'}
    class WZ09_OT_animation_export(bpy.types.Operator,ExportHelper):
        bl_idname='wz09.animation_export';bl_label='Export animation as new version'
        filename_ext='.json';filter_glob:StringProperty(default='*.json',options={'HIDDEN'})
        def invoke(self,context,event):
            self.filepath=str(_root(context.scene)/'exports'/('animation-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.json'))
            return super().invoke(context,event)
        def execute(self,context):
            try:export_bundle(context,self.filepath);self.report({'INFO'},'New GLB, blend, event and manifest versions saved');return {'FINISHED'}
            except (ValueError,RuntimeError,OSError,KeyError,TypeError) as exc:self.report({'ERROR'},str(exc)[:250]);return {'CANCELLED'}
    class WZ09_PT_animation_studio(bpy.types.Panel):
        bl_label='Animation Studio';bl_idname='WZ09_PT_animation_studio'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_parent_id='WZ09_PT_main'
        @classmethod
        def poll(cls,context):return getattr(context.scene,'wz09_workspace','')=='ANIMATION'
        def draw(self,context):
            layout=self.layout;s=context.scene.wz09_animation_settings
            def command(parent,label,operation,icon='NONE'):
                operator=parent.operator('wz09.animation_command',text=label,icon=icon);operator.operation=operation;return operator
            row=layout.row(align=True);command(row,'New animation scene','NEW_SCENE','SCENE_DATA');layout.operator('wz09.animation_import',icon='IMPORT')
            layout.prop(s,'rig');command(layout,'Register selected rig','REGISTER','ARMATURE_DATA')
            try:rig=_active_rig(context)
            except ValueError:layout.label(text='Select a rig or import native capture',icon='INFO');return
            box=layout.box();box.label(text='Clip library',icon='ACTION');box.prop(s,'search',text='',icon='VIEWZOOM');box.prop(s,'clip')
            row=box.row(align=True);command(row,'Activate','ACTIVATE','CHECKMARK');command(row,'Working copy','COPY','DUPLICATE')
            row=box.row(align=True);box.prop(s,'clip_name');command(box,'Create new clip','NEW_CLIP','ADD')
            row=layout.row(align=True);command(row,'Play','PLAY','PLAY');command(row,'Pause','PAUSE','PAUSE');layout.prop(context.scene,'frame_current',text='Frame')
            row=layout.row(align=True);command(row,'Action','ACTION_EDITOR','ACTION');command(row,'Graph','GRAPH_EDITOR','GRAPH');command(row,'NLA','NLA_EDITOR','NLA')
            command(layout,'Key pose bone location / rotation / scale','KEY','KEY_HLT')
            if rig.animation_data and rig.animation_data.action:
                action=rig.animation_data.action
                try:
                    record=_record(action);layout.label(text=('Original source - edit a working copy' if record['role']=='source' else record['name']),icon='LOCKED' if record['role']=='source' else 'EDITMODE_HLT')
                    if abs(record['fps']-fps(context.scene))>1e-6:command(layout,'Convert clip to current scene FPS','RETIME','TIME')
                    if record.get('unmapped_native_tracks'):layout.label(text=f"{len(record['unmapped_native_tracks'])} native additional tracks without bones",icon='INFO')
                    if record.get('unmapped_animated_tracks'):layout.label(text='Animated bones missing: inspect capture',icon='ERROR')
                    box=layout.box();box.label(text='Animation Events',icon='MARKER_HLT')
                    box.prop(s,'event_name');box.prop(s,'event_type');box.prop(s,'event_payload');command(box,'Event at current frame','ADD_EVENT','ADD')
                    for event in record['events'][:20]:
                        row=box.row(align=True);row.label(text=f"{event['frame']:.2f} · {event['name']}");command(row,'','REMOVE_EVENT','X').uid=event['id']
                    command(box,'Apply moved action markers','SYNC_EVENTS','FILE_REFRESH')
                except ValueError:layout.label(text='Register the action first',icon='INFO')
            box=layout.box();box.label(text='NLA and baking',icon='NLA');box.prop(s,'blend_mode');box.prop(s,'influence');box.prop(s,'repeat')
            row=box.row(align=True);command(row,'Create NLA strip','NLA','ADD');command(row,'Play NLA','NLA_PREVIEW','NLA')
            row=box.row(align=True);row.prop(s,'bake_start');row.prop(s,'bake_end');box.prop(s,'bake_object_motion');box.prop(s,'mute_baked_constraints');command(box,'Bake evaluated pose to a new action','BAKE','ACTION')
            if 'wz09_baked_constraint_restore_json' in rig:command(box,'Restore constraint state before baking','RESTORE_CONSTRAINTS','FILE_REFRESH')
            box=layout.box();box.label(text='Explicit bone map / retarget',icon='CONSTRAINT_BONE');box.prop(s,'target_rig')
            row=box.row(align=True);row.prop(s,'source_bone');row.prop(s,'target_bone');command(box,'Add bone pair','MAP_ADD','ADD')
            row=box.row(align=True);command(row,'Suggest exact-name matches','MAP_PROPOSE','SORTALPHA');command(row,'Clear','MAP_CLEAR','X')
            draft=json.loads(context.scene.get('wz09_bone_map_draft_json','{}'));box.label(text=f"Draft: {len(draft.get('entries', []))} Bonepaare")
            box.prop(s,'translation_scale');box.prop(s,'retarget_root');box.prop(s,'mapping_reviewed');command(box,'Apply verified mapping','MAP_COMMIT','CHECKMARK')
            command(box,'Retarget to a new baked action','RETARGET','ACTION')
            box=layout.box();box.label(text='Attach object to bone',icon='CONSTRAINT_BONE');box.prop(s,'attach_object');box.prop_search(s,'attach_bone',rig.data,'bones');command(box,'Attach while preserving world transform','ATTACH','LINKED')
            if 'wz09_native_capture_path' in rig:command(layout,'Explicitly apply native root translation','ROOT_DELTA','ORIENTATION_GLOBAL')
            command(layout,'Validate clip offline in Blender','VALIDATE','CHECKMARK');layout.operator('wz09.animation_export',icon='EXPORT')
            if 'wz09_animation_exports_json' in context.scene:
                layout.label(text=f"Exporthistorie: {len(json.loads(context.scene['wz09_animation_exports_json']))} - validating current channel version",icon='FILE_TICK')
            layout.label(text='Blender authoring - runtime unverified',icon='INFO')
    _CLASSES[:]=[WZ09AnimationSettings,WZ09_OT_animation_command,WZ09_OT_animation_import,WZ09_OT_animation_export,WZ09_PT_animation_studio]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_animation_settings=PointerProperty(type=WZ09AnimationSettings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_animation_settings'):del bpy.types.Scene.wz09_animation_settings
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear();_ENUM_CACHE.clear()
