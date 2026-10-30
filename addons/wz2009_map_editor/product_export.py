"""Separate, hash-bound editor products over the existing world-v1 contract.

Serializers run after world capture, never recursively from scene_io. Export
publishes immutable content-addressed sidecars; a failed world publication can
leave only reusable, fully validated sidecars, never a half-written manifest.
"""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import uuid
import importlib

KINDS=('wz09_gameplay_prefabs','wz09_animation_studio','wz09_cinematic_timeline',
       'wz09_poi_labels','wz09_asset_dependencies','wz09_navigation','wz09_audio_zones','wz09_action_elements')
RUNTIME_KINDS=('wz09_runtime_world','wz09_intro_runtime','wz09_royale_layout','wz09_terrain_collision')
SUPPORTED_KINDS=KINDS+RUNTIME_KINDS
VIEWS={'wz09_navigation':('nodes','edges'),'wz09_audio_zones':('audio_zones',),
       'wz09_action_elements':('action_elements',)}

def _module(name):
    return importlib.import_module(('.'+name) if __package__ else name,__package__ or None)


def identity(kind,world_id):
    if kind not in SUPPORTED_KINDS:raise ValueError('Unknown editor product kind')
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:product:'+world_id+':'+kind))


def encoded(document):
    if document.get('kind') in RUNTIME_KINDS:return _module('runtime_export').encoded(document)
    return (json.dumps(document,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n').encode('utf-8')


def document_id(document,world):
    value=document['id'] if 'id' in document else identity(document['kind'],world['world_id'])
    if not isinstance(value,str) or not value.strip() or len(value)>512:raise ValueError('Product ID must be nonempty text')
    return value


def _world_ids(world):
    result=_module('cinematic_core').world_ids(world)
    layers=world.get('composition',{}).get('material_layers',{})
    result.update(r['id'] for key in ('rules','masks') for r in layers.get(key,[]))
    return result


def validate_documents(world,documents,verify_sources=True):
    """Check product/global IDs, typed references and every supported contract."""
    prefab_core,animation_core,cinematic_core,poi_labels=map(_module,('prefab_core','animation_core','cinematic_core','poi_labels'))
    used=_world_ids(world);seen=set();clip_ids=set();rig_ids=set();exported_attachment_ids=set()
    world_object_ids={r['id'] for key in ('asset_placements','geometry') for r in world.get(key,[])}
    for document in documents:
        if not isinstance(document,dict) or document.get('kind') not in SUPPORTED_KINDS:raise ValueError('Unsupported editor product')
        kind=document['kind']
        if kind in seen:raise ValueError('Duplicate editor product kind')
        seen.add(kind)
        ids={document_id(document,world)}
        if kind in RUNTIME_KINDS:
            _module('runtime_export').validate_document(document,world)
            if kind=='wz09_intro_runtime':ids.update(fade['id'] for fade in document.get('screen_fades',[]))
        elif kind=='wz09_gameplay_prefabs':
            if set(document)!={'kind','version','id','prefabs','runtime_integrated'} or type(document['version']) is not int or document['version']!=1 or document['runtime_integrated'] is not False:
                raise ValueError('Unsupported prefab sidecar fields/claims')
            records=prefab_core.validate_records(document['prefabs'],maps={r['id']:r for r in world['maps']},
                placements={r['id']:r for r in world.get('asset_placements',[])},markers={r['id']:r for r in world['markers']},
                actions={r['id']:r for r in world.get('action_elements',[])},all_ids=used|ids,verify_sources=verify_sources)
            ids.update(r['id'] for r in records)
        elif kind=='wz09_animation_studio':
            checked=animation_core.validate_library(document,verify_sources=verify_sources)
            if document_id(document,world) in checked['ids']:raise ValueError('Animation product ID collides with its entity')
            ids.update(checked['ids']);clip_ids.update(r['id'] for r in document['clips']);rig_ids.update(r['id'] for r in document['rigs'])
            exported_attachment_ids.update(uid for record in document.get('exports',[]) for uid in record['object_ids'])
        elif kind=='wz09_cinematic_timeline':
            cinematic_core.validate_document(document,world)
            ids.update(r['id'] for field in ('cameras','route_bindings','cuts','events') for r in document[field])
        elif kind=='wz09_poi_labels':poi_labels.validate_document(document,world)
        elif kind=='wz09_asset_dependencies':_validate_dependencies(document,world,verify_sources)
        elif kind in VIEWS:
            fields=VIEWS[kind]
            if (set(document)!={'kind','version','id','units','runtime_integrated',*fields}
                    or type(document['version']) is not int or document['version']!=1
                    or document['units']!='editor_design_meters' or document['runtime_integrated'] is not False):
                raise ValueError('Unsupported world-view sidecar fields/claims')
            for field in fields:
                source=world['nav'][field] if kind=='wz09_navigation' else world.get(field,[])
                if document[field]!=source:raise ValueError('Sidecar differs from its existing world records: '+field)
        if ids&used:raise ValueError('Duplicate global product/entity ID: '+sorted(ids&used)[0])
        used.update(ids)
    for document in documents:
        if document['kind']=='wz09_cinematic_timeline':
            for event in document['events']:
                if event['kind']=='animation' and event['reference'].startswith('clip:') and event['reference'][5:] not in clip_ids:
                    raise ValueError('Cinematic animation clip reference is missing')
        elif document['kind']=='wz09_animation_studio':
            for record in document['attachments']:
                if record['object_id'] not in world_object_ids|exported_attachment_ids:raise ValueError('Animation attachment references an unexported object ID; export its clip in Animation Studio first')
    prefabs=[r for doc in documents if doc['kind']=='wz09_gameplay_prefabs' for r in doc['prefabs']]
    regions={r['id']:r for r in world.get('composition',{}).get('regions',[])}
    audio={r['id']:r for r in world.get('audio_zones',[])}
    for record in prefabs:
        region=record['parameters'].get('region_ref','')
        if region and (region not in regions or regions[region]['map_instance_id']!=record['map_instance_id']):raise ValueError('Prefab region reference is missing or belongs to another map')
        sound=record['parameters'].get('audio_ref','')
        if isinstance(sound,str) and re.fullmatch('[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',sound):
            if sound not in audio or audio[sound]['map_instance_id']!=record['map_instance_id']:raise ValueError('Prefab audio-zone reference is missing or belongs to another map')
    for scatter in world.get('composition',{}).get('scatter_sets',[]):
        if 'authoring_filters' in scatter:
            _module('terrain_authoring').validate_scatter_settings(scatter['authoring_filters'],world=world,prefabs=prefabs)
    return {'products':len(documents),'ids':used,'clips':clip_ids,'rigs':rig_ids}


def _validate_dependencies(document,world,verify_sources):
    validate_asset_record=_module('asset_catalog').validate_asset_record
    catalog_from_document=_module('asset_catalog').catalog_from_document
    verify_preview_bundle=_module('source_bundle').verify_preview_bundle
    if (set(document)!={'kind','version','id','assets','sources','runtime_integrated'} or type(document['version']) is not int
            or document['version']!=1 or document['runtime_integrated'] is not False):raise ValueError('Unsupported asset dependency sidecar')
    if not isinstance(document['assets'],list) or not isinstance(document['sources'],dict):raise ValueError('Invalid asset dependencies')
    ids=set();verified={}
    for record in document['assets']:
        validate_asset_record(record)
        if record['id'] in ids:raise ValueError('Duplicate dependency asset ID')
        ids.add(record['id']);source=document['sources'].get(record['source_sha256'])
        if source is None or source.get('source_map')!=record['source_map']:raise ValueError('Asset dependency source is missing')
        bundle=source.get('preview_bundle',{})
        if bundle.get('reference_sha256')!=record['source_sha256'] or bundle.get('reference_path')!=record['reference_path']:raise ValueError('Asset dependency bundle differs')
        if verify_sources:
            key=record['source_sha256']
            if key not in verified:
                native=verify_preview_bundle(bundle)
                verified[key]={a['id']:a for a in catalog_from_document(native,bundle,record['source_map'])['assets']}
            actual=verified[key].get(record['id'])
            if actual is None or any(record.get(k)!=v for k,v in actual.items()):raise ValueError('Asset dependency differs from original source')
    if any(a['asset_id'] not in ids for a in world.get('asset_placements',[])):raise ValueError('Placed asset has no exported dependency')


def collect(scene,world):
    _module('asset_workspace').validate_adoptions(scene)
    prefab_authoring,animation_studio,cinematic_studio,poi_labels,asset_browser=map(_module,('prefab_authoring','animation_studio','cinematic_studio','poi_labels','asset_browser'))
    documents=[]
    for kind,fields in VIEWS.items():
        records={field:copy.deepcopy(world['nav'][field] if kind=='wz09_navigation' else world.get(field,[])) for field in fields}
        if kind=='wz09_navigation' or any(records.values()):
            documents.append(dict(kind=kind,version=1,id=identity(kind,world['world_id']),
                units='editor_design_meters',runtime_integrated=False,**records))
    if any(o.get('wz09_prefab') for o in scene.objects):documents.append(prefab_authoring.serialize_scene(scene,world))
    if any(o.get('wz09_rig_id') for o in scene.objects):
        library=animation_studio.serialize_scene(scene)
        library.update(kind='wz09_animation_studio',id=identity('wz09_animation_studio',world['world_id']),runtime_integrated=False)
        documents.append(library)
    if scene.get('wz09_cinematics_json'):documents.append(cinematic_studio.serialize_scene(scene,world))
    if any(m['kind']=='poi' for m in world['markers']):documents.append(poi_labels.serialize_scene(scene,world))
    required={p['asset_id'] for p in world.get('asset_placements',[])}
    for doc in documents:
        if doc['kind']=='wz09_gameplay_prefabs':
            required.update(p['parameters']['destroyed_model_asset_id'] for p in doc['prefabs'] if p['parameters'].get('destroyed_model_asset_id'))
    if required:
        catalog=asset_browser._catalog(scene);lookup={a['id']:a for a in catalog['assets']}
        if required-set(lookup):raise ValueError('Missing asset dependencies in existing source catalog')
        assets=[copy.deepcopy(lookup[a]) for a in sorted(required)]
        documents.append(dict(kind='wz09_asset_dependencies',version=1,
            id=identity('wz09_asset_dependencies',world['world_id']),assets=assets,
            sources={a['source_sha256']:copy.deepcopy(catalog['sources'][a['source_sha256']]) for a in assets},runtime_integrated=False))
    validate_documents(world,documents)
    return sorted(documents,key=lambda d:d['kind'])


def references(world,documents):
    validate_documents(world,documents)
    result=[]
    for document in sorted(documents,key=lambda d:d['kind']):
        digest=hashlib.sha256(encoded(document)).hexdigest()
        result.append(dict(id=document_id(document,world),kind=document['kind'],
            path=('products/'+digest+'.json' if document['kind'] in RUNTIME_KINDS else
                  'products/'+document['kind']+'-'+digest[:20]+'.json'),sha256=digest,runtime_integrated=False))
    return result


def publish(world,documents,world_path,root):
    atomic_write=_module('scene_io').atomic_write;safe_output_path=_module('scene_io').safe_output_path
    target=safe_output_path(world_path,root);refs=references(world,documents)
    lookup={d['kind']:d for d in documents}
    for ref in refs:
        if ref['kind'] in RUNTIME_KINDS:
            _module('runtime_export').publish_bytes(target.parent/ref['path'],encoded(lookup[ref['kind']]),root=root)
        else:atomic_write(target.parent/ref['path'],lookup[ref['kind']],root=root)
    result=copy.deepcopy(world)
    if refs:result['products']=refs
    return result


def read_references(world,base_dir=None):
    refs=world.get('products',[]);kinds=set();ids=_world_ids(world);documents=[]
    if not isinstance(refs,list) or len(refs)>len(SUPPORTED_KINDS):raise ValueError('Invalid editor product reference list')
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)!={'id','kind','path','sha256','runtime_integrated'}:raise ValueError('Unknown product reference fields')
        if ref['kind'] not in SUPPORTED_KINDS or ref['kind'] in kinds:raise ValueError('Unsupported/duplicate product kind')
        kinds.add(ref['kind'])
        if not isinstance(ref['id'],str) or not ref['id'].strip() or ref['id'] in ids:raise ValueError('Duplicate or missing product ID')
        ids.add(ref['id'])
        if ref['runtime_integrated'] is not False or not isinstance(ref['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',ref['sha256']):raise ValueError('Invalid product hash/verification claim')
        value=ref['path']
        if not isinstance(value,str) or not value or '\\' in value or ':' in value:raise ValueError('Product path must be relative POSIX JSON')
        path=PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts or path.suffix!='.json':raise ValueError('Product path escapes export directory')
        if base_dir is not None:
            base=Path(base_dir).resolve();target=(base/value).resolve()
            if not target.is_relative_to(base) or not target.is_file():raise ValueError('Product file missing/escaped')
            if target.stat().st_size>64*1024*1024:raise ValueError('Product file exceeds the 64 MiB validation budget')
            payload=target.read_bytes()
            if hashlib.sha256(payload).hexdigest()!=ref['sha256']:raise ValueError('Product file hash changed')
            doc=json.loads(payload)
            if doc.get('kind')!=ref['kind'] or document_id(doc,world)!=ref['id']:raise ValueError('Product file identity differs')
            if ref['kind'] in RUNTIME_KINDS:_module('runtime_export').validate_document(doc,world,base)
            documents.append(doc)
    if base_dir is not None:validate_documents(world,documents)
    return documents
