"""Verified preview cost estimates and a sequential existing-importer UI.

No source is read by a panel draw. Explicit refresh uses one read-only worker;
Blender imports run one at a time on its main thread, with a fresh budget check
before each map. Bounds/hidden previews stay resident and never free memory.
"""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
from queue import Queue,Empty
import struct
from threading import Thread,RLock
import uuid

try:
    from .preview_budget import plan_preview_budget
    from .source_bundle import verify_preview_bundle,_external_resource
    from .authoring_helpers import resolve_owner_map
    from .map_library import MAP_REGIONS,MAP_NAMES
except ImportError:
    from preview_budget import plan_preview_budget
    from source_bundle import verify_preview_bundle,_external_resource
    from authoring_helpers import resolve_owner_map
    from map_library import MAP_REGIONS,MAP_NAMES

_CLASSES=[]
_EVENTS=Queue()
_WORKER=None
_GENERATION=0
_IMPORT=None
_COMPONENTS={5120:1,5121:1,5122:2,5123:2,5125:4,5126:4}
_ELEMENTS={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT2':4,'MAT3':9,'MAT4':16}
_HEADER_LIMIT=1024*1024
_REASONS={'unverified_source':'Source unverified','unknown_source_cost':'Quellkosten unbekannt',
          'unknown_resident_cost':'Residente Kosten unbekannt','triangle_budget':'Triangle budget',
          'memory_budget':'Speicherbudget','unknown_additional_resident_cost':'Zusatzdaten-Kosten unbekannt'}


def _integer(value,name,minimum=0,maximum=2**63-1):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError(name+': bounded integer required')
    return value


def bundle_fingerprint(bundle):
    """Identity includes original reference/resource paths AND expected hashes."""
    if not isinstance(bundle,dict) or set(bundle)!={'reference_path','reference_sha256','resources'}:
        raise ValueError('Exact original preview bundle required')
    value=copy.deepcopy(bundle)
    value['resources']=sorted(value['resources'],key=lambda r:r['path'])
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def image_dimensions(header):
    """Bounded PNG/JPEG/WebP header metadata; this is not an image decode test."""
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        if len(header)<33 or header[8:16]!=b'\0\0\0\rIHDR':raise ValueError('Invalid PNG dimension header')
        width,height=struct.unpack('>II',header[16:24]);bits=header[24]
        if bits not in {1,2,4,8,16} or header[25] not in {0,2,3,4,6}:raise ValueError('Unsupported PNG sample layout')
        pixel_bytes=8 if bits==16 else 4
    elif header[:2]==b'\xff\xd8':
        cursor=2;width=height=None
        while cursor<len(header):
            if header[cursor]!=255:raise ValueError('Invalid JPEG marker structure')
            while cursor<len(header) and header[cursor]==255:cursor+=1
            if cursor>=len(header):break
            marker=header[cursor];cursor+=1
            if marker in {0x01,*range(0xD0,0xD9)}:continue
            if marker in {0xD9,0xDA}:break
            if cursor+2>len(header):break
            length=int.from_bytes(header[cursor:cursor+2],'big')
            if length<2 or cursor+length>len(header):break
            if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}:
                if length<8 or header[cursor+2]!=8:raise ValueError('Only 8-bit JPEG estimate supported')
                height,width=struct.unpack('>HH',header[cursor+3:cursor+7]);break
            cursor+=length
        if width is None:raise ValueError('JPEG dimensions not found within bounded header')
        pixel_bytes=4
    elif len(header)>=30 and header[:4]==b'RIFF' and header[8:12]==b'WEBP':
        kind=header[12:16]
        if kind==b'VP8X':width=1+int.from_bytes(header[24:27],'little');height=1+int.from_bytes(header[27:30],'little')
        elif kind==b'VP8L' and header[20]==0x2F:
            value=int.from_bytes(header[21:25],'little');width=1+(value&0x3FFF);height=1+((value>>14)&0x3FFF)
        elif kind==b'VP8 ' and header[23:26]==b'\x9d\x01\x2a':
            width=int.from_bytes(header[26:28],'little')&0x3FFF;height=int.from_bytes(header[28:30],'little')&0x3FFF
        else:raise ValueError('Unsupported WebP dimension header')
        pixel_bytes=4
    else:raise ValueError('Unsupported image header: preview memory remains unknown')
    _integer(width,'image width',1,32768);_integer(height,'image height',1,32768)
    return width,height,pixel_bytes


def estimate_document(document,image_header):
    """Pure mesh/accessor + decoded RGBA estimate, including source instances.

    image_header(index, image) returns at most 1 MiB of already source-bound
    image bytes. No compressed file-size-to-RAM conversion is made. Geometry
    overhead is an explicit conservative model, never measured Blender RAM.
    """
    if document.get('asset',{}).get('version')!='2.0':raise ValueError('glTF2 source required')
    nodes=document.get('nodes',[]);meshes=document.get('meshes',[]);accessors=document.get('accessors',[])
    if len(nodes)>500000 or len(meshes)>100000 or len(accessors)>1000000:raise ValueError('Preview estimate source exceeds metadata budget')
    used_meshes={};draw_triangles=draw_vertices=0;used_accessors=set();unique_vertices=unique_triangles=primitives=0
    def accessor(index):
        if type(index) is not int or not 0<=index<len(accessors):raise ValueError('Invalid source accessor index')
        value=accessors[index];_integer(value.get('count'),'accessor count',0,2**31-1)
        if value.get('componentType') not in _COMPONENTS or value.get('type') not in _ELEMENTS:raise ValueError('Unsupported source accessor layout')
        return value
    for node in nodes:
        if node.get('skin') is not None:raise ValueError('Skinned preview cost requires its explicit animation adapter')
        if node.get('extensions',{}).get('EXT_mesh_gpu_instancing') is not None:raise ValueError('GPU instancing requires an explicit estimate adapter')
        if 'mesh' not in node:continue
        index=node['mesh'];_integer(index,'mesh index',0,len(meshes)-1)
        if index not in used_meshes:
            vertices=triangles=0;positions=set()
            for primitive in meshes[index].get('primitives',[]):
                if primitive.get('extensions') or primitive.get('targets'):raise ValueError('Compressed/morph preview cost needs an explicit adapter')
                attributes=primitive.get('attributes',{})
                if 'POSITION' not in attributes:raise ValueError('Mesh primitive has no source POSITION accessor')
                position=attributes['POSITION'];value=accessor(position)
                if value['type']!='VEC3':raise ValueError('Source POSITION must be VEC3')
                positions.add(position);used_accessors.update(attributes.values())
                count=value['count']
                if 'indices' in primitive:
                    indices=accessor(primitive['indices'])
                    if indices['type']!='SCALAR' or indices['componentType'] not in {5121,5123,5125}:raise ValueError('Invalid source index accessor')
                    count=indices['count'];used_accessors.add(primitive['indices'])
                mode=_integer(primitive.get('mode',4),'primitive mode',0,6)
                if mode==4:
                    if count%3:raise ValueError('Triangle source index/vertex count is not divisible by three')
                    triangles+=count//3
                elif mode in {5,6}:triangles+=max(0,count-2)
                elif mode not in {0,1,2,3}:raise ValueError('Unknown glTF primitive mode')
                primitives+=1
            if not positions:raise ValueError('Empty source mesh cannot be costed')
            vertices=sum(accessor(i)['count'] for i in positions)
            used_meshes[index]=(vertices,triangles);unique_vertices+=vertices;unique_triangles+=triangles
        vertices,triangles=used_meshes[index];draw_vertices+=vertices;draw_triangles+=triangles
    native_bytes=0
    for index in used_accessors:
        value=accessor(index)
        # Matrix columns have four-byte alignment in glTF for byte/short types.
        size=_COMPONENTS[value['componentType']];shape=value['type']
        element_bytes=_ELEMENTS[shape]*size
        if shape.startswith('MAT'):
            side=int(shape[-1]);element_bytes=((side*size+3)//4*4)*side
        native_bytes+=value['count']*element_bytes
    decoded=0;dimensions=[]
    images=document.get('images',[])
    if len(images)>16384:raise ValueError('Preview image count exceeds metadata budget')
    for index,image in enumerate(images):
        width,height,pixel_bytes=image_dimensions(image_header(index,image))
        size=width*height*pixel_bytes;decoded+=size
        dimensions.append({'index':index,'width':width,'height':height,'decoded_rgba_bytes':size})
    geometry=2*native_bytes+96*unique_vertices+48*unique_triangles+2048*len(nodes)+1024*primitives
    # CPU pixels plus GPU allocation/mip overhead; both are estimates.
    texture_estimate=2*((decoded*4+2)//3)
    estimated=geometry+texture_estimate
    _integer(estimated,'estimated bytes')
    return {'vertices':draw_vertices,'triangles':draw_triangles,'unique_vertices':unique_vertices,
        'unique_triangles':unique_triangles,'mesh_instances':sum('mesh' in n for n in nodes),
        'native_accessor_bytes':native_bytes,'decoded_texture_bytes':decoded,
        'estimated_geometry_bytes':geometry,'estimated_texture_bytes':texture_estimate,
        'estimated_bytes':estimated,'images':dimensions,'actual_memory_bytes':None,
        'estimate_kind':'explicit_accessor_geometry_plus_decoded_pixels_not_process_RAM',
        'geometry_formula':'2*accessor_bytes + 96*unique_vertices + 48*unique_triangles + 2048*nodes + 1024*primitives',
        'texture_formula':'2 * ceil(decoded_RGBA_bytes * 4/3) for CPU/GPU/mips'}


def _image_reader(document,bundle):
    reference=Path(bundle['reference_path']).resolve();base=reference.parent
    def uri_bytes(uri,offset=0,length=_HEADER_LIMIT):
        if uri.startswith('data:'):
            prefix,payload=uri.split(',',1)
            if not prefix.endswith(';base64'):raise ValueError('Only base64 embedded image/buffer headers supported')
            needed=((offset+length+2)//3)*4
            if needed>64*1024*1024:raise ValueError('Embedded buffer offset exceeds bounded header read')
            data=base64.b64decode(payload[:needed],validate=True)
            return data[offset:offset+length]
        path=_external_resource(uri,base)
        with path.open('rb') as stream:stream.seek(offset);return stream.read(length)
    def glb_offset():
        with reference.open('rb') as stream:
            stream.seek(12)
            while True:
                chunk=stream.read(8)
                if len(chunk)!=8:raise ValueError('GLB embedded image has no BIN chunk')
                size,kind=struct.unpack('<I4s',chunk)
                if kind==b'BIN\0':return stream.tell()
                stream.seek(size,1)
    def read(index,image):
        if 'uri' in image:return uri_bytes(image['uri'])
        view=document['bufferViews'][image['bufferView']];buffer=document['buffers'][view.get('buffer',0)]
        offset=view.get('byteOffset',0);length=min(_HEADER_LIMIT,view['byteLength'])
        if 'uri' in buffer:return uri_bytes(buffer['uri'],offset,length)
        with reference.open('rb') as stream:stream.seek(glb_offset()+offset);return stream.read(length)
    return read


class BundleCostCache:
    """Stat-invalidated metadata cache; imports ALWAYS force fresh hash checks."""
    def __init__(self,maximum=64):self.maximum=_integer(maximum,'cache maximum',1,512);self.entries={};self.lock=RLock()
    def clear(self):
        with self.lock:self.entries.clear()
    def signature(self,bundle):
        paths=[Path(bundle['reference_path']).resolve()]+[Path(r['path']).resolve() for r in bundle['resources']]
        if len(paths)>32768:raise ValueError('Preview resource count exceeds metadata budget')
        result=[]
        for path in paths:
            s=path.stat();result.append((str(path),s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
        if paths[0].suffix.casefold()=='.gltf' and result[0][3]>128*1024*1024:raise ValueError('Preview reference exceeds bounded JSON metadata size')
        return tuple(result)
    def get(self,bundle,*,force=False):
        key=bundle_fingerprint(bundle)
        with self.lock:
            before=self.signature(bundle);previous=self.entries.get(key)
            if not force and previous and previous['signature']==before:
                return dict(copy.deepcopy(previous['cost']),cache_hit=True,bundle_fingerprint=key)
            document=verify_preview_bundle(bundle)
            cost=estimate_document(document,_image_reader(document,bundle))
            if self.signature(bundle)!=before:raise ValueError('Source changed during cost verification; refresh again')
            self.entries.pop(key,None);self.entries[key]={'signature':before,'cost':copy.deepcopy(cost)}
            while len(self.entries)>self.maximum:self.entries.pop(next(iter(self.entries)))
            return dict(cost,cache_hit=False,bundle_fingerprint=key)


_CACHE=BundleCostCache()


def request_ids(maps,target_id,*,scope='TARGET',selected_ids=(),radius_m=1500.):
    """Choose stable existing IDs by target, selection, design region or radius."""
    records={m['map_instance_id']:m for m in maps}
    if len(records)!=len(maps):raise ValueError('Duplicate logical map ID')
    if target_id not in records:raise ValueError('Choose an existing target map')
    if type(radius_m) not in (int,float) or not math.isfinite(radius_m) or radius_m<=0:raise ValueError('Positive finite nearby radius required')
    target=records[target_id]
    for item in records.values():
        if len(item['position_m'])!=3 or any(type(v) not in (int,float) or not math.isfinite(v) for v in item['position_m']):raise ValueError('Finite map positions required')
    if scope=='TARGET':wanted=[target_id]
    elif scope=='SELECTED':
        if set(selected_ids)-set(records):raise ValueError('Selection contains missing logical map ID')
        wanted=list(dict.fromkeys(selected_ids)) or [target_id]
    elif scope=='REGION':
        if not target.get('region'):raise ValueError('Target has no known design region')
        wanted=[uid for uid,r in records.items() if r.get('region')==target['region']]
    elif scope=='NEARBY':wanted=[uid for uid,r in records.items() if math.dist(r['position_m'],target['position_m'])<=radius_m]
    elif scope=='ALL':wanted=list(records)
    else:raise ValueError('Unknown preview request scope')
    return sorted(wanted,key=lambda uid:(uid!=target_id,math.dist(records[uid]['position_m'],target['position_m']),uid))


def cost_inventory(snapshot,cache=None,*,force=False):
    """Pure source-only costing; unknown resident costs remain explicitly null."""
    cache=cache or _CACHE;inventory=[];details={};errors={};extra_bytes=extra_triangles=0;unknown_extra=[]
    for item in snapshot:
        uid=item['map_instance_id'];static=item.get('bundle');sha=static.get('reference_sha256') if static else None
        record={'map_instance_id':uid,'source_sha256':sha,'loaded':item['loaded'],'triangles':None,'estimated_bytes':None}
        try:
            bundles=item.get('resident_bundles',[]) if item['loaded'] else [static]
            if not bundles or any(b is None for b in bundles):raise ValueError('Source-bound resident preview costs unknown')
            costs=[cache.get(b,force=force) for b in bundles]
            record['triangles']=sum(c['triangles'] for c in costs);record['estimated_bytes']=sum(c['estimated_bytes'] for c in costs)
            details[uid]=costs
        except (ValueError,OSError,KeyError,TypeError) as exc:errors[uid]=str(exc)[:240]
        try:
            extras=[]
            for bundle in item.get('extra_resident_bundles',[]):
                if bundle is None:raise ValueError('Unbound supplemental resident source')
                extras.append(cache.get(bundle,force=force))
            extra_bytes+=sum(c['estimated_bytes'] for c in extras);extra_triangles+=sum(c['triangles'] for c in extras)
            if extras:details[uid+':additional_residents']=extras
        except (ValueError,OSError,KeyError,TypeError) as exc:
            unknown_extra.append(uid);errors[uid+':additional_residents']=str(exc)[:240]
        inventory.append(record)
    return {'inventory':inventory,'cost_details':details,'cost_errors':errors,
        'additional_resident_bytes':extra_bytes,'additional_resident_triangles':extra_triangles,
        'unknown_additional_resident_ids':unknown_extra}


def _snapshot(context):
    from . import map_browser
    scene=context.scene;owners=[o for o in scene.objects if o.get('wz09_type')=='map_instance']
    library={r['code']:r for r in map_browser._catalog(scene)};resident={str(o['wz09_id']):set() for o in owners}
    provenance={str(o['wz09_id']):json.loads(o.get('wz09_map_provenance_json','{}')) for o in owners}
    slices={str(o['wz09_id']):{} for o in owners}
    for obj in scene.objects:
        if obj.get('wz09_slice_bundle_json'):
            owner=resolve_owner_map(obj)
            if owner is not None:
                bundle=json.loads(obj['wz09_slice_bundle_json']);slices[str(owner['wz09_id'])][bundle_fingerprint(bundle)]=bundle
        if not map_browser.is_map_preview_object(obj):continue
        owner=resolve_owner_map(obj)
        if owner is None:continue
        uid=str(owner['wz09_id']);default=provenance[uid].get('preview_bundle',{}).get('reference_sha256')
        resident[uid].add(str(obj.get('wz09_native_visual_source_sha') or default or 'unknown'))
    result=[]
    for owner in owners:
        uid=str(owner['wz09_id']);code=str(owner['wz09_source_map']);entry=library.get(code,{})
        bundle=provenance[uid].get('preview_bundle') or entry.get('bundle')
        known=[bundle] if bundle else []
        known+=json.loads(owner.get('wz09_supplemental_preview_bundles_json','[]'))
        bysha={b['reference_sha256']:b for b in known}
        imports=json.loads(owner.get('wz09_native_visual_imports_json','{}'))
        native_sha={sha for sha,r in imports.items() if r.get('visuals_imported',0)>0 or r.get('source_assets',0)>0}
        static_sha=bundle.get('reference_sha256') if bundle else None
        native_sha.update(s for s in resident[uid] if s!=static_sha)
        result.append({'map_instance_id':uid,'source_map':code,'position_m':list(owner.matrix_world.translation),
            'region':entry.get('region') or MAP_REGIONS.get(code),'bundle':copy.deepcopy(bundle),
            'loaded':static_sha is not None and static_sha in resident[uid],
            'resident_bundles':[copy.deepcopy(bundle)] if static_sha is not None and static_sha in resident[uid] else [],
            'extra_resident_bundles':[copy.deepcopy(bysha.get(s)) for s in sorted(native_sha)]+list(slices[uid].values())})
    if len({r['map_instance_id'] for r in result})!=len(result):raise ValueError('Duplicate map ID; repair before preview import')
    return result


def _target(context,snapshot):
    obj=context.scene.wz09_preview_target or resolve_owner_map(context.active_object)
    if obj is None:raise ValueError('Select a target map or set a target object')
    uid=str(obj.get('wz09_id',''))
    if uid not in {r['map_instance_id'] for r in snapshot}:raise ValueError('Target map does not belong to the current scene')
    return uid


def _active_target(context,snapshot):
    owner=resolve_owner_map(context.active_object)
    uid=str(owner.get('wz09_id','')) if owner is not None else None
    if uid not in {r['map_instance_id'] for r in snapshot}:raise ValueError('Select the intended map or one of its objects as active')
    return uid


def _requests(context,snapshot):
    selected=[]
    for obj in context.selected_objects:
        owner=resolve_owner_map(obj)
        if owner is not None:selected.append(str(owner['wz09_id']))
    target=_target(context,snapshot)
    return request_ids(snapshot,target,scope=context.scene.wz09_preview_scope,selected_ids=selected,
                       radius_m=context.scene.wz09_preview_radius),[target]


def _make_plan(scene,costs,requested,selected):
    triangle_limit=scene.wz09_preview_triangles;memory_limit=scene.wz09_preview_memory_mib*1024**2
    extra_triangles=costs.get('additional_resident_triangles',0);extra_bytes=costs.get('additional_resident_bytes',0)
    reserve=scene.wz09_preview_reserve_mib*1024**2+extra_bytes
    if scene.wz09_preview_reserve_mib*1024**2>memory_limit:raise ValueError('Reserved memory exceeds the preview memory budget')
    plan=plan_preview_budget(costs['inventory'],requested,selected_ids=selected,
        triangle_budget=max(0,triangle_limit-extra_triangles),memory_budget_bytes=memory_limit,
        reserve_bytes=min(reserve,memory_limit))
    unknown=costs.get('unknown_additional_resident_ids',[])
    overflow=max(0,reserve-memory_limit)
    if unknown or overflow or extra_triangles>triangle_limit:
        for uid in plan['load_ids']:
            plan['deferred_ids'].append(uid);plan['reasons'][uid]='unknown_additional_resident_cost' if unknown else 'memory_budget' if overflow else 'triangle_budget'
        plan['load_ids']=[]
        for record in plan['entries']:
            if record['decision']=='load':record.update(decision='deferred',display='PROXY',reason=plan['reasons'][record['map_instance_id']])
    plan.update(planned_bytes=plan['planned_bytes']+overflow,planned_triangles=plan['planned_triangles']+extra_triangles,
        triangle_budget=triangle_limit,reserve_bytes=reserve,additional_resident_bytes=extra_bytes,
        additional_resident_triangles=extra_triangles,unknown_additional_resident_ids=unknown,
        costs_complete=plan['costs_complete'] and not unknown,
        resident_over_budget=plan['resident_over_budget'] or bool(overflow) or extra_triangles>triangle_limit,
        cost_details={uid:[dict({k:v for k,v in c.items() if k!='images'},image_count=c.get('image_count',len(c.get('images',[])))) for c in items]
                      for uid,items in costs['cost_details'].items()},cost_errors=costs['cost_errors'])
    return plan


def _snapshot_fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _store_report(scene,plan,snapshot):
    plan['source_snapshot_sha256']=_snapshot_fingerprint(snapshot)
    plan['map_labels']={r['map_instance_id']:dict(MAP_NAMES).get(r['source_map'],r['source_map']) for r in snapshot}
    scene['wz09_preview_budget_report_json']=json.dumps(plan,sort_keys=True,allow_nan=False)


def _scene_id(scene):
    if not scene.get('wz09_scene_id'):scene['wz09_scene_id']=str(uuid.uuid4())
    return str(scene['wz09_scene_id'])


def _find_scene(uid):
    import bpy
    found=[s for s in bpy.data.scenes if s.get('wz09_scene_id')==uid]
    if len(found)!=1:raise ValueError('Queue scene missing or persistent ID duplicated')
    return found[0]


def refresh_budget(context):
    global _WORKER,_GENERATION
    import bpy
    if _IMPORT is not None or _WORKER is not None and _WORKER.is_alive():raise ValueError('Finish the current budget check/import sequence first')
    snapshot=_snapshot(context);requested,selected=_requests(context,snapshot);uid=_scene_id(context.scene)
    _GENERATION+=1;generation=_GENERATION
    context.scene['wz09_preview_budget_message']='Reading sources and estimating preview cost ...'
    def worker():
        try:result=cost_inventory(snapshot);_EVENTS.put((generation,uid,snapshot,requested,selected,result,None))
        except Exception as exc:_EVENTS.put((generation,uid,snapshot,requested,selected,None,str(exc)[:240]))
    _WORKER=Thread(target=worker,daemon=True,name='WZ09 preview cost verification');_WORKER.start()
    if not bpy.app.timers.is_registered(_refresh_pump):bpy.app.timers.register(_refresh_pump,first_interval=.2)


def _refresh_pump():
    try:
        while True:
            generation,uid,snapshot,requested,selected,costs,error=_EVENTS.get_nowait()
            if generation!=_GENERATION:continue
            scene=_find_scene(uid)
            if error:scene['wz09_preview_budget_message']=error;continue
            plan=_make_plan(scene,costs,requested,selected)
            _store_report(scene,plan,snapshot)
            scene['wz09_preview_budget_message']=f"Estimate: {len(plan['load_ids'])} load - {len(plan['deferred_ids'])} deferred"
    except Empty:pass
    except (ValueError,ReferenceError,KeyError):return None
    return .2 if _WORKER is not None and _WORKER.is_alive() else None


def apply_display(context):
    from . import map_browser
    scene=context.scene;mode=scene.wz09_preview_display
    plan=json.loads(scene.get('wz09_preview_budget_report_json','{}'))
    desired={r['map_instance_id']:r['display'] for r in plan.get('entries',[])}
    if mode=='PLAN' and not desired:raise ValueError('Check the preview budget first')
    for obj in scene.objects:
        if not map_browser.is_map_preview_object(obj):continue
        owner=resolve_owner_map(obj)
        if owner is None:continue
        if mode=='RESTORE':
            if 'wz09_budget_saved_display' in obj:obj.display_type=obj['wz09_budget_saved_display'];del obj['wz09_budget_saved_display']
            if 'wz09_budget_saved_hidden' in obj:obj.hide_set(bool(obj['wz09_budget_saved_hidden']));del obj['wz09_budget_saved_hidden']
            continue
        if 'wz09_budget_saved_display' not in obj:obj['wz09_budget_saved_display']=obj.display_type
        if 'wz09_budget_saved_hidden' not in obj:obj['wz09_budget_saved_hidden']=obj.hide_get()
        display=desired.get(str(owner['wz09_id']),'BOUNDS') if mode=='PLAN' else mode
        obj.hide_set(display=='PROXY')
        if obj.type=='MESH':obj.display_type='BOUNDS' if display in {'BOUNDS','PROXY'} else 'TEXTURED'
    scene['wz09_preview_budget_message']='Display changed; loaded data remains fully resident in memory'


def start_imports(context):
    global _IMPORT
    import bpy
    from . import map_browser
    if _IMPORT is not None or _WORKER is not None and _WORKER.is_alive() or map_browser._QUEUE.running:
        raise ValueError('Another validation/import/conversion sequence is running')
    report=json.loads(context.scene.get('wz09_preview_budget_report_json','{}'))
    snapshot=_snapshot(context)
    if not report or _snapshot_fingerprint(snapshot)!=report.get('source_snapshot_sha256'):raise ValueError('Map inventory/sources changed: check the budget again')
    requested,selected=_requests(context,snapshot)
    costs={k:report[k] for k in ('cost_details','cost_errors')};costs['inventory']=[{k:r[k] for k in (
        'map_instance_id','source_sha256','loaded','triangles','estimated_bytes')} for r in report['entries']]
    for key in ('additional_resident_bytes','additional_resident_triangles','unknown_additional_resident_ids'):costs[key]=report.get(key,[] if key.startswith('unknown') else 0)
    plan=_make_plan(context.scene,costs,requested,selected)
    if not plan['load_ids']:raise ValueError('No additional map fits the verified preview budget')
    records={r['map_instance_id']:r for r in snapshot}
    _IMPORT={'scene_id':_scene_id(context.scene),'window':context.window.as_pointer(),
        'remaining':[dict(records[uid],expected_fingerprint=bundle_fingerprint(records[uid]['bundle'])) for uid in plan['load_ids']],
        'completed':[],'failed':[],'deferred':[],'cancel_requested':False,'total':len(plan['load_ids'])}
    _write_status(context.scene)
    if not bpy.app.timers.is_registered(_import_tick):bpy.app.timers.register(_import_tick,first_interval=.2)
    return plan


def _write_status(scene):
    if _IMPORT is None:return
    scene['wz09_preview_import_status_json']=json.dumps({k:v for k,v in _IMPORT.items() if k not in {'remaining','window'}},sort_keys=True)
    scene['wz09_preview_budget_message']=f"Importfolge: {len(_IMPORT['completed'])}/{_IMPORT['total']} · {len(_IMPORT['failed'])} Fehler · {len(_IMPORT['deferred'])} deferred"


def _import_tick():
    global _IMPORT
    import bpy
    from . import map_browser
    if _IMPORT is None:return None
    try:
        scene=_find_scene(_IMPORT['scene_id'])
        if _IMPORT['cancel_requested'] or not _IMPORT['remaining']:
            _write_status(scene);_IMPORT=None;return None
        windows=[w for w in bpy.context.window_manager.windows if w.as_pointer()==_IMPORT['window'] and w.scene==scene]
        if len(windows)!=1:raise ValueError('Active queue scene/window changed; import sequence stopped')
        if map_browser._QUEUE.running:raise ValueError('Offline conversion running; import sequence stopped')
        job=_IMPORT['remaining'].pop(0);uid=job['map_instance_id']
        with bpy.context.temp_override(window=windows[0],scene=scene,view_layer=windows[0].view_layer):
            snapshot=_snapshot(bpy.context);current=next((r for r in snapshot if r['map_instance_id']==uid),None)
            if current is None or bundle_fingerprint(current['bundle'])!=job['expected_fingerprint']:
                raise ValueError('Planned map proxy source binding changed')
            costs=cost_inventory(snapshot,force=True);plan=_make_plan(scene,costs,[uid],[uid])
            if uid not in plan['load_ids']:
                _IMPORT['deferred'].append({'map_instance_id':uid,'reason':plan['reasons'].get(uid,'already_resident')})
            else:
                _IMPORT['active_id']=uid;_IMPORT['active_fingerprint']=job['expected_fingerprint']
                result=bpy.ops.wz09.preview_import_one('EXEC_DEFAULT',map_id=uid,source_fingerprint=job['expected_fingerprint'])
                _IMPORT.pop('active_id',None);_IMPORT.pop('active_fingerprint',None)
                if result!={'FINISHED'}:raise ValueError('Existing map import rejected; inspect the previous message')
            updated=_snapshot(bpy.context);costs=cost_inventory(updated)
            # The next explicit action can reuse current cost metadata. No
            # timer silently imports a fresh plan after this queue completes.
            report=_make_plan(scene,costs,[r['map_instance_id'] for r in updated],[uid]);_store_report(scene,report,updated)
        _write_status(scene);return .25
    except (ValueError,OSError,RuntimeError,KeyError,TypeError,ReferenceError) as exc:
        if _IMPORT is not None:
            _IMPORT['failed'].append({'map_instance_id':locals().get('uid'),'error':str(exc)[:240]})
            try:_write_status(_find_scene(_IMPORT['scene_id']))
            except (ValueError,ReferenceError):pass
            if not locals().get('uid'):_IMPORT=None;return None
        return .25


def _load_cancel(*args):
    global _IMPORT,_GENERATION
    _IMPORT=None;_GENERATION+=1


def native_capacity_plan(scene,costs,owner_id,candidate,*,already_resident=False):
    """Budget a native-only operation using the SAME existing owner ID."""
    costs=copy.deepcopy(costs)
    own=next((r for r in costs['inventory'] if r['map_instance_id']==owner_id),None)
    if own is None:raise ValueError('Native destination map is missing')
    if not own['loaded']:own.update(triangles=0,estimated_bytes=0)
    if not already_resident:
        costs['additional_resident_bytes']=costs.get('additional_resident_bytes',0)+candidate['estimated_bytes']
        costs['additional_resident_triangles']=costs.get('additional_resident_triangles',0)+candidate['triangles']
    plan=_make_plan(scene,costs,[owner_id],[owner_id])
    plan['operation']='native_sources_and_models'
    plan['fits']=(own['loaded'] or owner_id in plan['load_ids']) and plan['costs_complete'] and not plan['resident_over_budget']
    return plan


def import_native_prepared(context):
    """Explicit selected-map semantics/models/vehicle-blueprint operation."""
    import bpy
    from . import native_semantics as ns,native_visuals as nv,prefab_authoring
    if _IMPORT is not None or _WORKER is not None and _WORKER.is_alive():raise ValueError('Finish the current budget/import sequence first')
    snapshot=_snapshot(context);uid=_active_target(context,snapshot)
    owner=next(o for o in context.scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_id')==uid)
    static=next(r['bundle'] for r in snapshot if r['map_instance_id']==uid)
    document=verify_preview_bundle(static)
    if document.get('asset',{}).get('generator')!='iw4l P1a semantic exporter':raise ValueError('Original models require the verified P1a export source')
    semantic=copy.deepcopy(json.loads(context.scene.get('wz09_native_semantics_index_json','{}')).get(uid))
    visual=copy.deepcopy(json.loads(context.scene.get('wz09_native_visual_index_json','{}')).get(uid))
    if not semantic or not visual:raise ValueError('Prepared semantic and visual indices are missing for this map')
    latest=Path(ns.prepared_source_for_map(context.scene,str(owner['wz09_source_map']))).resolve()
    if latest!=Path(semantic['path']).resolve() or ns.file_sha256(latest)!=semantic['sha256']:
        raise ValueError('Neueste vorbereitete Semantik weicht vom gespeicherten Visual-Index ab; Quellen bewusst neu zuordnen')
    if ns.file_sha256(visual['path'])!=visual['sha256']:raise ValueError('Prepared native visual report has changed')
    prepared=nv.read_visuals(visual['path'],semantic['path'])
    if prepared['semantics']['source_map']!=owner['wz09_source_map'] or prepared['semantics']['source_sha256']!=semantic['original_ff_sha256']:
        raise ValueError('Native original source belongs to another map')
    declared={'source_units_to_editor_unit':.0254,'axis_mapping':['x','y','z'],'offset_m':[0.,0.,0.],'basis':'explicit_export_convention'}
    if owner.get('wz09_native_transform_json'):
        previous=ns.validate_transform(json.loads(owner['wz09_native_transform_json']))
        if (not math.isclose(previous['source_units_to_editor_unit'],.0254,rel_tol=1e-6)
                or [a.lstrip('+') for a in previous['axis_mapping']]!=['x','y','z'] or any(abs(v)>1e-7 for v in previous['offset_m'])):
            raise ValueError('Existing native transform binding differs from P1a; no automatic replacement')
    cost=_CACHE.get(prepared['bundle'],force=True);costs=cost_inventory(snapshot,force=True)
    imports=json.loads(owner.get('wz09_native_visual_imports_json','{}'))
    capacity=native_capacity_plan(context.scene,costs,uid,cost,already_resident=prepared['bundle']['reference_sha256'] in imports)
    if not capacity['fits']:raise ValueError('Native models exceed the verified budget or resident cost is unknown')
    previous_selection=list(context.selected_objects);previous_active=context.active_object
    try:
        for obj in context.selected_objects:obj.select_set(False)
        owner.select_set(True);context.view_layer.objects.active=owner
        if bpy.ops.wz09.native_export_coordinates()!={'FINISHED'}:raise ValueError('Explizite P1a-Exportkonvention abgelehnt')
        settings=context.scene.wz09_native_settings;settings.import_audio=True;settings.import_actions=True
        if bpy.ops.wz09.native_semantics_ready()!={'FINISHED'}:raise ValueError('Native semantics could not be instantiated')
        owner['wz09_native_transform_json']=json.dumps(declared,sort_keys=True)
        current=json.loads(context.scene['wz09_native_semantics_index_json'])[uid]
        if current['sha256']!=semantic['sha256']:raise ValueError('Semantic source changed during import')
        result=nv.import_into_scene(context,visual['path'],semantics_path=semantic['path'])
        context.view_layer.objects.active=owner
        prefabs=prefab_authoring.attach_native_vehicles(context,all_maps=False)
        report={'map_instance_id':uid,'visuals':result,'vehicle_blueprints':prefabs,
                'physical_scale_verified':False,'runtime_integrated':False,'budget_estimate':capacity}
        context.scene['wz09_preview_native_import_json']=json.dumps(report,sort_keys=True,allow_nan=False)
        context.scene['wz09_preview_budget_message']='Original sources/models loaded - P1a 0.0254 - runtime remains unverified'
        return report
    finally:
        for obj in context.selected_objects:obj.select_set(False)
        for obj in previous_selection:
            if obj.name in context.scene.objects:obj.select_set(True)
        if previous_active is not None and previous_active.name in context.scene.objects:context.view_layer.objects.active=previous_active


def plan_static_unload(records,map_instance_id):
    """Pure transactional preflight; keys identify this in-memory object set.

    Every candidate must be local, exclusively in this scene, not linked into a
    foreign collection, and have no foreign data owners. Any noncandidate child
    blocks deletion. Shared local meshes/templates are allowed and retained by
    actual datablock users after unlinking. No geometry or metadata is changed.
    """
    bykey={r['key']:r for r in records}
    if len(bykey)!=len(records):raise ValueError('Duplicate object transaction key')
    selected={key:r for key,r in bykey.items() if r.get('eligible') and r.get('map_instance_id')==map_instance_id}
    preview_ids=set()
    for key,record in selected.items():
        preview_id=record.get('preview_id')
        if not isinstance(preview_id,str) or not preview_id or preview_id in preview_ids:raise ValueError('Missing/duplicate persistent preview ID')
        preview_ids.add(preview_id)
        if (not record.get('scene_local') or record.get('linked') or record.get('foreign_collection')
                or record.get('foreign_data_user') or record.get('authored_modifications')):
            raise ValueError('Linked/shared foreign or modified preview data cannot be unloaded automatically')
    if any(r.get('parent_key') in selected and key not in selected for key,r in bykey.items()):
        raise ValueError('Static preview has unrelated authoring children; detach them explicitly before unloading')
    depths={}
    for key in selected:
        visited=set();current=key;depth=0
        while current in selected:
            if current in visited:raise ValueError('Cyclic source preview hierarchy')
            visited.add(current);current=selected[current].get('parent_key');depth+=1
        depths[key]=depth
    return {'map_instance_id':map_instance_id,'object_keys':sorted(selected,key=lambda k:(-depths[k],str(k))),
            'objects':len(selected),'automatic_orphan_purge':False}


def unload_static_preview(context):
    """Remove only selected owner's known static objects and unused candidates."""
    import bpy
    from . import map_browser,composite_world,preview_linking
    if _IMPORT is not None or _WORKER is not None and _WORKER.is_alive():raise ValueError('Finish the current budget/import sequence first')
    snapshot=_snapshot(context);uid=_active_target(context,snapshot)
    owner=next(o for o in context.scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_id')==uid)
    provenance=json.loads(owner.get('wz09_map_provenance_json','{}'));source=provenance.get('preview_bundle',{}).get('reference_sha256')
    if not source:raise ValueError('Map has no bound static preview source')
    scene_keys={o.as_pointer() for o in context.scene.objects};collections=set()
    def collect(collection):
        if collection.as_pointer() in collections:return
        collections.add(collection.as_pointer())
        for child in collection.children:collect(child)
    collect(context.scene.collection)
    objects={o.as_pointer():o for o in context.scene.objects};records=[];candidates=[]
    all_objects=list(bpy.data.objects)
    foreign_objects=composite_world._foreign_scene_index(context.scene,all_objects)
    collection_links=preview_linking.collection_index(objects.values(),bpy)
    foreign_data_keys=set()
    for user in all_objects:
        data=getattr(user,'data',None)
        if data is None:continue
        known_template=bool(user.get('wz09_browser_template') and not user.library and not user.override_library)
        if not known_template and (user.as_pointer() not in scene_keys or user.as_pointer() in foreign_objects):
            foreign_data_keys.add(data.as_pointer())
    for key,obj in objects.items():
        belongs=resolve_owner_map(obj)==owner
        eligible=bool(belongs and map_browser.is_map_preview_object(obj)
            and obj.get('wz09_native_visual_source_sha',source)==source)
        data=getattr(obj,'data',None);foreign_data=data is not None and data.as_pointer() in foreign_data_keys
        records.append({'key':key,'parent_key':obj.parent.as_pointer() if obj.parent else None,
            'map_instance_id':uid if belongs else None,'eligible':eligible,
            'scene_local':key in scene_keys and key not in foreign_objects,
            'linked':bool(obj.library or obj.override_library or data is not None and (data.library or data.override_library)),
            'foreign_collection':any(c.as_pointer() not in collections for c in collection_links[key]),
            'foreign_data_user':foreign_data,'preview_id':obj.get('wz09_preview_id'),
            'authored_modifications':bool(obj.constraints or obj.modifiers or obj.animation_data and
                (obj.animation_data.action or obj.animation_data.drivers or obj.animation_data.nla_tracks))})
        if eligible:candidates.append(obj)
    plan=plan_static_unload(records,uid)
    if not plan['objects']:raise ValueError('This map has no unloadable static preview')
    # Other objects referencing a candidate through constraints/modifiers would
    # receive a dangling target after deletion. Inspect Blender's actual ID map.
    candidate_keys={o.as_pointer() for o in candidates}
    for obj,users in bpy.data.user_map(subset=set(candidates)).items():
        for user in users:
            if isinstance(user,bpy.types.Object) and user.as_pointer() not in candidate_keys:
                raise ValueError('Authoring object references a static source object; detach explicitly before unloading')
    meshes={o.data.as_pointer():o.data for o in candidates if o.type=='MESH'}
    for obj in candidates:
        original=getattr(obj,'wz09_core_original_mesh',None)
        if original is not None:meshes[original.as_pointer()]=original
    materials={slot.material.as_pointer():slot.material for o in candidates for slot in o.material_slots if slot.material}
    for mesh in meshes.values():
        for material in mesh.materials:
            if material is not None:materials[material.as_pointer()]=material
    images={}
    for material in materials.values():
        if material.node_tree:
            for node in material.node_tree.nodes:
                image=getattr(node,'image',None)
                if image is not None:images[image.as_pointer()]=image
    for key in plan['object_keys']:bpy.data.objects.remove(objects[key],do_unlink=True)
    owner.select_set(True);context.view_layer.objects.active=owner
    removed={'objects':plan['objects'],'meshes':0,'materials':0,'images':0}
    for group,data in (('meshes',meshes),('materials',materials),('images',images)):
        registry=getattr(bpy.data,group)
        for item in data.values():
            if item.users==0 and item.library is None and item.override_library is None:
                registry.remove(item);removed[group]+=1
    scene=context.scene
    scene['wz09_map_library_json']=json.dumps(map_browser._update_imported_counts(scene,map_browser._catalog(scene)),sort_keys=True)
    remaining=_snapshot(context);costs=cost_inventory(remaining)
    report=_make_plan(scene,costs,[uid],[uid]);_store_report(scene,report,remaining)
    result=dict(removed,map_instance_id=uid,retained_shared_meshes=len(meshes)-removed['meshes'],
        automatic_orphan_purge=False,source_files_written=False,actual_memory_reclaimed_bytes=None)
    scene['wz09_preview_unload_json']=json.dumps(result,sort_keys=True)
    scene['wz09_preview_budget_message']=f"Static preview unloaded: {removed['objects']} Objekte · gemeinsam genutzte Daten bleiben erhalten"
    return result


def register():
    import bpy
    from bpy.props import IntProperty,FloatProperty,EnumProperty,PointerProperty,StringProperty
    class WZ09_OT_preview_import_one(bpy.types.Operator):
        bl_idname='wz09.preview_import_one';bl_label='Load verified map preview';bl_options={'UNDO','INTERNAL'}
        map_id:StringProperty();source_fingerprint:StringProperty()
        def execute(self,ctx):
            try:
                from . import map_browser
                if (_IMPORT is None or _IMPORT.get('active_id')!=self.map_id
                        or _IMPORT.get('active_fingerprint')!=self.source_fingerprint):raise ValueError('No freshly budget-verified import request')
                item=next(r for r in _snapshot(ctx) if r['map_instance_id']==self.map_id)
                if bundle_fingerprint(item['bundle'])!=self.source_fingerprint:raise ValueError('Source binding changed during import request')
                owner=next(o for o in ctx.scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_id')==self.map_id)
                result=map_browser.import_map_preview(ctx,item['source_map'],item['bundle'],target=owner,placement='ORIGIN')
                ctx.scene['wz09_map_library_json']=json.dumps(map_browser._update_imported_counts(ctx.scene,map_browser._catalog(ctx.scene)),sort_keys=True)
                _IMPORT['completed'].append({'map_instance_id':self.map_id,'objects':result['objects']})
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError,TypeError,StopIteration) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    class WZ09_OT_preview_budget(bpy.types.Operator):
        bl_idname='wz09.preview_budget';bl_label='Preview budget';bl_options={'REGISTER','UNDO'}
        action:EnumProperty(items=[(s,s,'') for s in ('REFRESH','DISPLAY','IMPORT','STOP','NATIVE','UNLOAD')])
        def execute(self,ctx):
            try:
                if self.action=='REFRESH':refresh_budget(ctx)
                elif self.action=='DISPLAY':apply_display(ctx)
                elif self.action=='IMPORT':start_imports(ctx)
                elif self.action=='NATIVE':import_native_prepared(ctx)
                elif self.action=='UNLOAD':unload_static_preview(ctx)
                elif _IMPORT is not None:_IMPORT['cancel_requested']=True
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError,TypeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    class WZ09_PT_preview_budget(bpy.types.Panel):
        bl_idname='WZ09_PT_preview_budget';bl_label='Map preview - memory budget';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
        @classmethod
        def poll(cls,ctx):return getattr(ctx.scene,'wz09_workspace','') in {'MAPS','WORLD'}
        def draw(self,ctx):
            a=self.layout;s=ctx.scene
            for name in ('wz09_preview_target','wz09_preview_scope'):a.prop(s,name)
            if s.wz09_preview_scope=='NEARBY':a.prop(s,'wz09_preview_radius')
            for name in ('wz09_preview_memory_mib','wz09_preview_reserve_mib','wz09_preview_triangles'):a.prop(s,name)
            a.operator('wz09.preview_budget',text='Check budget and sources',icon='FILE_REFRESH').action='REFRESH'
            report=json.loads(s.get('wz09_preview_budget_report_json','{}'))
            if report:
                a.label(text=f"Geplant: {report['planned_bytes']/1024**2:.0f} MiB · {report['planned_triangles']:,} Dreiecke",icon='INFO')
                a.label(text=f"Resident: {len(report['resident_ids'])} · Ladbar: {len(report['load_ids'])} - Deferred: {len(report['deferred_ids'])}")
                if report['resident_over_budget']:a.label(text='Loaded maps already exceed budget',icon='ERROR')
                for uid,reason in list(report['reasons'].items())[:4]:a.label(text=report.get('map_labels',{}).get(uid,uid[:8])+' · '+_REASONS.get(reason,reason),icon='ERROR')
                for uid,error in list(report.get('cost_errors',{}).items())[:2]:a.label(text=error[:85],icon='ERROR')
            a.operator('wz09.preview_budget',text='Load fitting maps sequentially',icon='IMPORT').action='IMPORT'
            a.operator('wz09.preview_budget',text='Unload selected static map',icon='TRASH').action='UNLOAD'
            a.operator('wz09.preview_budget',text='Load original sources + models',icon='FILE_TICK').action='NATIVE'
            a.label(text='Native: explicit P1a convention 0.0254 XYZ',icon='INFO')
            a.label(text='Physical source scale remains unverified',icon='INFO')
            if _IMPORT is not None:a.operator('wz09.preview_budget',text='Stop after current map',icon='CANCEL').action='STOP'
            a.prop(s,'wz09_preview_display');a.operator('wz09.preview_budget',text='Apply display settings',icon='MESH_CUBE').action='DISPLAY'
            a.label(text='Estimate only; process memory was not measured',icon='INFO')
            a.label(text='Bounds / hiding do not release memory',icon='INFO')
            if s.get('wz09_preview_budget_message'):a.label(text=str(s['wz09_preview_budget_message'])[:100])
    _CLASSES[:]=[WZ09_OT_preview_import_one,WZ09_OT_preview_budget,WZ09_PT_preview_budget]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_preview_memory_mib=IntProperty(name='Preview budget (MiB)',default=2048,min=64,max=32768)
    bpy.types.Scene.wz09_preview_reserve_mib=IntProperty(name='Reserve for other data (MiB)',default=512,min=0,max=32768)
    bpy.types.Scene.wz09_preview_triangles=IntProperty(name='Triangle budget',default=3000000,min=10000,max=100000000)
    bpy.types.Scene.wz09_preview_target=PointerProperty(name='Target map',type=bpy.types.Object,poll=lambda self,o:o.get('wz09_type')=='map_instance')
    bpy.types.Scene.wz09_preview_scope=EnumProperty(name='Load scope',items=[('TARGET','Target map',''),('SELECTED','Selected maps',''),
        ('REGION','Same design region',''),('NEARBY','Near target map',''),('ALL','All logical maps','')],default='TARGET')
    bpy.types.Scene.wz09_preview_radius=FloatProperty(name='Search radius (editor meters)',default=1500,min=1,max=1e7)
    bpy.types.Scene.wz09_preview_display=EnumProperty(name='Display',items=[('PLAN','According to budget plan',''),('BOUNDS','Geladene Maps als Bounds',''),
        ('PROXY','Proxies only (meshes remain in memory)',''),('FULL','Show loaded maps in full',''),('RESTORE','Restore previous display','')],default='PLAN')
    bpy.app.handlers.persistent(_load_cancel);bpy.app.handlers.load_pre.append(_load_cancel)


def unregister():
    import bpy
    _load_cancel()
    for timer in (_refresh_pump,_import_tick):
        if bpy.app.timers.is_registered(timer):bpy.app.timers.unregister(timer)
    if _load_cancel in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(_load_cancel)
    for name in ('memory_mib','reserve_mib','triangles','target','scope','radius','display'):
        key='wz09_preview_'+name
        if hasattr(bpy.types.Scene,key):delattr(bpy.types.Scene,key)
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear();_CACHE.clear()
