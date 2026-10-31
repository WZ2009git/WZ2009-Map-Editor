"""Reusable editable map parts, cached in private versioned detail libraries."""
import json
from pathlib import Path
import uuid

FORMAT='wz09-editable-parts-v1'
READY='wz09_editable_parts_json'


def geometry_sources(objects):
    from . import city_editing as city
    result=[]
    for obj in objects:
        if obj.type!='MESH' or not obj.data.polygons or obj.modifiers:continue
        if obj.get(city.PART) or obj.get('wz09_editor_overlay') or obj.get('wz09_native_instance_prototype'):continue
        if not (obj.get('wz09_preview_only') or obj.get(city.SOURCE)):continue
        metadata=obj.get('iw4l',{})
        if hasattr(metadata,'get') and metadata.get('model'):continue
        result.append(obj)
    return result


def ready(collection):
    if collection is None:return False
    try:return json.loads(collection.get(READY,'{}')).get('format')==FORMAT
    except (ValueError,TypeError):return False


def make_asset_mesh_unique(obj):
    """Keep placements cheap until the user edits one placement's vertices."""
    metadata=obj.get('iw4l',{})
    individual=obj.get('wz09_individual_asset') or (hasattr(metadata,'get') and metadata.get('model'))
    if individual and obj.type=='MESH' and obj.data.users>1:
        obj.data=obj.data.copy()
        obj.data.use_fake_user=False
        obj['wz09_asset_mesh_unique']=True
        return True
    return False


def repair_legacy_wrappers(root,objects):
    """Disambiguate only coincident, nested, empty serialization wrappers."""
    from . import city_editing as city
    grouped={}
    for obj in objects:
        identity=obj.get('wz09_library_object_id')
        if identity:grouped.setdefault(identity,[]).append(obj)
    repairs=[]
    for identity,duplicates in grouped.items():
        if len(duplicates)<2:continue
        if any(obj.type!='EMPTY' or obj.get('wz09_id')!=root['wz09_id'] or obj.get('wz09_type')=='map_instance'
               or obj.get(city.SOURCE) or obj.get(city.PART) or obj.modifiers or obj.constraints or obj.animation_data for obj in duplicates):
            raise ValueError('Duplicate persistent IDs outside legacy assembly wrappers; no identities were guessed')
        reference=duplicates[0].matrix_world
        if any(max(abs(obj.matrix_world[r][c]-reference[r][c]) for r in range(4) for c in range(4))>1e-6 for obj in duplicates):
            raise ValueError('Ambiguous legacy wrapper transforms; identity repair refused')
        def depth(obj):
            result=0
            while obj.parent is not None:
                result+=1;obj=obj.parent
                if result>10000:raise ValueError('Assembly parent cycle')
            return result
        ordered=sorted(((depth(obj),obj) for obj in duplicates),key=lambda row:row[0],reverse=True)
        if len({row[0] for row in ordered})!=len(ordered):raise ValueError('Ambiguous sibling wrapper identities')
        # Retain the original ID on the oldest inner wrapper used by existing
        # parent records. New IDs describe derived wrappers, never mesh parts.
        for level,obj in ordered[1:]:
            replacement=str(uuid.uuid5(uuid.NAMESPACE_URL,f"wz09-wrapper:{root['wz09_id']}:{identity}:{level}"))
            if replacement in grouped:raise ValueError('Repaired wrapper ID would collide with existing data')
            repairs.append((obj,identity,replacement))
    for obj,identity,replacement in repairs:
        obj['wz09_wrapper_origin_id']=identity;obj['wz09_library_object_id']=replacement
    return len(repairs)


def cache_path(project):return Path(project)/'.local/editable_parts/index.json'


def cache_key(record):return str(record['map_id'])+':'+str(record['sha256'])


def resolve(record,project):
    """Only exact immutable source revisions can reuse a prepared library."""
    from . import map_residency as residency
    if record.get('editable_parts_format')==FORMAT:return record
    path=cache_path(project)
    if not path.is_file():return record
    index=json.loads(path.read_text(encoding='utf-8'))
    candidate=index.get('libraries',{}).get(cache_key(record))
    if not isinstance(candidate,dict):return record
    for field in ('map_id','source_map','layer'):
        if candidate.get(field)!=record.get(field):raise ValueError('Prepared map-parts cache identity mismatch')
    if candidate.get('editable_parts_format')!=FORMAT:raise ValueError('Prepared map-parts cache format mismatch')
    target=Path(candidate['path']).resolve()
    if not target.is_relative_to(Path(project).resolve()) or target.suffix!='.blend':raise ValueError('Prepared map-parts cache escapes this editor')
    if not target.is_file():return record
    if residency.sha(target)!=candidate.get('sha256'):raise ValueError('Prepared map-parts library changed outside this workflow')
    return candidate


def publish_cache(original,prepared,project):
    """Publish only fully written, verified, source-bound local records."""
    if prepared.get('editable_parts_format')!=FORMAT:raise ValueError('Cannot cache unprepared map geometry')
    if any(original.get(k)!=prepared.get(k) for k in ('map_id','source_map','layer')):raise ValueError('Prepared map identity changed')
    path=cache_path(project);path.parent.mkdir(parents=True,exist_ok=True)
    index=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else dict(format=FORMAT,libraries={})
    index['libraries'][cache_key(original)]=prepared
    temporary=path.with_name('index-'+uuid.uuid4().hex+'.tmp')
    with temporary.open('x',encoding='utf-8') as stream:json.dump(index,stream,indent=2)
    temporary.replace(path)


def prepare(ctx,root,*,progress=None):
    from . import map_residency as residency
    collection=residency.resident(ctx.scene,root)
    if collection is None:raise ValueError('Load map geometry before preparing editable parts')
    before={value.as_pointer() for value in residency.all_ids()}
    try:return _prepare(ctx,root,progress=progress)
    finally:
        for value in residency.all_ids():
            if value.as_pointer() not in before and value.library is None:value[residency.TOKEN]=collection[residency.TOKEN]


def _prepare(ctx,root,*,progress=None):
    import bpy
    from . import city_editing as city,city_loose_parts as loose,compact_preview,map_residency as residency
    collection=residency.resident(ctx.scene,root)
    if collection is None:raise ValueError('Load map geometry before preparing editable parts')
    if ready(collection):return json.loads(collection[READY])
    wrapper_repairs=repair_legacy_wrappers(root,list(collection.all_objects))
    sources=geometry_sources(list(collection.all_objects))
    split_sources=0;new_parts=0
    for number,source in enumerate(sources):
        if progress:progress('Separating map geometry',number,max(len(sources),1))
        groups=loose.face_components(source.data)
        if len(groups)<=1:continue
        ctx.view_layer.objects.active=source
        steps=loose.split_city_steps(ctx,_groups=groups)
        try:
            while True:
                try:
                    done,total=next(steps)
                    if progress:progress('Separating map geometry',done,total)
                except StopIteration as completed:
                    new_parts+=completed.value;break
        finally:steps.close()
        split_sources+=1
    groups=[o for o in collection.all_objects if o.get(compact_preview.RECORDS)]
    parents={o.get('wz09_library_object_id'):o for o in [root,*collection.all_objects] if o.get('wz09_library_object_id')}
    prior_hidden=collection.hide_viewport
    collection.hide_viewport=True
    expanded=0;expanded_objects=[]
    try:
        for number,group in enumerate(groups):
            if progress:progress('Preparing individual prop placements',number,len(groups))
            before=set(bpy.data.objects)
            try:made=compact_preview.restore(group,_parents_by_id=parents,_remove_group=False)
            except BaseException:
                added=set(bpy.data.objects)-before
                if added:bpy.data.batch_remove(ids=added)
                raise
            expanded_objects.extend(made)
            for obj in made:
                if not obj.get('wz09_library_object_id'):obj['wz09_library_object_id']=str(uuid.uuid4())
                obj[residency.TOKEN]=collection[residency.TOKEN]
                obj['wz09_individual_asset']=True
            expanded+=len(made)
        if groups:bpy.data.batch_remove(ids=groups)
    except BaseException:
        if expanded_objects:bpy.data.batch_remove(ids=expanded_objects)
        raise
    finally:collection.hide_viewport=prior_hidden
    bpy.context.view_layer.update()
    remaining=[o for o in collection.all_objects if o.get(compact_preview.RECORDS)]
    if remaining:raise ValueError('Some native prop groups are still instanced')
    identities=[o.get('wz09_library_object_id') for o in collection.all_objects if o.get('wz09_library_object_id')]
    if len(identities)!=len(set(identities)):raise ValueError('Prepared map contains duplicate persistent object IDs')
    result=dict(format=FORMAT,split_sources=split_sources,new_parts=new_parts,wrapper_identity_repairs=wrapper_repairs,
        geometry_parts=sum(bool(o.get(city.PART)) for o in collection.all_objects),expanded_prop_placements=expanded,
        geometry_objects=sum(bool(o.get(city.PART)) for o in collection.all_objects)+len(geometry_sources(collection.all_objects)),
        mesh_objects=sum(o.type=='MESH' and not o.get('wz09_editor_overlay') and not o.get('wz09_native_instance_prototype') for o in collection.all_objects),
        unique_meshes=len({o.data for o in collection.all_objects if o.type=='MESH'}),shared_prop_meshes=True)
    collection[READY]=json.dumps(result)
    ctx.view_layer.objects.active=root
    return result


def ensure_saved(ctx,root,*,cache_source=None):
    from . import map_residency as residency
    collection=residency.resident(ctx.scene,root)
    if ready(collection):
        for obj in collection.all_objects:
            if obj.get('wz09_compacted_source_parts'):del obj['wz09_compacted_source_parts']
        return root
    manager=ctx.window_manager;workspace=getattr(ctx,'workspace',None)
    manager.progress_begin(0,100)
    def progress(message,done,total):
        manager.progress_update(done*100/max(total,1))
        if workspace:workspace.status_text_set(message+f': {done}/{total}')
    try:
        prepare(ctx,root,progress=progress)
        objects=set(collection.all_objects)
        directory=residency.project_root()/'.local/editable_parts/libraries'
        record=residency.write_library(ctx.scene,root,objects,directory)
        root[residency.LIBRARY]=json.dumps(record)
        if cache_source is not None:publish_cache(cache_source,record,residency.project_root())
        return root
    finally:
        manager.progress_end()
        if workspace:workspace.status_text_set(None)
