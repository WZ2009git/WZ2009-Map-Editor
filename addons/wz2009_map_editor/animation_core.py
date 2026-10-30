"""Pure animation contracts; Blender owns curves, posing and NLA evaluation."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

VERSION=1
EVENT_TYPES=('notify','audio','vfx','gameplay','custom')
SHA=re.compile(r'^[0-9a-f]{64}$')

def identifier():return str(uuid.uuid4())
def stable_id(*parts):return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:animation:'+':'.join(map(str,parts))))
def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1048576),b''):digest.update(block)
    return digest.hexdigest()
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def text(value,label):
    if not isinstance(value,str) or not value.strip() or len(value)>512:raise ValueError(label+' must be nonempty text')
    return value
def number(value,label,minimum=None):
    if isinstance(value,bool) or not isinstance(value,(float,int)):raise ValueError(label+' must be finite')
    try:finite=math.isfinite(value)
    except OverflowError:finite=False
    if not finite:raise ValueError(label+' must be finite')
    if minimum is not None and value<minimum:raise ValueError(label+' is below its minimum')
    return value
def integer(value,label,minimum=0):
    if type(value) is not int or value<minimum:raise ValueError(label+' must be an integer >= '+str(minimum))
    return value
def object_fields(value,required,optional=(),label='Animation record'):
    if not isinstance(value,dict):raise ValueError(label+' must be an object')
    if any(not isinstance(key,str) for key in value):raise ValueError(label+' keys must be strings')
    missing=set(required)-value.keys();unknown=value.keys()-set(required)-set(optional)
    if missing or unknown:raise ValueError(label+' missing/unknown fields: '+str(sorted(missing))+' / '+str(sorted(unknown)))
    return value
def array(value,label):
    if not isinstance(value,list):raise ValueError(label+' must be an array')
    return value
def boolean(value,label):
    if type(value) is not bool:raise ValueError(label+' must be boolean')
    return value
def hash_value(value,label):
    if not isinstance(value,str) or not SHA.fullmatch(value):raise ValueError(label+' must be lowercase SHA-256')
    return value
def choice(value,choices,label):
    if text(value,label) not in choices:raise ValueError('Unknown '+label)
    return value
def matrix(value,label):
    if not isinstance(value,list) or len(value)!=16:raise ValueError(label+' requires 16 row-major values')
    for item in value:number(item,label)
    if any(abs(value[i]-expected)>1e-6 for i,expected in ((12,0),(13,0),(14,0),(15,1))):raise ValueError(label+' must be affine')
    a,b,c,d,e,f,g,h,i=value[0],value[1],value[2],value[4],value[5],value[6],value[8],value[9],value[10]
    if abs(a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g))<1e-10:raise ValueError(label+' is singular')
    return value
def json_value(value,depth=0):
    if depth>32:raise ValueError('Event payload exceeds 32 nesting levels')
    if value is None or type(value) in (str,bool):return
    if type(value) in (int,float):number(value,'Event payload number');return
    if isinstance(value,list):
        for item in value:json_value(item,depth+1)
        return
    if isinstance(value,dict):
        for key,item in value.items():
            if not isinstance(key,str):raise ValueError('Event payload object keys must be strings')
            json_value(item,depth+1)
        return
    raise ValueError('Event payload contains a non-JSON value')
def rig_rest_fingerprint(bones):
    names={bone['id']:bone['name'] for bone in bones}
    return fingerprint(sorted([[bone['name'],names.get(bone['parent_id']),bone['rest_matrix_local']] for bone in bones],key=lambda value:value[0]))
def source_reference(value,verify=True):
    if value is None:return
    object_fields(value,('kind','path','sha256'),label='Animation source')
    choice(value['kind'],{'native_capture','imported_file','blender_export','blender_verification'},'animation source reference')
    path=Path(text(value.get('path'),'Animation source path'))
    hash_value(value['sha256'],'Animation source SHA-256')
    if not path.is_absolute():raise ValueError('Animation source requires absolute path and SHA-256')
    if verify and (not path.is_file() or file_sha256(path)!=value['sha256']):raise ValueError('Animation source missing or changed: '+str(path))


def validate_skeleton(bones):
    if not isinstance(bones,list) or not bones:raise ValueError('Rig requires actual bones')
    lookup={};names=set()
    for bone in bones:
        object_fields(bone,('id','name','parent_id','rest_matrix_local'),label='Bone')
        uid=text(bone.get('id'),'Bone ID');name=text(bone.get('name'),'Bone name')
        matrix(bone['rest_matrix_local'],'Bone bind matrix')
        if uid in lookup or name in names:raise ValueError('Duplicate bone ID/name')
        lookup[uid]=bone;names.add(name)
    for bone in bones:
        parent=bone.get('parent_id')
        if parent is not None:text(parent,'Parent bone ID')
        if parent is not None and parent not in lookup:raise ValueError('Bone parent does not exist')
        visited={bone['id']}
        while parent is not None:
            if parent in visited:raise ValueError('Bone hierarchy contains a cycle')
            visited.add(parent);parent=lookup[parent].get('parent_id')
    return lookup


def validate_event(event,start,end):
    object_fields(event,('id','name','frame','type','payload'),('source_event_id',),'Animation event')
    text(event.get('id'),'Event ID');text(event.get('name'),'Event name')
    frame=number(event.get('frame'),'Event frame')
    if not start<=frame<=end:raise ValueError('Animation event is outside the clip range')
    if event.get('type') not in EVENT_TYPES:raise ValueError('Unknown animation event type')
    if not isinstance(event.get('payload'),dict):raise ValueError('Event payload must be a JSON object')
    json_value(event['payload'])
    if 'source_event_id' in event:text(event['source_event_id'],'Source event ID')
    return event


def validate_library(data,verify_sources=True):
    object_fields(data,('version','rigs','clips','nla','bone_maps','attachments'),('kind','id','runtime_integrated','exports'),'Animation library')
    if type(data['version']) is not int or data['version']!=VERSION:raise ValueError('Unsupported animation library version')
    if 'kind' in data and data['kind']!='wz09_animation_studio':raise ValueError('Unsupported animation studio product kind')
    if 'id' in data:text(data['id'],'Animation product ID')
    if 'runtime_integrated' in data and data['runtime_integrated'] is not False:raise ValueError('Animation product is not runtime verified')
    for key in ('rigs','clips','nla','bone_maps','attachments'):array(data[key],key)
    ids=set();rigs={};clips={}
    def add(uid):
        text(uid,'Animation ID')
        if uid in ids:raise ValueError('Duplicate global animation ID: '+uid)
        ids.add(uid)
    for rig in data.get('rigs',[]):
        object_fields(rig,('id','name','bones','source','rest_fingerprint','runtime_integrated'),label='Animation rig')
        add(rig['id']);text(rig.get('name'),'Rig name')
        for uid in validate_skeleton(rig['bones']):add(uid)
        hash_value(rig['rest_fingerprint'],'Rig rest fingerprint')
        if rig['rest_fingerprint']!=rig_rest_fingerprint(rig['bones']):raise ValueError('Rig bind matrix fingerprint differs')
        source_reference(rig.get('source'),verify_sources)
        if rig.get('runtime_integrated') is not False:raise ValueError('Editor rig cannot claim runtime integration')
        rigs[rig['id']]=rig
    for clip in data.get('clips',[]):
        object_fields(clip,('id','rig_id','name','role','source_clip_id','source','frame_start','frame_end','fps','looping','root_motion','additive_mode','events','channels_fingerprint','blender_verified','runtime_integrated'),
            ('slot_identifier','source_fps','native_clip','unmapped_native_tracks','unmapped_animated_tracks','verification','bone_map_id'),'Animation clip')
        add(clip['id']);text(clip.get('name'),'Clip name')
        text(clip['rig_id'],'Clip rig ID')
        if clip['source_clip_id'] is not None:text(clip['source_clip_id'],'Source clip ID')
        boolean(clip['looping'],'Clip looping');hash_value(clip['channels_fingerprint'],'Animation channel fingerprint')
        array(clip['events'],'Animation events')
        for key in ('slot_identifier','native_clip','bone_map_id'):
            if key in clip:text(clip[key],key)
        if 'source_fps' in clip:number(clip['source_fps'],'Original FPS',1e-6)
        for key in ('unmapped_native_tracks','unmapped_animated_tracks'):
            if key in clip:
                for name in array(clip[key],key):text(name,'Native track name')
        if clip.get('rig_id') not in rigs:raise ValueError('Clip rig reference does not exist')
        choice(clip['role'],{'source','edit','baked','retarget'},'animation clip role')
        choice(clip['root_motion'],{'in_place','object_delta','authored_object'},'root motion policy')
        choice(clip['additive_mode'],{'absolute','user_additive'},'additive policy')
        start=number(clip.get('frame_start'),'Clip start');end=number(clip.get('frame_end'),'Clip end')
        if end<start:raise ValueError('Animation duration is negative')
        if number(clip.get('fps'),'Clip FPS',1e-6)>1000:raise ValueError('Animation FPS exceeds 1000')
        if clip.get('runtime_integrated') is not False:raise ValueError('Editor clip cannot claim runtime integration')
        if type(clip.get('blender_verified')) is not bool:raise ValueError('Blender verification must be explicit')
        if clip['blender_verified']:
            proof=clip.get('verification')
            object_fields(proof,('clip_fingerprint','source'),label='Blender animation verification')
            if not isinstance(proof,dict) or proof.get('clip_fingerprint')!=clip.get('channels_fingerprint'):raise ValueError('Blender verification is stale or absent')
            reference=proof.get('source')
            if not isinstance(reference,dict) or reference.get('kind')!='blender_verification':raise ValueError('Blender verification requires an actual evidence file')
            source_reference(reference,verify_sources)
            if verify_sources:
                evidence=json.loads(Path(reference['path']).read_text(encoding='utf-8'))
                if (evidence.get('kind')!='blender_animation_check' or evidence.get('clip_id')!=clip['id']
                        or evidence.get('clip_fingerprint')!=clip['channels_fingerprint'] or evidence.get('runtime_integrated') is not False):
                    raise ValueError('Blender verification evidence belongs to another clip/version')
        source_reference(clip.get('source'),verify_sources)
        if not clip['blender_verified'] and 'verification' in clip:raise ValueError('Unverified clip must not retain stale proof metadata')
        for event in clip.get('events',[]):validate_event(event,start,end);add(event['id'])
        clips[clip['id']]=clip
    for clip in clips.values():
        source=clip.get('source_clip_id')
        if source is not None and source not in clips:raise ValueError('Source clip reference does not exist')
        if source==clip['id']:raise ValueError('Clip cannot derive from itself')
        visited={clip['id']}
        while source is not None:
            if source in visited:raise ValueError('Clip derivation contains a cycle')
            visited.add(source);source=clips[source].get('source_clip_id')
        direct=clips.get(clip.get('source_clip_id'))
        source_events={event['id'] for event in direct['events']} if direct else set()
        for event in clip['events']:
            if 'source_event_id' in event and event['source_event_id'] not in source_events:
                raise ValueError('Derived event does not reference an event of its source clip')
    for mapping in data.get('bone_maps',[]):
        validate_bone_map(mapping,rigs)
        add(mapping['id'])
    for attachment in data.get('attachments',[]):
        object_fields(attachment,('id','rig_id','bone_id','object_id','offset_matrix'),label='Bone attachment')
        add(attachment['id']);text(attachment['rig_id'],'Attachment rig ID');text(attachment['bone_id'],'Attachment bone ID');rig=rigs.get(attachment.get('rig_id'))
        if rig is None or attachment.get('bone_id') not in {b['id'] for b in rig['bones']}:raise ValueError('Attachment bone/rig does not exist')
        text(attachment.get('object_id'),'Attached object ID')
        matrix(attachment['offset_matrix'],'Attachment offset')
    nla_owners=set()
    for nla in data.get('nla',[]):
        object_fields(nla,('rig_id','enabled','tracks'),label='NLA authoring')
        text(nla['rig_id'],'NLA rig ID')
        if nla.get('rig_id') not in rigs or type(nla.get('enabled')) is not bool:raise ValueError('NLA rig/enable state is invalid')
        if nla['rig_id'] in nla_owners:raise ValueError('Duplicate NLA owner rig')
        nla_owners.add(nla['rig_id'])
        for track in array(nla['tracks'],'NLA tracks'):
            object_fields(track,('name','muted','solo','strips'),label='NLA track');text(track['name'],'NLA track name')
            boolean(track['muted'],'NLA track muted');boolean(track['solo'],'NLA track solo')
            for strip in array(track['strips'],'NLA strips'):
                object_fields(strip,('clip_id','name','frame_start','frame_end','action_frame_start','action_frame_end','repeat','scale','influence','blend_type','extrapolation','muted'),
                    ('reverse','animated_influence','animated_time','controlled_influence'),'NLA strip')
                text(strip['name'],'NLA strip name');boolean(strip['muted'],'NLA strip muted')
                text(strip['clip_id'],'NLA clip ID')
                for key in ('reverse','animated_influence','animated_time','controlled_influence'):
                    if key in strip:boolean(strip[key],key)
                if strip.get('controlled_influence') and not strip.get('animated_influence'):
                    raise ValueError('Constant controlled influence requires the actual Blender control flag')
                if strip.get('animated_influence') and not strip.get('controlled_influence') or strip.get('animated_time'):
                    raise ValueError('Animated NLA control curves require an explicit visual bake before manifest export')
                clip=clips.get(strip.get('clip_id'))
                if clip is None:raise ValueError('NLA strip references an absent clip')
                if clip['rig_id']!=nla['rig_id']:raise ValueError('NLA clip belongs to another registered rig')
                for key in ('frame_start','frame_end','action_frame_start','action_frame_end','influence'):number(strip.get(key),'NLA '+key)
                if strip['frame_end']<strip['frame_start'] or strip['action_frame_end']<strip['action_frame_start']:raise ValueError('NLA interval is reversed')
                for key in ('repeat','scale'):number(strip.get(key),'NLA '+key,1e-6)
                if not 0<=strip['influence']<=1:raise ValueError('NLA influence outside 0..1')
                choice(strip['blend_type'],{'REPLACE','ADD','SUBTRACT','MULTIPLY','COMBINE'},'NLA blending policy')
                choice(strip['extrapolation'],{'NOTHING','HOLD','HOLD_FORWARD'},'NLA extrapolation')
    maps={mapping['id']:mapping for mapping in data['bone_maps']}
    for clip in clips.values():
        if 'bone_map_id' in clip and clip['bone_map_id'] not in maps:raise ValueError('Retarget clip bone map reference is missing')
        if clip['role']=='retarget' and 'bone_map_id' not in clip:raise ValueError('Retarget clip requires its reviewed bone map')
        if 'bone_map_id' in clip and maps[clip['bone_map_id']]['target_rig_id']!=clip['rig_id']:
            raise ValueError('Retarget clip does not belong to the bone-map target rig')
    exported_objects=set()
    for exported in array(data.get('exports',[]),'Animation exports'):
        validate_export_record(exported,rigs,clips,verify_sources);add(exported['id']);exported_objects.update(exported['object_ids'])
    attached={attachment['object_id'] for attachment in data['attachments']}
    if not exported_objects.issubset(attached):raise ValueError('Animation export claims an object that has no authored attachment')
    return {'rigs':len(rigs),'clips':len(clips),'events':sum(len(c.get('events',[])) for c in clips.values()),'ids':ids}


def validate_export_record(record,rigs,clips,verify_sources=True):
    object_fields(record,('id','rig_id','clip_id','channels_fingerprint','source','object_ids'),label='Animation export binding')
    for key in ('id','rig_id','clip_id'):text(record[key],key)
    hash_value(record['channels_fingerprint'],'Exported animation fingerprint')
    clip=clips.get(record['clip_id'])
    if record['rig_id'] not in rigs or clip is None or clip['rig_id']!=record['rig_id']:raise ValueError('Animation export references an absent or different rig/clip')
    if record['channels_fingerprint']!=clip['channels_fingerprint']:raise ValueError('Animation export is stale; export the current edited clip as a new version')
    object_ids=array(record['object_ids'],'Exported attachment IDs')
    for uid in object_ids:text(uid,'Exported attachment ID')
    if len(object_ids)!=len(set(object_ids)):raise ValueError('Animation export repeats an attachment ID')
    source=record['source'];source_reference(source,verify_sources)
    if source is None or source['kind']!='blender_export':raise ValueError('Animation export requires an actual Blender export manifest')
    if not verify_sources:return
    path=Path(source['path']);manifest=json.loads(path.read_text(encoding='utf-8'))
    if (manifest.get('kind')!='wz09_animation_export' or type(manifest.get('version')) is not int or manifest['version']!=1
            or manifest.get('runtime_integrated') is not False or manifest.get('game_started') is not False):raise ValueError('Expected an editor-only animation export manifest')
    if manifest.get('selected_rig_id')!=record['rig_id'] or manifest.get('selected_clip_id')!=record['clip_id']:raise ValueError('Export manifest belongs to another selected rig/clip')
    if manifest.get('export_id')!=record['id']:raise ValueError('Animation export manifest ID differs')
    snapshots=manifest.get('animation_studio',{}).get('clips',[])
    matches=[item for item in snapshots if item.get('id')==clip['id']]
    if len(matches)!=1 or matches[0].get('channels_fingerprint')!=record['channels_fingerprint']:raise ValueError('Manifest does not contain the exported clip fingerprint')
    if clip_export_fingerprint(matches[0])!=clip_export_fingerprint(clip):raise ValueError('Animation timing, root policy or events changed after this export')
    declared=manifest.get('selected_attachment_object_ids')
    if not isinstance(declared,list) or sorted(declared)!=sorted(object_ids):raise ValueError('Export attachment inventory differs from its binding')
    files=object_fields(manifest.get('files'),('model_animation','editable_blend','events'),label='Animation product files')
    resolved={}
    for key,value in files.items():
        object_fields(value,('path','sha256'),label='Animation product file');hash_value(value['sha256'],'Animation product file SHA')
        relative=Path(text(value['path'],'Animation product filename'))
        target=(path.parent/relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(path.parent.resolve()) or not target.is_file() or file_sha256(target)!=value['sha256']:
            raise ValueError('Animation product missing, changed or escaped: '+str(target))
        resolved[key]=target
    if resolved['model_animation'].suffix.lower()!='.glb' or resolved['editable_blend'].suffix.lower()!='.blend' or resolved['events'].suffix.lower()!='.json':raise ValueError('Animation export requires actual GLB, Blend and JSON products')
    event_data=json.loads(resolved['events'].read_text(encoding='utf-8'))
    if (event_data.get('clip_id')!=clip['id'] or event_data.get('fps')!=clip['fps'] or event_data.get('frame_start')!=clip['frame_start']
            or event_data.get('events')!=clip['events'] or event_data.get('runtime_integrated') is not False):raise ValueError('Exported event product differs from its clip')
    try:from .source_bundle import verify_preview_bundle
    except ImportError:from source_bundle import verify_preview_bundle
    bundle=manifest.get('model_animation_bundle',{})
    if Path(bundle.get('reference_path','')).resolve()!=resolved['model_animation']:raise ValueError('Animation bundle is not the exported model file')
    document=verify_preview_bundle(bundle)
    if not document.get('skins') or not document.get('animations'):raise ValueError('Animation product lacks its actual skin/clip')
    actual=set()
    for node in document.get('nodes',[]):
        extras=node.get('extras',{})
        for key in ('wz09_animation_object_id','wz09_asset_placement_id'):
            if key in extras and isinstance(extras[key],str):actual.add(extras[key])
    if not set(object_ids).issubset(actual):raise ValueError('Animation GLB does not contain its declared attachment object IDs')


def clip_export_fingerprint(clip):
    return fingerprint({key:clip[key] for key in ('frame_start','frame_end','fps','looping','root_motion','additive_mode','events')})


def validate_bone_map(mapping,rigs):
    object_fields(mapping,('id','source_rig_id','target_rig_id','mode','reviewed','translation_scale','root_motion','entries','source_rest_fingerprint','target_rest_fingerprint'),label='Bone map')
    for key in ('id','source_rig_id','target_rig_id'):text(mapping[key],key)
    for key in ('source_rest_fingerprint','target_rest_fingerprint'):hash_value(mapping[key],key)
    source=rigs.get(mapping.get('source_rig_id'));target=rigs.get(mapping.get('target_rig_id'))
    if source is None or target is None or source['id']==target['id']:raise ValueError('Retarget requires two distinct registered rigs')
    if mapping.get('mode')!='rest_relative' or mapping.get('reviewed') is not True:raise ValueError('Retarget requires an explicitly reviewed rest-relative mapping')
    choice(mapping['root_motion'],{'ignore','copy_object'},'retarget root motion policy')
    if mapping.get('source_rest_fingerprint')!=source.get('rest_fingerprint') or mapping.get('target_rest_fingerprint')!=target.get('rest_fingerprint'):
        raise ValueError('Reviewed bone map bind pose is stale')
    number(mapping.get('translation_scale'),'Retarget translation scale',1e-6)
    source_ids={b['id'] for b in source['bones']};target_ids={b['id'] for b in target['bones']}
    used_source=set();used_target=set();entries=mapping.get('entries')
    if not isinstance(entries,list) or not entries:raise ValueError('Bone mapping is empty')
    for entry in entries:
        object_fields(entry,('source_bone_id','target_bone_id'),('source_name','target_name'),'Bone mapping pair')
        left,right=entry.get('source_bone_id'),entry.get('target_bone_id')
        text(left,'Source bone ID');text(right,'Target bone ID')
        if left not in source_ids or right not in target_ids:raise ValueError('Bone mapping references a missing source/target bone')
        if left in used_source or right in used_target:raise ValueError('Bone mapping must be one-to-one')
        used_source.add(left);used_target.add(right)
        for key,uid,bones in (('source_name',left,source['bones']),('target_name',right,target['bones'])):
            if key in entry and entry[key]!=next(b['name'] for b in bones if b['id']==uid):raise ValueError('Bone-map display name differs from its stable Bone ID')
    return entries


def new_clip(rig_id,name,frame_start,frame_end,fps,**values):
    clip={'id':identifier(),'rig_id':rig_id,'name':name,'role':'edit','source_clip_id':None,
        'source':None,'frame_start':frame_start,'frame_end':frame_end,'fps':fps,'looping':False,
        'root_motion':'in_place','additive_mode':'absolute','events':[],
        'channels_fingerprint':fingerprint([]),'blender_verified':False,'runtime_integrated':False}
    clip.update(values);return clip


def copied_clip(record,name=None,rig_id=None,role='edit'):
    result=copy.deepcopy(record);result.update(id=identifier(),name=name or record['name']+' · Edit',
        rig_id=rig_id or record['rig_id'],source_clip_id=record['id'],role=role,blender_verified=False)
    result.pop('verification',None)
    for event in result['events']:
        event['source_event_id']=event['id'];event['id']=identifier()
    return result


def retime_events(events,old_start,new_start,old_fps,new_fps):
    number(old_fps,'Old FPS',1e-6);number(new_fps,'New FPS',1e-6)
    result=copy.deepcopy(events)
    for event in result:event['frame']=new_start+(event['frame']-old_start)*new_fps/old_fps
    return result


def validate_capture_data(data):
    """Validate source metadata without importing Blender or accepting guessed rigs."""
    object_fields(data,('kind','version','model','bones','clip','scene','source_path','surfaces','mesh_triangles','materials','images',
        'coordinate_system','source_units_to_editor_unit','physical_scale_verified','runtime_integrated','game_started','game_workspace_written'),label='Native animation capture')
    if data['kind']!='wz09_native_animation_capture' or type(data['version']) is not int or data['version']!=1:raise ValueError('Unsupported native rig/clip capture')
    for key in ('physical_scale_verified','runtime_integrated','game_started','game_workspace_written'):
        if data[key] is not False:raise ValueError('Native rig cannot grant runtime/physical verification')
    text(data['model'],'Native model');text(data['source_path'],'Native source path')
    if data['coordinate_system']!='gltf_y_up_export_convention' or data['scene']!='scene.gltf':raise ValueError('Unknown native rig export convention')
    number(data['source_units_to_editor_unit'],'Explicit native coordinate factor',1e-12)
    for key in ('mesh_triangles','materials','images'):integer(data[key],key)
    for index in array(data['surfaces'],'Native surfaces'):integer(index,'Native surface ordinal')
    if not data['mesh_triangles']:raise ValueError('Native rig capture has no actual mesh')
    bones=array(data['bones'],'Native bones');names=set()
    if not bones:raise ValueError('Native rig is empty')
    for index,bone in enumerate(bones):
        object_fields(bone,('index','name','parent','bind_local_native','bind_world_native'),label='Native bone')
        name=text(bone['name'],'Native bone name')
        if type(bone['index']) is not int or bone['index']!=index or name in names:raise ValueError('Native bone ordering/names are invalid')
        names.add(name);parent=bone['parent']
        if parent is not None and (type(parent) is not int or not 0<=parent<index):raise ValueError('Native bone parent is not ordered')
        for key in ('bind_local_native','bind_world_native'):
            values=array(bone[key],key)
            if len(values)!=16:raise ValueError('Native bind matrix requires 16 column-major values')
            matrix([values[row+4*column] for row in range(4) for column in range(4)],'Native '+key)
    clip=data['clip']
    object_fields(clip,('name','fps','native_frames','duration_s','looping','tracks','matched_tracks','animated_tracks','unmapped_tracks',
        'unmapped_animated_tracks','events','additive_semantics','root_delta_samples','root_delta_applied','pose_reference'),label='Native original clip')
    text(clip['name'],'Native clip name');number(clip['fps'],'Native FPS',1e-6);integer(clip['native_frames'],'Native frame count',1)
    number(clip['duration_s'],'Native duration',1e-6);boolean(clip['looping'],'Native looping')
    if abs(clip['duration_s']-clip['native_frames']/clip['fps'])>1e-4:raise ValueError('Native clip timing is inconsistent')
    for key in ('tracks','matched_tracks','animated_tracks'):integer(clip[key],key)
    if clip['matched_tracks']>clip['tracks'] or clip['animated_tracks']>clip['tracks']:raise ValueError('Native track counts are inconsistent')
    for key in ('unmapped_tracks','unmapped_animated_tracks'):
        items=array(clip[key],key)
        for name in items:text(name,'Native unmapped track name')
        if len(items)!=len(set(items)):raise ValueError('Duplicate native track names')
    if clip['tracks']-clip['matched_tracks']!=len(clip['unmapped_tracks']):raise ValueError('Native unmapped track count differs')
    if not set(clip['unmapped_animated_tracks']).issubset(clip['unmapped_tracks']):raise ValueError('Unmapped animated track lacks source track')
    if clip['additive_semantics']!='not_inferred' or clip['root_delta_applied'] is not False:raise ValueError('Native clip must preserve unguessed additive/root semantics')
    for event in array(clip['events'],'Native events'):
        object_fields(event,('name','normalized_time','time_s'),label='Native notify');text(event['name'],'Native notify name')
        normalized=number(event['normalized_time'],'Native normalized time');seconds=number(event['time_s'],'Native event seconds')
        if not 0<=normalized<=1 or abs(seconds-normalized*clip['duration_s'])>1e-4:raise ValueError('Native event timing differs')
    for key,vector_key in (('root_delta_samples','delta_native'),('pose_reference','bone_heads_editor')):
        samples=array(clip[key],key)
        if not samples:raise ValueError('Native pose/root reference is empty')
        previous=-1.
        for sample in samples:
            object_fields(sample,('time_s',vector_key),label='Native pose/root sample')
            seconds=number(sample['time_s'],'Native sample time')
            if seconds<previous or not 0<=seconds<=clip['duration_s']+1e-5:raise ValueError('Native samples are outside their ordered clip')
            previous=seconds;vectors=sample[vector_key] if vector_key=='bone_heads_editor' else [sample[vector_key]]
            if vector_key=='bone_heads_editor' and len(array(vectors,'Native bone heads'))!=len(bones):raise ValueError('Native pose reference loses bones')
            for vector in vectors:
                if len(array(vector,'Native XYZ'))!=3:raise ValueError('Native XYZ requires three finite values')
                for item in vector:number(item,'Native XYZ')
    return data


def read_capture(path,verify_sources=True):
    try:from .source_bundle import verify_preview_bundle
    except ImportError:from source_bundle import verify_preview_bundle
    path=Path(path).resolve();report=json.loads(path.read_text(encoding='utf-8'))
    if (report.get('kind')!='offline_native_animation_export' or report.get('status')!='PASS'
            or type(report.get('version')) is not int or report.get('version')!=1):raise ValueError('Expected a passing native animation export report')
    if any(report.get(key) is not False for key in ('game_started','game_workspace_written','runtime_integrated','physical_scale_verified')):
        raise ValueError('Native animation capture cannot imply runtime or physical verification')
    capture=Path(report['capture_path']).resolve()
    if not capture.is_relative_to(path.parent) or file_sha256(capture)!=report['capture_sha256']:raise ValueError('Native animation capture missing/changed/escaped')
    sources=report.get('source_files')
    if not isinstance(sources,list) or len(sources)<2:raise ValueError('Native rig capture requires map and common FF provenance')
    for source in sources:
        object_fields(source,('path','sha256'),label='Native FF source');hash_value(source['sha256'],'Native FF hash')
        if not Path(text(source['path'],'Native FF path')).is_absolute():raise ValueError('Native FF source path must be absolute')
        if verify_sources and file_sha256(source['path'])!=source['sha256']:raise ValueError('Native rig/animation FF source changed')
    document=verify_preview_bundle(report['preview_bundle'])
    data=validate_capture_data(json.loads(capture.read_text(encoding='utf-8')));bones=data['bones'];clip=data['clip']
    skins=document.get('skins',[]);animations=document.get('animations',[])
    if len(skins)!=1 or len(animations)!=1 or len(skins[0].get('joints',[]))!=len(bones):raise ValueError('Native glTF loses its actual skin/clip')
    for bone,node in zip(bones,skins[0]['joints']):
        if document['nodes'][node].get('name')!=bone['name']:raise ValueError('Native rig/glTF bone identity differs')
    if animations[0].get('name')!=clip.get('name'):raise ValueError('Native original clip identity differs')
    return report,data
