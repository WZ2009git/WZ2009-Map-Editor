"""Reference-only native mesh recipes; native vertex/UV payloads are not saved.

This module runs inside Blender. Native geometry comes exclusively from the
user's pinned local glTF export. Recipes select triangles and describe edits.
"""
import hashlib
import json
from pathlib import Path
import re
import uuid


def plain(value):
    if hasattr(value,'to_dict'): value=value.to_dict()
    if hasattr(value,'to_list'): value=value.to_list()
    if isinstance(value,dict): return {k:plain(v) for k,v in value.items() if not k.endswith('_path')}
    if isinstance(value,(list,tuple)): return [plain(v) for v in value]
    if isinstance(value,str):
        if re.match(r'^[A-Za-z]:[/\\]|^[/\\]{2}',value): return None
        return value
    if isinstance(value,(int,float,bool)) or value is None: return value
    return None


def read(items,field,width=1,dtype=None):
    import numpy as np
    values=np.empty((len(items),width),dtype=dtype or np.float32)
    items.foreach_get(field,values.ravel())
    return values


def triangles(mesh,offset=(0,0,0)):
    import numpy as np
    mesh.calc_loop_triangles()
    positions=read(mesh.vertices,'co',3).astype(np.float64)+offset
    indices=read(mesh.loop_triangles,'vertices',3,np.int32)
    vertices=positions[indices]
    order=np.lexsort((vertices[:,:,2],vertices[:,:,1],vertices[:,:,0]),axis=1)
    return np.take_along_axis(vertices,order[:,:,None],axis=1)


def match_faces(candidate,target,offset=(0,0,0),tolerance=.004):
    """Return candidate polygon IDs; reject unmatched or ambiguous edits."""
    import numpy as np
    a=triangles(candidate); b=triangles(target,offset)
    def error_for(first,second):
        distances=np.abs(first[..., :,None,:]-second[...,None,:,:]).max(axis=-1)
        return np.maximum(distances.min(axis=-1).max(axis=-1),distances.min(axis=-2).max(axis=-1))
    if not len(b): return [],0.
    if len(a)==len(b):
        maximum=float(error_for(a,b).max())
        if maximum<=tolerance:
            ids=read(candidate.loop_triangles,'polygon_index',1,np.int32).ravel()
            return ids.tolist(),maximum
    # Most retained subsets are exact native triangles. Avoid a Python search
    # per triangle; different clipping diagonals use source-corner references.
    def packed(values):
        quantized=np.rint(values*100).astype(np.int64)
        order=np.lexsort((quantized[:,:,2],quantized[:,:,1],quantized[:,:,0]),axis=1)
        return np.ascontiguousarray(np.take_along_axis(quantized,order[:,:,None],axis=1)).reshape((-1,9)).view('V72').ravel()
    unique,first=np.unique(packed(a),return_index=True)
    positions=np.searchsorted(unique,packed(b)); valid=positions<len(unique)
    if valid.all() and (unique[positions]==packed(b)).all():
        chosen=first[positions]; errors=error_for(a[chosen],b)
        if float(errors.max())<=tolerance:
            polygon=read(candidate.loop_triangles,'polygon_index',1,np.int32).ravel()
            return polygon[chosen].tolist(),float(errors.max())
    raise ValueError('Native triangle selection requires source-corner mapping')


def metadata(obj):
    node=obj
    while node:
        value=node.get('iw4l')
        if hasattr(value,'get') and value.get('model'): return plain(value)
        record=node.get('wz09_asset_record_json')
        if record:
            asset=json.loads(record).get('asset',{})
            if asset.get('model'): return dict(model=asset['model'],source_map=asset.get('source_map'),source_sha256=asset.get('source_sha256'))
        node=node.parent
    return None


def import_reference(scene,path,supplemental=(),primary_code=None):
    import bpy
    from wz2009_map_editor import preview_geometry
    bpy.context.window.scene=scene
    before=set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    objects=set(bpy.data.objects)-before
    preview_geometry.repair_preview_winding(scene,objects=objects)
    world=max((o for o in objects if o.type=='MESH'),key=lambda o:len(o.data.polygons))
    object_codes={o:primary_code for o in objects}; primary_objects=set(objects)
    for extra in supplemental:
        extra_path,extra_code=extra if isinstance(extra,tuple) else (extra,primary_code)
        previous=set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(extra_path))
        added=set(bpy.data.objects)-previous
        preview_geometry.repair_preview_winding(scene,objects=added)
        objects.update(added)
        object_codes.update({o:extra_code for o in added})
    by_slot={}; models={}; semantic_models={}; models_by_code={}; semantic_by_code={}
    for obj in objects:
        info=metadata(obj)
        if info and obj.type=='MESH':
            models.setdefault(info['model'],obj)
            models_by_code.setdefault((object_codes[obj],info['model']),obj)
            if info.get('source_kind'):
                semantic_models.setdefault(info['model'],obj)
                semantic_by_code.setdefault((object_codes[obj],info['model']),obj)
            if obj in primary_objects and info.get('authored_slot') is not None: by_slot[info['authored_slot']]=obj
    owner=bpy.data.objects.new('Native local coordinate frame',None); scene.collection.objects.link(owner)
    bpy.context.view_layer.update()
    return dict(objects=objects,world=world,by_slot=by_slot,models=models,semantic_models=semantic_models,object_codes=object_codes,
                models_by_code=models_by_code,semantic_by_code=semantic_by_code,primary_code=primary_code,owner=owner)


def candidate_for(obj,reference,boundary,cache,*,clipped=True):
    import bpy
    from wz2009_map_editor.fast_preview_clip import clipped_mesh
    info=metadata(obj)
    if info:
        code=info.get('source_map') or reference.get('primary_code')
        original=(reference['models_by_code'].get((code,info['model'])) if code!=reference.get('primary_code') else None)
        original=original or (reference['by_slot'].get(info.get('authored_slot')) if info.get('authored_slot') is not None else None)
        if original is None and info.get('source_kind'): original=reference['semantic_models'].get(info['model'])
        original=original or reference['models'].get(info['model'])
        if original is None: raise ValueError('Native model reference missing: '+info['model'])
        key=('model',info.get('authored_slot'),info['model'],code,info.get('source_kind'))
    else: original=reference['world']; key=('world',)
    key=(*key,clipped)
    if key not in cache:
        cache[key]=clipped_mesh(original,reference['owner'],boundary) if clipped else original.data.copy()
    return cache[key],dict(role=key[0],model=info['model'] if info else None,authored_slot=info.get('authored_slot') if info else None,
                          source_map=info.get('source_map') or reference.get('primary_code') if info else reference.get('primary_code'),
                          source_kind=info.get('source_kind') if info else None,clipped=clipped)


def match_corners(candidate,target,offset=(0,0,0),tolerance=.004):
    """Reference source corners when equivalent polygons use another diagonal."""
    import numpy as np
    a=read(candidate.vertices,'co',3).astype(np.float64)
    b=read(target.vertices,'co',3).astype(np.float64)+offset
    source_loops=read(candidate.loops,'vertex_index',1,np.int32).ravel()
    target_loops=read(target.loops,'vertex_index',1,np.int32).ravel()
    auv=read(candidate.uv_layers[0].data,'uv',2) if candidate.uv_layers else np.zeros((len(source_loops),2))
    buv=read(target.uv_layers[0].data,'uv',2) if target.uv_layers else np.zeros((len(target_loops),2))
    def packed(co,uv):
        values=np.c_[np.rint(co*100),np.rint(uv*1000)].astype(np.int64)
        return np.ascontiguousarray(values).view('V40').ravel()
    source_keys,first=np.unique(packed(a[source_loops],auv),return_index=True)
    target_keys=packed(b[target_loops],buv)
    indices=np.searchsorted(source_keys,target_keys); valid=indices<len(source_keys)
    valid[valid]&=source_keys[indices[valid]]==target_keys[valid]
    result=np.full(len(target_loops),-1,np.int32); result[valid]=first[indices[valid]]
    chosen=np.flatnonzero(valid)
    errors=np.abs(a[source_loops[result[valid]]]-b[target_loops[valid]]).max(axis=1)
    invalid=chosen[errors>tolerance]; result[invalid]=-1
    maximum=float(errors[errors<=tolerance].max()) if (errors<=tolerance).any() else 0.
    missing=np.flatnonzero(result<0)
    if not len(missing):
        return corner_result(candidate,target,result),maximum
    buckets={}
    for index,key in enumerate(np.floor(a*100).astype(np.int64)): buckets.setdefault(tuple(key),[]).append(index)
    corners={}
    for index,vertex in enumerate(source_loops): corners.setdefault(int(vertex),[]).append(index)
    options={}
    for vertex in np.unique(target_loops[missing]):
        point=b[vertex]
        key=np.floor(point*100).astype(np.int64)
        chosen=[]
        for x in (-1,0,1):
            for y in (-1,0,1):
                for z in (-1,0,1):
                    for index in buckets.get(tuple(key+(x,y,z)),[]):
                        error=float(np.abs(a[index]-point).max())
                        if error<=tolerance: chosen.append(index); maximum=max(maximum,error)
        if not chosen: raise ValueError('Native recipe has an unmatched vertex; no coordinate payload is exported')
        options[int(vertex)]=chosen
    for index in missing:
        vertex=target_loops[index]
        possible=[corner for v in options[int(vertex)] for corner in corners.get(v,[])]
        if auv is not None and buv is not None:
            possible=[corner for corner in possible if float(np.abs(auv[corner]-buv[index]).max())<=.002]
        if not possible: raise ValueError('Native recipe has an unmatched UV corner')
        result[index]=possible[0]
    return corner_result(candidate,target,result),maximum


def corner_result(candidate,target,result):
    import numpy as np
    source_names={re.sub(r'\.\d{3}$','',m.name):i for i,m in enumerate(candidate.materials) if m}
    material_map=[]
    for material in target.materials:
        name=re.sub(r'\.\d{3}$','',material.name) if material else None
        if name not in source_names: raise ValueError('Native recipe material is not bound to its source')
        material_map.append(source_names[name])
    material_indices=np.asarray(material_map,np.int32)[read(target.polygons,'material_index',1,np.int32).ravel()]
    return dict(corner_indices=np.asarray(result,np.int32),
                polygon_sizes=read(target.polygons,'loop_total',1,np.int32).ravel(),
                material_indices=np.asarray(material_indices,np.int32))


def fit_mesh_frame(candidate,target,offset):
    """Recover a baked asset normalization as a small explicit affine edit."""
    import numpy as np
    a=read(candidate.vertices,'co',3).astype(np.float64)
    b=read(target.vertices,'co',3).astype(np.float64)+offset
    full=a
    if len(candidate.loops)==len(target.loops) and len(candidate.loops)>=4:
        a=a[read(candidate.loops,'vertex_index',1,np.int32).ravel()]
        b=b[read(target.loops,'vertex_index',1,np.int32).ravel()]
    elif len(a)!=len(b) or len(a)<4: return None,None
    fit,_,rank,_=np.linalg.lstsq(np.c_[a,np.ones(len(a))],b,rcond=None)
    if rank<4 or float(np.abs(np.c_[a,np.ones(len(a))]@fit-b).max())>.003: return None,None
    matrix=np.eye(4); matrix[:3,:]=fit.T
    if abs(np.linalg.det(matrix[:3,:3]))<1e-10: return None,None
    made=candidate.copy()
    normals=read(candidate.corner_normals,'vector',3).astype(np.float64)@np.linalg.inv(matrix[:3,:3])
    normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-20)
    made.vertices.foreach_set('co',(np.c_[full,np.ones(len(full))]@fit).astype(np.float32).ravel())
    made.update(); made.normals_split_custom_set(normals)
    return made,matrix.tolist()


def capture_map(root,collection,reference,boundary,parts,output):
    import bpy
    import numpy as np
    from wz2009_map_editor.compact_preview import RECORDS
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    objects=list(collection.all_objects)
    identity={obj:obj.get('wz09_library_object_id') or str(uuid.uuid4()) for obj in objects}
    if len(set(identity.values()))!=len(identity):
        raise ValueError('Detail library contains duplicate persistent object IDs: '+str(root.get('wz09_id')))
    cache={}; records=[]; total=0; maximum=0.; failures=[]
    fields=('wz09_preview_id','wz09_library_object_id','wz09_preview_only','wz09_city_edit_source_id',
            'wz09_city_edit_part_id','wz09_city_part_source','wz09_retained_source_root')
    for obj in objects:
        row=dict(id=identity[obj],parent=identity.get(obj.parent),name=obj.name,
                 matrix_basis=[list(r) for r in obj.matrix_basis],
                 matrix_parent_inverse=[list(r) for r in obj.matrix_parent_inverse],
                 properties={k:plain(obj[k]) for k in fields if k in obj},hidden=bool(obj.hide_viewport))
        info=metadata(obj)
        if obj.get('iw4l') is not None: row['properties']['iw4l']=plain(obj['iw4l'])
        if obj.get('wz09_library_root_id'): row['assembly_root']=True
        if obj.type=='MESH' and obj.get(RECORDS):
            rows=json.loads(obj[RECORDS]); model=rows[0].get('properties',{}).get('iw4l',{}).get('model')
            if not model: raise ValueError('Instance group has no native model identity')
            row.update(kind='instances',model=model,source_kind=rows[0].get('properties',{}).get('iw4l',{}).get('source_kind'),points=read(obj.data.vertices,'co',3).tolist(),
                       rotations=read(obj.data.attributes['wz_rotation'].data,'vector',3).tolist(),
                       scales=read(obj.data.attributes['wz_scale'].data,'vector',3).tolist(),records=plain(rows))
        elif obj.type=='MESH' and obj.data.polygons:
            part=parts.get(str(obj.get('wz09_city_edit_part_id','')), {})
            offset=part.get('source_origin_m',[0.,0.,0.])
            try:
                attempts=[]
                corner_arrays=None; mesh_frame=None
                for clipped in (False,True):
                    candidate,source=candidate_for(obj,reference,boundary,cache,clipped=clipped)
                    try:
                        selected,error=match_faces(candidate,obj.data,offset)
                        break
                    except ValueError as exc:
                        attempts.append(str(exc))
                        if len(candidate.vertices)==len(obj.data.vertices) and len(candidate.polygons)==len(obj.data.polygons):
                            center=read(candidate.vertices,'co',3).astype(np.float64).mean(axis=0)-read(obj.data.vertices,'co',3).astype(np.float64).mean(axis=0)
                            try: selected,error=match_faces(candidate,obj.data,center); offset=center.tolist(); break
                            except ValueError: pass
                        transformed,frame=fit_mesh_frame(candidate,obj.data,offset) if source['role']=='model' else (None,None)
                        if transformed is not None:
                            try:
                                selected,error=match_faces(transformed,obj.data,offset)
                                mesh_frame=frame
                                bpy.data.meshes.remove(transformed)
                                break
                            except ValueError: bpy.data.meshes.remove(transformed)
                        try:
                            corner_arrays,error=match_corners(candidate,obj.data,offset)
                            selected=list(range(len(obj.data.polygons)))
                            break
                        except ValueError as exc: attempts.append(str(exc))
                else: raise ValueError('; '.join(attempts))
            except ValueError as exc:
                failures.append(dict(object=obj.name,faces=len(obj.data.polygons),vertices=len(obj.data.vertices),metadata=info,
                                     candidate_faces=len(candidate.polygons) if 'candidate' in locals() else None,
                                     candidate_vertices=len(candidate.vertices) if 'candidate' in locals() else None,error=str(exc)))
                continue
            maximum=max(maximum,error); total+=len(selected)
            file=identity[obj]+'.npz'
            arrays=corner_arrays if corner_arrays is not None else dict(face_indices=np.asarray(selected,dtype=np.int32))
            attribute=obj.data.attributes.get('wz09_city_source_face')
            if attribute: arrays['editor_face_ids']=read(attribute.data,'value',1,np.int32).ravel()
            np.savez_compressed(output/file,**arrays)
            row.update(kind='native_faces',source=source,selection=file,selection_sha256=hashlib.sha256((output/file).read_bytes()).hexdigest(),mesh_origin=offset,mesh_frame=mesh_frame,faces=len(selected))
        elif obj.type=='MESH': row['kind']='empty_mesh'
        else: row['kind']='empty'
        records.append(row)
    for mesh in cache.values(): bpy.data.meshes.remove(mesh)
    return dict(objects=records,matched_triangles=total,max_coordinate_error=maximum,failures=failures)
