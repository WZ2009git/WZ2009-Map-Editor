"""Editable static-map imports without Blender's global append remapping.

Link once, copy the dependency closure, remap only those copies, then release
the temporary links. Source files and previously existing data are untouched.
"""
import json
import time


class UnsupportedLibrary(ValueError):
    pass


def dependency_closure(seeds,users):
    seeds=list(seeds)
    if not seeds:return set()
    dependencies={}
    for dependency,owners in users.items():
        for owner in owners:dependencies.setdefault(owner,set()).add(dependency)
    result=set();queue=seeds
    while queue:
        item=queue.pop()
        if item in result:continue
        result.add(item);queue.extend(dependencies.get(item,()))
    return result


def private_temporary_ids(closure,users,local_ids,previous_ids):
    """Exclude existing data and dependencies visible through foreign owners."""
    allowed=closure|local_ids
    shared={value for value in closure if value in previous_ids or users.get(value,set())-allowed}
    if not shared:return set(closure)
    return closure-(dependency_closure(shared,users)&closure)


def _properties(value,mapping,bpy):
    try:keys=list(value.keys())
    except (AttributeError,TypeError):return
    for key in keys:
        item=value[key]
        if isinstance(item,bpy.types.ID):
            if item in mapping:value[key]=mapping[item]
        elif hasattr(item,'keys'):_properties(item,mapping,bpy)


_SKIP={'rna_type','id_data','original','library','override_library','asset_data',
       'users_collection','users_scene','children','children_recursive','all_objects',
       'vertices','edges','loops','polygons','loop_triangles','attributes','uv_layers',
       'color_attributes','vertex_colors','vertex_normals','polygon_normals','corner_normals',
       'skin_vertices','edit_bones','bones','splines','points','pixels','packed_files',
       'evaluated_geometry','data','materials','material_slots','active_material'}
_RNA_FIELDS={}


def _rna(value,mapping,bpy,seen):
    if value is None or not hasattr(value,'bl_rna'):return
    pointer=value.as_pointer()
    if pointer in seen:return
    seen.add(pointer);_properties(value,mapping,bpy)
    kind=value.bl_rna.as_pointer()
    fields=_RNA_FIELDS.get(kind)
    if fields is None:
        fields=tuple((p.identifier,p.type,p.is_readonly) for p in value.bl_rna.properties
                     if p.identifier not in _SKIP and p.type in {'POINTER','COLLECTION'})
        _RNA_FIELDS[kind]=fields
    for field,property_type,readonly in fields:
        item=getattr(value,field)
        if property_type=='POINTER':
            if isinstance(item,bpy.types.ID):
                replacement=mapping.get(item)
                if replacement is not None and replacement!=item:
                    if readonly:
                        if item.is_embedded_data:_rna(item,mapping,bpy,seen)
                        else:raise UnsupportedLibrary('Read-only dependency cannot be copied: '+field)
                    else:setattr(value,field,replacement)
                elif item is not None and item.is_embedded_data:_rna(item,mapping,bpy,seen)
            else:_rna(item,mapping,bpy,seen)
        else:
            for child in item:
                if not isinstance(child,bpy.types.ID):_rna(child,mapping,bpy,seen)


def _modifier_inputs(obj,mapping,bpy):
    for modifier in obj.modifiers:
        if modifier.type!='NODES' or not modifier.node_group:continue
        for socket in modifier.node_group.interface.items_tree:
            if socket.item_type!='SOCKET' or socket.in_out!='INPUT':continue
            if hasattr(modifier,'properties'):
                entry=getattr(modifier.properties.inputs,socket.identifier,None)
                if entry is not None:
                    value=getattr(entry,'value',None)
                    if isinstance(value,bpy.types.ID) and value in mapping:entry.value=mapping[value]
            else:
                value=modifier.get(socket.identifier)
                if isinstance(value,bpy.types.ID) and value in mapping:modifier[socket.identifier]=mapping[value]


def append_editable(path,collection_name,all_ids,progress=None,validate=None):
    import bpy
    started=time.perf_counter();before={v.as_pointer() for v in all_ids()}
    _RNA_FIELDS.clear()
    if progress:progress('link',0,1)
    with bpy.data.libraries.load(str(path),link=True) as (source,target):
        if collection_name not in source.collections:raise ValueError('Detail collection is missing')
        target.collections=[collection_name]
    source_collection=target.collections[0]
    names=json.loads(source_collection.get('wz09_library_baselines_json','[]'))
    baselines=[]
    if names:
        with bpy.data.libraries.load(str(path),link=True) as (source,target):target.meshes=names
        baselines=target.meshes
        if any(v is None for v in baselines):raise ValueError('A retained baseline is missing')
    linked=time.perf_counter()
    users=bpy.data.user_map()
    closure=dependency_closure([source_collection,*baselines],users)
    overrides={owner for dependency,owners in users.items() if isinstance(dependency,bpy.types.Material)
               for owner in owners if isinstance(owner,bpy.types.Object) and owner in closure}
    supported=(bpy.types.Object,bpy.types.Mesh,bpy.types.Collection,bpy.types.Material,bpy.types.Image,bpy.types.NodeTree)
    unsupported=[type(v).__name__ for v in closure if not isinstance(v,supported)]
    if unsupported:raise UnsupportedLibrary('Unsupported library dependencies: '+', '.join(sorted(set(unsupported))))
    if any(isinstance(v,bpy.types.Object) and (v.type not in {'MESH','EMPTY'} or v.animation_data) for v in closure):
        raise UnsupportedLibrary('Animated or non-static objects require the compatibility loader')
    if validate:validate('source',closure,None)
    mapping={}
    for index,old in enumerate(closure):
        if progress and index%128==0:progress('copy',index,len(closure))
        made=old.copy()
        if made.library is not None:raise UnsupportedLibrary('Blender did not create a local copy: '+old.name)
        mapping[old]=made
        if isinstance(old,bpy.types.Material):
            # Blender renames copied IDs to avoid namespace collisions. Keep
            # legacy geometry fingerprints independent of that display suffix.
            made['wz09_fingerprint_material_name']=old.get('wz09_fingerprint_material_name',old.name)
        if isinstance(old,bpy.types.Material) and old.node_tree:
            mapping[old.node_tree]=made.node_tree
        if isinstance(old,bpy.types.Image) and old.filepath:
            made.filepath=bpy.path.abspath(old.filepath,library=old.library)
    copied=time.perf_counter()
    from .material_palette import compact_copy
    palettes={};generated=set();compacted=0
    override_meshes={o.data for o in overrides if o.data is not None}
    seen=set()
    rewrite_types={}
    for index,old in enumerate(closure):
        if progress and index%128==0:progress('remap',index,len(closure))
        item_started=time.perf_counter()
        made=mapping[old]
        if isinstance(old,bpy.types.Collection):
            for obj in list(made.objects):made.objects.unlink(obj)
            for child in list(made.children):made.children.unlink(child)
            for obj in old.objects:made.objects.link(mapping[obj])
            for child in old.children:made.children.link(mapping[child])
        if isinstance(old,bpy.types.Object):
            if old.data is not None:made.data=mapping[old.data]
            if old in overrides:
                for slot_index,slot in enumerate(old.material_slots):
                    if slot.link=='OBJECT' and slot.material is not None:
                        target_material=mapping.get(slot.material,slot.material)
                        if made.material_slots[slot_index].material!=target_material:
                            made.material_slots[slot_index].material=target_material
        if isinstance(old,bpy.types.Mesh):
            if old.shape_keys:raise UnsupportedLibrary('Shape-key meshes require the compatibility loader')
            compacted_here=old not in override_meshes and compact_copy(old,made,mapping,palettes,generated,bpy)
            if compacted_here:compacted+=1
            else:
                for index,material in enumerate(old.materials):
                    if material is not None:
                        target_material=mapping.get(material,material)
                        if made.materials[index]!=target_material:made.materials[index]=target_material
        if isinstance(old,(bpy.types.Mesh,bpy.types.Collection,bpy.types.Image)):
            _properties(made,mapping,bpy)
        elif isinstance(old,bpy.types.Material):
            _properties(made,mapping,bpy)
            if made.node_tree:_rna(made.node_tree,mapping,bpy,seen)
        else:_rna(made,mapping,bpy,seen)
        if isinstance(old,bpy.types.Object):_modifier_inputs(made,mapping,bpy)
        kind=type(old).__name__;rewrite_types[kind]=rewrite_types.get(kind,0)+time.perf_counter()-item_started
    collection=mapping[source_collection]
    collection['wz09_library_baselines_json']=json.dumps([mapping[m].name for m in baselines])
    for mesh in baselines:mapping[mesh].use_fake_user=True
    if validate:validate('copy',closure,mapping)
    remapped=time.perf_counter()
    if progress:progress('cleanup',0,1)
    local=set(mapping.values())|generated
    final_users=bpy.data.user_map()
    remaining={dependency:owners & local for dependency,owners in final_users.items()
               if dependency in closure and owners & local}
    if remaining:
        kinds=sorted({type(value).__name__ for value in remaining})
        raise UnsupportedLibrary('Unmapped native dependencies: '+', '.join(kinds))
    # Delete only temporary links introduced by this call, never prior scene IDs.
    temporary=[v for v in all_ids() if v.as_pointer() not in before and v not in local]
    if temporary:
        private=private_temporary_ids(set(temporary),final_users,local,set())
        if private:bpy.data.batch_remove(ids=private)
    elapsed=time.perf_counter()-started
    return collection,dict(mode='linked_local_copy',seconds=elapsed,link_seconds=linked-started,
        copy_seconds=copied-linked,remap_seconds=remapped-copied,cleanup_seconds=elapsed-(remapped-started),
        source_ids=len(closure),local_ids=len(local),objects=len(collection.all_objects),
        object_material_overrides=len(overrides),compacted_meshes=compacted,palette_banks=len(generated),remap_types=rewrite_types)
