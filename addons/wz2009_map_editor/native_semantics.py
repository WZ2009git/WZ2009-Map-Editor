"""Bind real offline FF semantic records to existing audio/action authoring.

No native coordinates receive a default conversion. The caller must provide a
registered transform or explicitly declare the exporter convention. Aliases and
native type fields are evidence; their payloads/behaviors remain runtime gated.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

try:
    from . import audio_zones, action_elements
    from .authoring_helpers import resolve_owner_map
except ImportError:
    import audio_zones, action_elements
    from authoring_helpers import resolve_owner_map

_CLASSES = []
_ALIAS_ITEMS = [('NONE','First import native metadata','')]
_ALIAS_CACHE = {}


def file_io_path(path):
    """Use Win32 extended paths only at the read/write boundary.

    Canonical manifest paths and containment checks retain ordinary absolute
    paths. Blender's Windows process may not opt in to long ordinary paths,
    even when the system Python can open the same captured payload.
    """
    value = os.fspath(path)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    absolute = os.path.abspath(value)
    if len(absolute) < 248:
        return value
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absolute[2:]
    return '\\\\?\\' + absolute


def file_sha256(path):
    h = hashlib.sha256()
    with open(file_io_path(path), 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()


def read_semantics(path, *, verify_sources=True):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 64*1024*1024:
        raise ValueError('Native semantics file is missing or exceeds 64 MiB')
    data = json.loads(path.read_text(encoding='utf-8'),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite semantic JSON')))
    if (not isinstance(data, dict) or data.get('kind') != 'wz09_native_semantics'
            or data.get('version') != 1 or data.get('coordinate_system') != 'iw4_native_xyz'
            or data.get('runtime_integrated') is not False or data.get('physical_scale_verified') is not False):
        raise ValueError('Expected actual wz09_native_semantics version 1 with raw native coordinates and runtime gates')
    if not isinstance(data.get('assets_walked'), int) or data['assets_walked'] <= 0:
        raise ValueError('Native FF walk is empty; no fallback dump accepted')
    if not isinstance(data.get('source_map'), str) or not data['source_map'].startswith('mp_'):
        raise ValueError('Native source map is missing')
    for field in ('script_models', 'script_brush_models', 'triggers', 'dyn_ents'):
        if not isinstance(data.get(field), list): raise ValueError('Native semantics missing '+field)
    if (not isinstance(data.get('audio'), dict)
            or not isinstance(data['audio'].get('createfx_loops'), list)
            or not isinstance(data['audio'].get('aliases'), list)):
        raise ValueError('Native audio capture is missing')
    if verify_sources:
        source = Path(data.get('source_path', ''))
        if (not source.is_absolute() or not re.fullmatch('[0-9a-f]{64}', str(data.get('source_sha256', '')))
                or not source.is_file() or file_sha256(source) != data['source_sha256']):
            raise ValueError('Original FF source SHA-256 differs or source is missing')
        for record in data.get('source_modules', []):
            source = Path(record.get('source_path', ''))
            if not source.is_absolute() or not source.is_file() or file_sha256(source) != record.get('source_sha256'):
                raise ValueError('Native parser source changed: '+str(source))
        for record in data['audio'].get('selected_rawfiles', []):
            name = record.get('native_path', '')
            if not isinstance(name, str): raise ValueError('Native rawfile name is invalid')
            exported = path.parent/name.replace('/', '__').replace('\\', '__')
            if (not exported.is_file() or file_sha256(exported) != record.get('sha256')):
                raise ValueError('Extracted native rawfile SHA-256 differs: '+name)
    return data


def validate_transform(transform):
    if not isinstance(transform, dict) or set(transform) != {'source_units_to_editor_unit', 'axis_mapping', 'offset_m', 'basis'}:
        raise ValueError('Explicit source transform requires factor, axes, offset and basis')
    scale = audio_zones._number(transform['source_units_to_editor_unit'], 'Native factor', 1e-12)
    axes = transform['axis_mapping']
    if (not isinstance(axes, (tuple, list)) or len(axes) != 3
            or any(not isinstance(v, str) or not re.fullmatch('[+-]?[xyz]', v) for v in axes)
            or {v[-1] for v in axes} != set('xyz')):
        raise ValueError('Explicit signed source axes required')
    if transform['basis'] not in {'verified_registration', 'explicit_export_convention'}:
        raise ValueError('Transform basis must be registered or explicitly declared export convention')
    return {'source_units_to_editor_unit': scale, 'axis_mapping': list(axes),
            'offset_m': audio_zones._vector(transform['offset_m'], 'Native offset'), 'basis': transform['basis']}


def transform_from_calibration(calibration, *, source_sha256=None):
    try:
        from .calibration import validate_calibration, verify_calibration_source
    except ImportError:
        from calibration import validate_calibration, verify_calibration_source
    calibration = validate_calibration(calibration)
    verify_calibration_source(calibration)
    registration = calibration.get('registration')
    if source_sha256 is not None and (not registration or registration['native_source_sha256'] != source_sha256):
        raise ValueError('Map registration does not bind this exact original FF source')
    return validate_transform({'source_units_to_editor_unit': calibration['source_units_to_meter'],
        'axis_mapping': calibration['axis_mapping'], 'offset_m': calibration['offset_m'], 'basis': 'verified_registration'})


def _matrix(rotation):
    x, y, z = [math.radians(v) for v in audio_zones._vector(rotation, 'Map rotation')]
    cx,sx,cy,sy,cz,sz=math.cos(x),math.sin(x),math.cos(y),math.sin(y),math.cos(z),math.sin(z)
    return [[cz*cy,cz*sy*sx-sz*cx,cz*sy*cx+sz*sx],
            [sz*cy,sz*sy*sx+cz*cx,sz*sy*cx-cz*sx],[-sy,cy*sx,cy*cx]]


def _multiply(a, b):
    return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _euler(matrix):
    y = math.asin(max(-1, min(1, -matrix[2][0])))
    # Normalized float32 source quaternions can put asin within ~2e-8 rad
    # of a pole. Treat that numerically ambiguous direction as gimbal lock.
    if abs(math.cos(y)) > 1e-7:
        x,z=math.atan2(matrix[2][1],matrix[2][2]),math.atan2(matrix[1][0],matrix[0][0])
    else:
        x,z=math.atan2(-matrix[1][2],matrix[1][1]),0
    return [math.degrees(v) for v in (x,y,z)]


def _position(point, transform, map_position, map_rotation):
    point=audio_zones._vector(point,'Native origin')
    local=[point['xyz'.index(axis[-1])]*(-1 if axis.startswith('-') else 1)*transform['source_units_to_editor_unit']+offset
           for axis,offset in zip(transform['axis_mapping'],transform['offset_m'])]
    matrix=_matrix(map_rotation)
    return [sum(matrix[i][j]*local[j] for j in range(3))+map_position[i] for i in range(3)]


def _rotation(native_angles, transform, map_rotation, quaternion=None):
    # Native ScriptModel convention is Rz(yaw)*Ry(pitch)*Rx(roll), reused from
    # assets/src/lane/helpers.rs. The signed basis conjugates this rotation.
    if quaternion is None:
        pitch,yaw,roll=audio_zones._vector(native_angles,'Native pitch/yaw/roll')
        native=_matrix([roll,pitch,yaw])
    else:
        if not isinstance(quaternion,(list,tuple)) or len(quaternion)!=4:
            raise ValueError('Native quaternion requires four values')
        q=[audio_zones._number(v,'Native quaternion') for v in quaternion]
        length=math.sqrt(sum(v*v for v in q))
        if length <= 1e-9: raise ValueError('Native quaternion is zero')
        x,y,z,w=[v/length for v in q]
        native=[[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]]
    basis=[[(-1 if axis.startswith('-') else 1) if 'xyz'.index(axis[-1])==j else 0 for j in range(3)] for axis in transform['axis_mapping']]
    inverse=[list(row) for row in zip(*basis)]
    return _euler(_multiply(_matrix(map_rotation),_multiply(_multiply(basis,native),inverse)))


def prepare_records(data, semantics_path, *, map_instance_id, transform,
                    map_position=(0,0,0), map_rotation=(0,0,0), ambient_bounds_m=None):
    """Convert only actual captured records. Bounds for ambientPlay are authored.

    Missing native distance metadata receives an explicitly documented design
    radius; no native volume/collision is invented. Action visuals remain
    unbound dependencies until a real authored placement is explicitly linked.
    """
    transform=validate_transform(transform)
    audio_zones._text(map_instance_id,'Map instance ID')
    map_position=audio_zones._vector(map_position,'Map position')
    map_rotation=audio_zones._vector(map_rotation,'Map rotation')
    path=Path(semantics_path).resolve();digest=file_sha256(path)
    if json.loads(path.read_text(encoding='utf-8')) != data:
        raise ValueError('Prepared semantic records differ from the checksum-bound metadata file')
    evidence={'status':'source_bound','source_path':str(path),'source_sha256':digest,'source_record':''}
    factor=transform['source_units_to_editor_unit']
    native_sha=data['source_sha256']
    aliases={r['name']:r for r in data['audio']['aliases']}
    zones,actions,notes=[],[],[]
    def identity(kind,ordinal):
        return str(uuid.uuid5(uuid.NAMESPACE_URL,f'wz09:semantic:{native_sha}:{kind}:{ordinal}:{map_instance_id}'))
    def bound(pointer,extra=''):
        return dict(evidence,source_record=pointer+'; transform='+audio_zones._encoded(transform)+extra)
    for index,loop in enumerate(data['audio']['createfx_loops']):
        alias=audio_zones._text(loop.get('alias_ref'),'Native alias')
        record=audio_zones.new_record('ambient','sphere',position=_position(loop['origin_source'],transform,map_position,map_rotation),map_instance_id=map_instance_id,alias_ref=alias)
        ordinal=loop.get('source_ordinal',index)
        record['id']=identity('createfx_loop',ordinal)
        variants=aliases.get(alias,{}).get('variants',[])
        distances=[(v.get('distance_min_source'),v.get('distance_max_source')) for v in variants]
        distances=[(a,b) for a,b in distances if type(a) in (int,float) and type(b) in (int,float) and math.isfinite(a) and math.isfinite(b) and 0<=a<b]
        if distances:
            inner=min(a for a,b in distances)*factor;outer=max(b for a,b in distances)*factor
            record['radius_m']=outer;record['inner_radius_m']=min(inner,outer)
            record['fade_m']=max(0,outer-inner)
            extra='; range=native_alias_variants; falloff=editor_smoothstep_not_native_curve'
        else:
            extra='; range=explicit_editor_design_default_20; native_range_unresolved'
            notes.append(f'{alias}: native range unresolved; 20-unit design radius')
        record['evidence']=bound(f'audio.createfx_loops[{index}]',extra)
        zones.append(record)
    ambient=data['audio'].get('ambient_alias')
    if ambient and ambient_bounds_m is not None:
        if (not isinstance(ambient_bounds_m,(list,tuple)) or len(ambient_bounds_m)!=6
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in ambient_bounds_m)
                or any(ambient_bounds_m[i]>=ambient_bounds_m[i+3] for i in range(3))):
            raise ValueError('Ambient design bounds require positive world XYZ extents')
        record=audio_zones.new_record('ambient','box',position=[(ambient_bounds_m[i]+ambient_bounds_m[i+3])/2 for i in range(3)],map_instance_id=map_instance_id,alias_ref=ambient)
        record['dimensions_m']=[ambient_bounds_m[i+3]-ambient_bounds_m[i] for i in range(3)]
        record['id']=identity('ambient_alias',0)
        record['evidence']=bound('audio.ambient_alias','; volume=explicit_editor_design_bounds_not_native_zone')
        zones.append(record)
    elif ambient:
        notes.append('Native ambient alias captured; no spatial zone inferred without explicit design bounds: '+ambient)
    def add_action(source,pointer,kind,profile,ordinal,*,radius=None,quaternion=None,dependencies=()):
        r=action_elements.new_record(kind,position=_position(source['origin_source'],transform,map_position,map_rotation),map_instance_id=map_instance_id)
        r['id']=identity(pointer.split('[')[0],ordinal)
        r['rotation_deg']=_rotation(source.get('angles_deg',[0,0,0]),transform,map_rotation,quaternion)
        r['behavior_ref']=profile
        r['dependencies']=sorted(set(dependencies))
        r['missing_dependencies']=list(r['dependencies'])  # No visual/collider/payload binding was performed.
        extra='; visual_collision_dependencies=unbound'
        if radius is not None and type(radius) in (int,float) and math.isfinite(radius) and radius>0:
            r['radius_m']=radius*factor
            extra+='; radius=native'
        else:
            extra+='; radius=explicit_editor_design_default_2.5'
        health=source.get('health')
        if type(health) in (int,float) and math.isfinite(health) and health>0:r['health']=health
        r['evidence']=bound(pointer,extra)
        actions.append(r)
    for index,s in enumerate(data['script_models']):
        ordinal=s['source_ordinal'];deps=['visual_binding:script_model:'+str(ordinal)]
        if s.get('model'):deps.append('xmodel:'+s['model'])
        if s.get('destructible_type') or s.get('destructible_def') or s.get('native_prop_machine'):
            profile='iw4:destructible/'+(s.get('destructible_type') or s.get('destructible_def') or s['native_prop_machine'])
            add_action(s,f'script_models[{index}]','destructible',profile,ordinal,dependencies=deps)
        elif s.get('script_exploder'):
            # An exploder label proves script linkage, not shootable car logic.
            deps.append('script_exploder:'+s['script_exploder'])
            add_action(s,f'script_models[{index}]','trigger','iw4:script_exploder/'+s['script_exploder'],ordinal,dependencies=deps)
    for index,s in enumerate(data['script_brush_models']):
        if s.get('script_exploder') or s.get('script_fxid'):
            deps=['cmodel:'+str(s['cmodel_handle'])]
            if s.get('script_fxid'):deps.append('fx:'+s['script_fxid'])
            add_action(s,f'script_brush_models[{index}]','trigger','iw4:script_brush_model/'+str(s['source_ordinal']),s['source_ordinal'],dependencies=deps)
    for index,s in enumerate(data['triggers']):
        deps=['native_trigger_volume:'+str(s['source_ordinal'])]
        if s.get('model'):deps.append('cmodel:'+s['model'])
        add_action(s,f'triggers[{index}]','trigger','iw4:'+s['classname'],s['source_ordinal'],radius=s.get('radius_source'),dependencies=deps)
    for index,s in enumerate(data['dyn_ents']):
        if s.get('native_type')!=2:continue
        ordinal=str(s['draw_type'])+':'+str(s['source_index'])
        deps=['visual_binding:dyn_ent:'+ordinal]
        if s.get('model'):deps.append('xmodel:'+s['model'])
        if s.get('destroy_fx'):deps.append('fx:'+s['destroy_fx'])
        if s.get('physics_preset'):deps.append('phys_preset:'+s['physics_preset']['name'])
        add_action(s,f'dyn_ents[{index}]','destructible','iw4:dyn_ent/destruct',ordinal,quaternion=s['quaternion_xyzw'],dependencies=deps)
    return {'audio_zones':audio_zones.validate_records(zones,{map_instance_id},verify_sources=True),
            'action_elements':action_elements.validate_records(actions,{map_instance_id},verify_sources=True),
            'notes':notes,'transform':transform,'source_semantics_path':str(path),'source_semantics_sha256':digest,
            'physical_scale_verified':False,'runtime_integrated':False}


def semantic_binding_key(record, *, owner_map=None):
    """Identify an exact native-source record independently of copied UUIDs.

    This deliberately does not merge separately extracted metadata hashes or
    unrelated authored aliases. A copied map can refresh its inventory while
    retaining the already copied, newly identified source objects.
    """
    if not isinstance(record,dict):return None
    evidence=record.get('evidence')
    if not isinstance(evidence,dict) or evidence.get('status')!='source_bound':return None
    digest=evidence.get('source_sha256','')
    pointer=evidence.get('source_record','')
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or not isinstance(pointer,str):return None
    pointer=pointer.partition(';')[0].strip()
    if not re.fullmatch(r'(audio\.createfx_loops|script_models|script_brush_models|triggers|dyn_ents)\[\d+\]|audio\.ambient_alias',pointer):
        return None
    owner=owner_map if owner_map is not None else record.get('map_instance_id')
    if not isinstance(owner,str) or not owner.strip():return None
    return owner,digest,pointer


def import_into_scene(context,path,*,transform,import_audio=True,import_actions=True,ambient_bounds_m=None):
    import bpy
    owner=resolve_owner_map(context.active_object)
    if owner is None:raise ValueError('Choose the destination map or one of its objects')
    data=read_semantics(path)
    if owner.get('wz09_source_map')!=data['source_map']:
        raise ValueError('Native source map does not match selected map instance')
    context.view_layer.update()
    prepared=prepare_records(data,path,map_instance_id=str(owner['wz09_id']),transform=transform,
        map_position=list(owner.matrix_world.translation),
        map_rotation=[math.degrees(v) for v in owner.matrix_world.to_euler('XYZ')],ambient_bounds_m=ambient_bounds_m)
    audio_ids={o.get('wz09_audio_id') for o in context.scene.objects if o.get('wz09_audio_zone')}
    action_ids={o.get('wz09_action_id') for o in context.scene.objects if o.get('wz09_action_element')}
    source_bindings=set()
    for obj in context.scene.objects:
        field='wz09_audio_json' if obj.get('wz09_audio_zone') else ('wz09_action_json' if obj.get('wz09_action_element') else None)
        if field is None or resolve_owner_map(obj)!=owner:continue
        try:previous=json.loads(obj.get(field,''))
        except (TypeError,ValueError) as exc:
            raise ValueError('Existing native audio/action record is invalid; fix it before importing') from exc
        key=semantic_binding_key(previous,owner_map=str(owner['wz09_id']))
        if key is not None:source_bindings.add(key)
    created=[];counts={'audio_created':0,'actions_created':0,'existing_skipped':0}
    try:
        for field,enabled,ids,create in [('audio_zones',import_audio,audio_ids,audio_zones.create_zone),
                                        ('action_elements',import_actions,action_ids,action_elements.create_action)]:
            if not enabled:continue
            for r in prepared[field]:
                key=semantic_binding_key(r)
                if r['id'] in ids or key in source_bindings:
                    counts['existing_skipped']+=1;continue
                kwargs={'_existing_ids':ids,'_source_verified':True}
                kwargs['owner' if field=='audio_zones' else '_owner']=owner
                item=create(context,r,**kwargs);created.append(item)
                if field=='action_elements':
                    model=next((v[len('xmodel:'):] for v in r['dependencies'] if v.startswith('xmodel:')),None)
                    item.name='Aktion · '+(model or r['behavior_ref'] or r['kind'])
                if key is not None:source_bindings.add(key)
                counts['audio_created' if field=='audio_zones' else 'actions_created']+=1
        context.view_layer.update()
        inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}'))
        inventory[str(owner['wz09_id'])]={'source_map':data['source_map'],'path':str(Path(path).resolve()),
            'sha256':prepared['source_semantics_sha256'],'original_ff_sha256':data['source_sha256'],
            'ambient_alias':data['audio'].get('ambient_alias'),
            'aliases':sorted({r['name'] for r in data['audio']['aliases']}),'notes':prepared['notes'],
            'runtime_integrated':False}
        context.scene['wz09_native_semantics_index_json']=audio_zones._encoded(inventory)
        return dict(counts,notes=prepared['notes'],aliases=len(data['audio']['aliases']))
    except Exception:
        for root in reversed(created):
            for child in list(root.children):
                mesh=child.data
                bpy.data.objects.remove(child,do_unlink=True)
                if mesh and mesh.users==0:bpy.data.meshes.remove(mesh)
            bpy.data.objects.remove(root,do_unlink=True)
        raise


def _aliases(settings,context):
    owner=resolve_owner_map(context.active_object) if context else None
    inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}')) if context else {}
    source=inventory.get(str(owner['wz09_id']),{}) if owner is not None else {}
    if not source.get('aliases'):return _ALIAS_ITEMS
    key=(str(owner['wz09_id']),source.get('sha256',''))
    if key not in _ALIAS_CACHE:
        # Blender keeps references to dynamic Enum strings; retain old keyed
        # lists as well as the active one until the module is unregistered.
        _ALIAS_CACHE[key]=[(alias,alias,'Captured alias; payload and runtime untested') for alias in source['aliases']]
    return _ALIAS_CACHE[key]


def draw_captured_aliases(layout,context):
    owner=resolve_owner_map(context.active_object)
    inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}'))
    source=inventory.get(str(owner['wz09_id']),{}) if owner is not None else {}
    if not source:return
    box=layout.box();box.label(text=f"{len(source['aliases'])} originale Sound-Aliase",icon='SOUND')
    box.prop(context.scene.wz09_native_settings,'alias_choice',text='Alias')
    box.operator('wz09.native_alias_choose',text='Apply alias',icon='CHECKMARK')


def prepared_source_for_map(scene, source_map):
    """Find only versioned editor semantic batch manifests, never scan the game."""
    root=Path(getattr(scene,'wz09_project_root','')).expanduser().resolve()
    if not (root/'AGENTS.md').is_file():raise ValueError('Set the standalone editor project root first')
    reports=sorted((root/'worklog').glob('KRV-17_NATIVE_SEMANTICS_*.json'),reverse=True)
    for report_path in reports:
        report=json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('kind')!='offline_native_semantic_library' or report.get('status')!='PASS':continue
        entry=next((r for r in report.get('maps',[]) if r.get('source_map')==source_map and r.get('status')=='PASS'),None)
        if entry is None:continue
        path=Path(entry['path']).resolve()
        try:path.relative_to(root)
        except ValueError:raise ValueError('Prepared semantic file must stay in the standalone editor')
        if not path.is_file() or file_sha256(path)!=entry.get('sha256'):
            raise ValueError('Prepared semantic file hash differs; rerun the offline metadata extractor')
        return str(path)
    raise ValueError('No successful prepared semantic batch for '+source_map+'; import an explicit semantics.json file')


def register():
    import bpy
    from bpy.props import StringProperty,BoolProperty,FloatProperty,FloatVectorProperty,EnumProperty,PointerProperty
    from bpy_extras.io_utils import ImportHelper
    class WZ09_NativeSettings(bpy.types.PropertyGroup):
        use_registration:BoolProperty(name='Use verified selected-map registration',default=True)
        source_scale:FloatProperty(name='Explicit exporter coordinate factor',default=0,min=0,precision=9)
        source_axes:StringProperty(name='Signed axes',default='',description='Explicit x,y,z or signed permutation; no default')
        source_offset:FloatVectorProperty(name='Source offset',size=3,default=(0,0,0))
        import_audio:BoolProperty(name='Import actual CreateFX sources',default=True)
        import_actions:BoolProperty(name='Import native action / trigger proxies',default=True)
        ambient_core:BoolProperty(name='Ambient alias on explicit core design bounds',default=False,description='Adds an authored box from the chosen map core; this is not a native extracted zone')
        alias_choice:EnumProperty(name='Captured native aliases',items=_aliases)
    class WZ09_OT_native_import(bpy.types.Operator,ImportHelper):
        bl_idname='wz09.native_semantics_import';bl_label='Import Actual Native Semantics';bl_options={'REGISTER','UNDO'}
        filename_ext='.json'
        filter_glob:StringProperty(default='semantics.json;*.json',options={'HIDDEN'})
        def execute(self,context):
            try:
                owner=resolve_owner_map(context.active_object)
                if owner is None:raise ValueError('Select the matching destination map')
                data=read_semantics(self.filepath);s=context.scene.wz09_native_settings
                if s.use_registration:
                    raw=owner.get('wz09_calibration_json')
                    if not raw:raise ValueError('Map has no verified registration; load one or explicitly enter exporter coordinates')
                    transform=transform_from_calibration(json.loads(raw),source_sha256=data['source_sha256'])
                else:
                    transform=validate_transform({'source_units_to_editor_unit':s.source_scale,
                        'axis_mapping':[v.strip() for v in s.source_axes.split(',')],
                        'offset_m':list(s.source_offset),'basis':'explicit_export_convention'})
                bounds=None
                if s.ambient_core:
                    from .composite_world import serialize_regions
                    regions=[r for r in serialize_regions(context.scene) if r['map_instance_id']==str(owner['wz09_id'])]
                    if len(regions)!=1:raise ValueError('Ambient design box requires one explicit core for the selected map')
                    region=regions[0];poly=region['polygon'];z=region['height_m']
                    bounds=[min(p[0] for p in poly),min(p[1] for p in poly),z-6,max(p[0] for p in poly),max(p[1] for p in poly),z+6]
                result=import_into_scene(context,self.filepath,transform=transform,import_audio=s.import_audio,
                    import_actions=s.import_actions,ambient_bounds_m=bounds)
                self.report({'WARNING'},f"Native: {result['audio_created']} sources, {result['actions_created']} proxies; runtime untested")
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError,TypeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    class WZ09_OT_native_alias(bpy.types.Operator):
        bl_idname='wz09.native_alias_choose';bl_label='Use Native Alias for Audio Zone'
        def execute(self,context):
            try:
                owner=resolve_owner_map(context.active_object)
                if owner is None:raise ValueError('Select the source map')
                inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}'))
                source=inventory.get(str(owner['wz09_id']),{})
                alias=context.scene.wz09_native_settings.alias_choice
                if alias not in source.get('aliases',[]):raise ValueError('Choose an actually captured alias')
                data=read_semantics(source['path'])
                if data['source_map']!=owner.get('wz09_source_map'):
                    raise ValueError('Native alias inventory belongs to another source map')
                if file_sha256(source['path'])!=source['sha256']:raise ValueError('Semantic metadata changed')
                context.scene.wz09_audio_settings.alias_ref=alias
                context.scene['wz09_audio_alias_evidence_json']=audio_zones._encoded({'alias_ref':alias,
                    'map_instance_id':str(owner['wz09_id']),'evidence':{'status':'source_bound','source_path':source['path'],
                    'source_sha256':source['sha256'],'source_record':'audio.aliases[name='+alias+']; shape=explicit_editor_design'}})
                self.report({'INFO'},'Actual captured alias selected; sound payload/runtime untested')
                return {'FINISHED'}
            except (ValueError,OSError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
    class WZ09_OT_native_export_coordinates(bpy.types.Operator):
        bl_idname='wz09.native_export_coordinates';bl_label='Use Reviewed P1a Export Coordinates'
        def execute(self,context):
            try:
                from .source_bundle import verify_preview_bundle
                owner=resolve_owner_map(context.active_object)
                if owner is None:raise ValueError('Select a map with an actual P1a preview')
                provenance=json.loads(owner.get('wz09_map_provenance_json','{}'))
                bundle=provenance.get('preview_bundle')
                if not bundle:raise ValueError('Selected map has no bound native preview bundle')
                verify_preview_bundle(bundle)
                gltf=json.loads(Path(bundle['reference_path']).read_text(encoding='utf-8'))
                if gltf.get('asset',{}).get('generator')!='iw4l P1a semantic exporter':
                    raise ValueError('Preview generator is not the reviewed native P1a exporter')
                settings=context.scene.wz09_native_settings
                settings.source_scale=.0254;settings.source_axes='x,y,z';settings.source_offset=(0,0,0)
                settings.use_registration=False
                self.report({'WARNING'},'P1a XYZ export convention selected explicitly; physical scale remains unverified')
                return {'FINISHED'}
            except (ValueError,OSError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
    class WZ09_OT_native_ready(bpy.types.Operator):
        bl_idname='wz09.native_semantics_ready';bl_label='Load prepared sources for this map';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:
                owner=resolve_owner_map(context.active_object)
                if owner is None:raise ValueError('Select a target map or one of its objects')
                path=prepared_source_for_map(context.scene,str(owner['wz09_source_map']))
                return bpy.ops.wz09.native_semantics_import('EXEC_DEFAULT',filepath=path)
            except (ValueError,OSError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
    class WZ09_PT_native(bpy.types.Panel):
        bl_label='Import original sources';bl_idname='WZ09_PT_native';bl_options={'DEFAULT_CLOSED'}
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_parent_id='WZ09_PT_main'
        @classmethod
        def poll(cls,context):return getattr(context.scene,'wz09_workspace','') in {'WORLD','AUDIO','ACTIONS'}
        def draw(self,context):
            layout,s=self.layout,context.scene.wz09_native_settings
            from . import native_visuals
            native_visuals.draw(layout,context)
            layout.label(text='Original FF facts; no game launch',icon='FILE_TICK')
            layout.prop(s,'use_registration')
            layout.operator('wz09.native_export_coordinates',icon='ORIENTATION_GLOBAL')
            if not s.use_registration:
                layout.prop(s,'source_scale');layout.prop(s,'source_axes');layout.prop(s,'source_offset')
                layout.label(text='Explicit export convention ≠ physical scale',icon='INFO')
            layout.prop(s,'import_audio');layout.prop(s,'import_actions');layout.prop(s,'ambient_core')
            layout.operator('wz09.native_semantics_ready',icon='FILE_TICK')
            layout.operator('wz09.native_semantics_import',text='Choose another metadata file',icon='IMPORT')
            owner=resolve_owner_map(context.active_object)
            inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}'))
            source=inventory.get(str(owner['wz09_id']),{}) if owner is not None else {}
            if source:
                layout.label(text=f"{len(source['aliases'])} actual captured aliases",icon='SOUND')
                if source.get('ambient_alias'):layout.label(text='Map ambient: '+source['ambient_alias'],icon='SOUND')
                layout.prop(s,'alias_choice');layout.operator('wz09.native_alias_choose',icon='CHECKMARK')
            layout.label(text='Radio type is chosen explicitly',icon='INFO')
            layout.label(text='Payload / collider / runtime not installed')
    _CLASSES[:]=[WZ09_NativeSettings,WZ09_OT_native_import,WZ09_OT_native_alias,
                WZ09_OT_native_export_coordinates,WZ09_OT_native_ready,WZ09_PT_native]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_native_settings=PointerProperty(type=WZ09_NativeSettings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_native_settings'):del bpy.types.Scene.wz09_native_settings
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    _ALIAS_CACHE.clear()
