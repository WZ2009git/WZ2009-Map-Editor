"""Cached city overview; full editable originals remain in source collections."""
SOURCE='wz09_overview_source_map_id'
PROXY='wz09_overview_map_id'


def set_focus(scene, identity=None):
    import json
    records=json.loads(scene.get('wz09_city_edit_registry_json','{}'))
    states=json.loads(scene.get('wz09_city_view_states_json','{}'))
    edited={row['owner_map'] for kind in ('meshes','models') for row in records.get(kind,{}).values()}
    def visible(uid):
        state=states.get(uid)
        if state=='loaded':return True
        if state in ('unloaded','baked'):return False
        return identity in (uid,'*') or uid in edited
    for coll in scene.collection.children_recursive:
        uid=coll.get(SOURCE)
        if uid:
            coll.hide_viewport=not visible(uid)
    for obj in scene.objects:
        uid=obj.get(PROXY)
        if uid:
            obj.hide_set(visible(uid))
    scene['wz09_edit_focus_map_id']=identity or ''


def selected_owner(obj):
    while obj is not None:
        if obj.get(PROXY):return str(obj[PROXY])
        if obj.get('wz09_type')=='map_instance':return str(obj['wz09_id'])
        obj=obj.parent
    return None


def build(scene, progress=print):
    import bpy
    import numpy as np
    roots={str(o['wz09_id']):o for o in scene.objects if o.get('wz09_type')=='map_instance'}
    parent=next(c for c in scene.collection.children if c.name.startswith('WZ09_PREVIEWS'))
    memberships={}
    for coll in [scene.collection]+list(scene.collection.children_recursive):
        for obj in coll.objects:memberships.setdefault(obj.as_pointer(),[]).append(coll)
    cache={};groups={uid:[] for uid in roots}
    def owner(obj):
        chain=[]
        while obj:
            key=obj.as_pointer()
            if key in cache:result=cache[key];break
            chain.append(key)
            if obj.get('wz09_type')=='map_instance':result=str(obj['wz09_id']);break
            obj=obj.parent
        else:result=None
        for key in chain:cache[key]=result
        return result
    for obj in scene.objects:
        if obj.get('wz09_preview_only') and not obj.get('wz09_editor_overlay'):
            uid=owner(obj)
            if uid in groups:groups[uid].append(obj)
    proxies=bpy.data.collections.new('Current city overview cache')
    scene.collection.children.link(proxies);proxies.hide_render=True
    mat=bpy.data.materials.new('City overview · source architecture')
    mat.diffuse_color=(.58,.6,.57,1)
    total=0;source_meshes=0
    for uid,objects in groups.items():
        root=roots[uid];inverse=root.matrix_world.inverted()
        coords=[];loops=[];starts=[];totals=[];vo=lo=0
        for obj in objects:
            if obj.type!='MESH' or obj.get('wz09_compact_count') or obj.hide_get() or obj.hide_viewport:
                continue
            mesh=obj.data
            if not mesh.polygons:continue
            def array(collection,field,width=1):
                data=np.empty(len(collection)*width,dtype=np.float32 if field=='co' else np.int32)
                collection.foreach_get(field,data)
                return data
            co=array(mesh.vertices,'co',3).reshape((-1,3))
            matrix=np.array(inverse@obj.matrix_world)
            coords.append((co@matrix[:3,:3].T+matrix[:3,3]).astype(np.float32))
            loops.append(array(mesh.loops,'vertex_index')+vo)
            starts.append(array(mesh.polygons,'loop_start')+lo)
            totals.append(array(mesh.polygons,'loop_total'))
            vo+=len(mesh.vertices);lo+=len(mesh.loops);source_meshes+=1
        if not coords:raise ValueError('City has no architectural preview: '+uid)
        mesh=bpy.data.meshes.new('Overview · '+root['wz09_source_map'])
        mesh.vertices.add(vo);mesh.vertices.foreach_set('co',np.concatenate(coords).reshape(-1))
        mesh.loops.add(lo);mesh.loops.foreach_set('vertex_index',np.concatenate(loops))
        count=sum(len(p) for p in starts)
        mesh.polygons.add(count);mesh.polygons.foreach_set('loop_start',np.concatenate(starts))
        mesh.polygons.foreach_set('loop_total',np.concatenate(totals))
        mesh.update(calc_edges=True)
        if mesh.validate(clean_customdata=False):raise ValueError('Invalid merged preview')
        mesh.materials.append(mat)
        obj=bpy.data.objects.new(mesh.name,mesh);proxies.objects.link(obj);obj.parent=root
        obj[PROXY]=uid;obj['wz09_editor_overlay']=True;obj['wz09_preview_only']=True
        obj.hide_render=True
        coll=bpy.data.collections.new('Editable city · '+root['wz09_source_map'])
        parent.children.link(coll);coll[SOURCE]=uid
        for original in objects:
            coll.objects.link(original)
            for old in memberships.get(original.as_pointer(),[]):old.objects.unlink(original)
        total+=count
        progress('OVERVIEW '+root['wz09_source_map']+' faces='+str(count))
    set_focus(scene)
    result=dict(cities=len(groups),architectural_mesh_objects_batched=source_meshes,
        overview_objects=len(groups),architectural_faces=total,
        repeated_props='retained in originals; omitted from overview',
        originals_retained=True,collision_changed=False,game_started=False)
    return result
