"""Reversible viewport residency and material-preserving city preview caches.

This is editor visibility, not RAM eviction or a runtime streaming implementation.
"""
import hashlib
import json
import uuid

STATES='wz09_city_view_states_json'
SNAPSHOT='wz09_city_cache_source_digest'
OBJECT_ID='wz09_city_cache_object_id'


def states(scene):
    return json.loads(scene.get(STATES,'{}'))


def _collection(scene, identity):
    matches=[c for c in scene.collection.children_recursive if c.get('wz09_overview_source_map_id')==identity]
    if len(matches)!=1:
        raise ValueError('No unique source collection found for this map')
    return matches[0]


def _inputs(scene, root):
    import numpy as np
    from .city_loose_parts import _capture
    collection=_collection(scene,str(root['wz09_id']))
    inverse=root.matrix_world.inverted()
    entries=[];seen=set();digest=hashlib.sha256()
    for obj in collection.all_objects:
        if obj.type!='MESH' or not obj.data.polygons or obj.get('wz09_editor_overlay'):
            continue
        if obj.get('wz09_compact_records_json'):
            continue  # Repeated detail props remain in the editable source only.
        if obj.hide_viewport or obj.hide_get():
            continue
        if obj.modifiers or obj.constraints or obj.animation_data:
            raise ValueError('Preview currently supports static geometry without modifiers: '+obj.name)
        identity=obj.get(OBJECT_ID)
        if not identity or identity in seen:
            identity=str(uuid.uuid4());obj[OBJECT_ID]=identity
        seen.add(identity)
        matrix=np.array(inverse@obj.matrix_world,dtype=np.float64)
        if np.linalg.det(matrix[:3,:3])<=1e-12:
            raise ValueError('Mirrored or singular geometry cannot be silently baked')
        captured=_capture(obj.data)
        entries.append((identity,obj,captured,matrix))
    entries.sort(key=lambda row:row[0])
    for identity,obj,captured,matrix in entries:
        digest.update(identity.encode())
        digest.update(np.round(matrix,5).tobytes())
        used=next((row[4].ravel() for row in captured['attributes'] if row[0]=='material_index'),np.zeros(1,dtype=np.int32))
        names=[obj.material_slots[int(i)].material.name_full if obj.material_slots and obj.material_slots[int(i)].material else None for i in np.unique(used)]
        digest.update(json.dumps(names).encode())
        for name in ('co','vertices','starts','totals','smooth','normals'):
            digest.update(captured[name].tobytes())
        for name,domain,kind,prop,values in sorted(captured['attributes'],key=lambda a:a[0]):
            digest.update(json.dumps([name,domain,kind]).encode());digest.update(values.tobytes())
    if not entries:
        raise ValueError('No visible static map geometry available to bake')
    return entries,digest.hexdigest()


def load_assets(ctx):
    from . import city_editing as city,overview_preview
    root=city.map_root(ctx.active_object);uid=str(root['wz09_id'])
    from . import map_residency
    if root.get(map_residency.LIBRARY): return map_residency.load(ctx, root)
    _collection(ctx.scene,uid)
    values=states(ctx.scene);values[uid]='loaded';ctx.scene[STATES]=json.dumps(values)
    overview_preview.set_focus(ctx.scene,uid)
    return root


def unload_assets(ctx):
    from . import city_editing as city,overview_preview
    root=city.map_root(ctx.active_object);uid=str(root['wz09_id'])
    from . import map_residency
    if root.get(map_residency.LIBRARY): return map_residency.unload(ctx, root)
    records=city.registry(ctx.scene)
    edited=any(row['owner_map']==uid for kind in ('meshes','models') for row in records[kind].values())
    proxies=[o for o in ctx.scene.objects if o.get(overview_preview.PROXY)==uid]
    baked=next((o for o in proxies if o.get(SNAPSHOT)),None)
    if baked:
        _,snapshot=_inputs(ctx.scene,root)
        if snapshot!=baked[SNAPSHOT]:
            raise ValueError('Geometry changed after baking: rebuild the map preview first')
    elif edited:
        raise ValueError('Save the edited map with Rebuild map preview first')
    elif not proxies:
        raise ValueError('No preview available: rebuild the map preview first')
    values=states(ctx.scene);values[uid]='unloaded';ctx.scene[STATES]=json.dumps(values)
    overview_preview.set_focus(ctx.scene)
    return root


def bake(ctx):
    import bpy
    import numpy as np
    from . import city_editing as city,overview_preview
    root=city.map_root(ctx.active_object);uid=str(root['wz09_id'])
    entries,snapshot=_inputs(ctx.scene,root)
    vertex_count=sum(len(row[2]['co']) for row in entries)
    loop_count=sum(len(row[2]['vertices']) for row in entries)
    face_count=sum(len(row[2]['totals']) for row in entries)
    mesh=bpy.data.meshes.new('Gebackene Stadtvorschau')
    proxy=None
    try:
        coords=[];loops=[];starts=[];totals=[];smooth=[];normals=[];materials=[];material_ids=[]
        slots={};attributes={};vo=lo=0
        # Union schemas first; absent UV/colour channels receive zero defaults.
        for _,obj,data,matrix in entries:
            for name,domain,kind,prop,values in data['attributes']:
                if domain=='EDGE' or name=='material_index':continue
                key=(domain,kind,prop,values.shape[1])
                if name in attributes and attributes[name]['schema']!=key:
                    raise ValueError('Inconsistent preview attribute: '+name)
                attributes.setdefault(name,dict(schema=key,values=[],dtype=values.dtype))
        for _,obj,data,matrix in entries:
            coords.append((data['co']@matrix[:3,:3].T+matrix[:3,3]).astype(np.float32))
            loops.append(data['vertices']+vo);starts.append(data['starts']+lo)
            totals.append(data['totals']);smooth.append(data['smooth'])
            normal=data['normals']@np.linalg.inv(matrix[:3,:3])
            length=np.linalg.norm(normal,axis=1,keepdims=True)
            normals.append((normal/np.maximum(length,1e-20)).astype(np.float32))
            old=np.empty(len(obj.data.polygons),dtype=np.int32)
            obj.data.polygons.foreach_get('material_index',old)
            used,local=np.unique(old,return_inverse=True)
            remap=[]
            for index in used:
                if obj.material_slots and index>=len(obj.material_slots):raise ValueError('Invalid material index')
                material=obj.material_slots[int(index)].material if obj.material_slots else None
                key=material.as_pointer() if material else 0
                if key not in slots:slots[key]=len(materials);materials.append(material)
                remap.append(slots[key])
            material_ids.append(np.asarray(remap,dtype=np.int32)[local])
            available={row[0]:row for row in data['attributes']}
            for name,attribute in attributes.items():
                domain,kind,prop,width=attribute['schema']
                count={'POINT':len(data['co']),'FACE':len(data['totals']),'CORNER':len(data['vertices'])}[domain]
                values=available[name][4] if name in available else np.zeros((count,width),dtype=attribute['dtype'])
                attribute['values'].append(values)
            vo+=len(data['co']);lo+=len(data['vertices'])
        mesh.vertices.add(vertex_count);mesh.vertices.foreach_set('co',np.concatenate(coords).ravel())
        mesh.loops.add(loop_count);mesh.loops.foreach_set('vertex_index',np.concatenate(loops))
        mesh.polygons.add(face_count);mesh.polygons.foreach_set('loop_start',np.concatenate(starts))
        mesh.polygons.foreach_set('loop_total',np.concatenate(totals))
        mesh.polygons.foreach_set('use_smooth',np.concatenate(smooth))
        for material in materials:mesh.materials.append(material)
        mesh.update(calc_edges=True)
        for name,attribute in attributes.items():
            domain,kind,prop,width=attribute['schema'];values=np.concatenate(attribute['values']).ravel()
            if kind=='FLOAT2' and domain=='CORNER':
                layer=mesh.uv_layers.new(name=name);layer.data.foreach_set('uv',values)
            else:
                target=mesh.attributes.get(name) or mesh.attributes.new(name,kind,domain)
                target.data.foreach_set(prop,values)
        mesh.polygons.foreach_set('material_index',np.concatenate(material_ids))
        mesh.normals_split_custom_set(np.concatenate(normals));mesh.update()
        proxy=bpy.data.objects.new('Stadtvorschau · '+str(root['wz09_source_map']),mesh)
        proxy.parent=root;proxy[overview_preview.PROXY]=uid
        proxy['wz09_editor_overlay']=True;proxy['wz09_preview_only']=True
        proxy[SNAPSHOT]=snapshot;proxy['wz09_cache_mesh_objects']=len(entries)
        proxy.hide_render=True
        old=[o for o in ctx.scene.objects if o.get(overview_preview.PROXY)==uid]
        collection=old[0].users_collection[0] if old else ctx.scene.collection
        collection.objects.link(proxy)
        for obj in old:
            previous=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if previous.users==0:bpy.data.meshes.remove(previous)
        values=states(ctx.scene);values[uid]='baked';ctx.scene[STATES]=json.dumps(values)
        overview_preview.set_focus(ctx.scene)
        bpy.ops.object.select_all(action='DESELECT');proxy.select_set(True);ctx.view_layer.objects.active=proxy
        return dict(source_objects=len(entries),preview_objects=1,faces=face_count,materials=len(materials),
                    source_digest=snapshot,originals_retained=True,runtime_integrated=False)
    except Exception:
        if proxy is not None:bpy.data.objects.remove(proxy,do_unlink=True)
        if mesh.users==0:bpy.data.meshes.remove(mesh)
        raise
