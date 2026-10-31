"""Reversible viewport residency and material-preserving city preview caches.

This is editor visibility, not RAM eviction or a runtime streaming implementation.
"""
import hashlib
import json
import uuid

STATES='wz09_city_view_states_json'
SNAPSHOT='wz09_city_cache_source_digest'
OBJECT_ID='wz09_city_cache_object_id'


def preview_attribute_plan(captures):
    """Unify compatible colours; namespace incompatible derived-only channels."""
    variants={}
    for data in captures:
        for name,domain,kind,prop,values in data['attributes']:
            if domain=='EDGE' or name=='material_index':continue
            variants.setdefault(name,set()).add((domain,kind,prop,values.shape[1]))
    result={};reserved=set(variants)
    for name,schemas in sorted(variants.items()):
        if len(schemas)==1:
            result[name]=dict(schema=next(iter(schemas)),sources={(name,*next(iter(schemas)))})
        elif all(s[0] in {'POINT','CORNER'} and s[1] in {'BYTE_COLOR','FLOAT_COLOR'} and s[2:] == ('color',4) for s in schemas):
            result[name]=dict(schema=('CORNER','FLOAT_COLOR','color',4),sources={(name,*s) for s in schemas})
        else:
            for schema in sorted(schemas):
                alias=name+'__'+schema[0]+'_'+schema[1]
                while alias in reserved:alias+='_'
                reserved.add(alias)
                result[alias]=dict(schema=schema,sources={(name,*schema)})
    return result


def preview_attribute_values(data,attribute,np):
    domain,kind,prop,width=attribute['schema']
    for name,source_domain,source_kind,source_prop,values in data['attributes']:
        if (name,source_domain,source_kind,source_prop,values.shape[1]) not in attribute['sources']:continue
        if source_domain=='POINT' and domain=='CORNER':return values[data['vertices']]
        return values
    count={'POINT':len(data['co']),'FACE':len(data['totals']),'CORNER':len(data['vertices'])}[domain]
    dtype=np.bool_ if kind=='BOOLEAN' else np.float32 if kind in {'FLOAT','FLOAT_VECTOR','FLOAT2','FLOAT_COLOR','BYTE_COLOR'} else np.int32
    return np.zeros((count,width),dtype=dtype)


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
        if obj.get('wz09_compact_records_json') or obj.get('wz09_individual_asset'):
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
        if abs(np.linalg.det(matrix[:3,:3]))<=1e-12:
            raise ValueError('Singular geometry cannot be baked into a map preview: '+obj.name)
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
        slots={};vo=lo=0
        # Union schemas first; absent UV/colour channels receive zero defaults.
        attributes=preview_attribute_plan(row[2] for row in entries)
        for attribute in attributes.values():attribute['values']=[]
        for _,obj,data,matrix in entries:
            # A mirrored prop is valid source geometry. Reverse only the derived
            # preview's corner order, including UVs, colours and split normals.
            mirrored=np.linalg.det(matrix[:3,:3])<0
            order=(np.repeat(data['starts']*2+data['totals']-1,data['totals'])-np.arange(len(data['vertices']))) if mirrored else None
            coords.append((data['co']@matrix[:3,:3].T+matrix[:3,3]).astype(np.float32))
            loops.append((data['vertices'][order] if mirrored else data['vertices'])+vo);starts.append(data['starts']+lo)
            totals.append(data['totals']);smooth.append(data['smooth'])
            normal=data['normals']@np.linalg.inv(matrix[:3,:3])
            length=np.linalg.norm(normal,axis=1,keepdims=True)
            normal=(normal/np.maximum(length,1e-20)).astype(np.float32)
            normals.append(normal[order] if mirrored else normal)
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
            for name,attribute in attributes.items():
                values=preview_attribute_values(data,attribute,np)
                if mirrored and attribute['schema'][0]=='CORNER':values=values[order]
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
        proxy['wz09_preview_mirrored_inputs']=int(sum(np.linalg.det(row[3][:3,:3])<0 for row in entries))
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
