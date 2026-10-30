"""Reconstruct a public project from user-local native exports and edit recipes."""
import copy
import json
from pathlib import Path
import uuid


def _evict_temporary_data(before,scene):
    """Remove only this source import's unused IDs, including fake-user data."""
    import bpy
    from .map_residency import all_ids
    current=set(all_ids())
    additions={value for value in current if value.as_pointer() not in before}
    keep=(current-additions)|{scene,scene.collection}
    users=bpy.data.user_map()
    while True:
        retained={value for value in additions-keep if users.get(value,set()) & keep}
        if not retained: break
        keep.update(retained)
    doomed=additions-keep
    for value in doomed: value.use_fake_user=False
    if doomed: bpy.data.batch_remove(ids=doomed)


def _matrix_mesh(mesh,matrix):
    import numpy as np
    from .city_loose_parts import _capture
    captured=_capture(mesh)
    m=np.asarray(matrix,dtype=np.float64)
    coords=captured['co']@m[:3,:3].T+m[:3,3]
    normal=captured['normals']@np.linalg.inv(m[:3,:3])
    normal/=np.maximum(np.linalg.norm(normal,axis=1,keepdims=True),1e-20)
    mesh.vertices.foreach_set('co',coords.astype(np.float32).ravel())
    mesh.update(); mesh.normals_split_custom_set(normal)


def _corners_mesh(source,arrays,origin):
    import bpy
    import numpy as np
    from .city_loose_parts import _capture
    data=_capture(source); corners=arrays['corner_indices']; vertices=data['vertices'][corners]
    counts=arrays['polygon_sizes']; mesh=bpy.data.meshes.new('Locally reconstructed native part')
    mesh.vertices.add(len(vertices)); mesh.vertices.foreach_set('co',(data['co'][vertices]-origin).astype(np.float32).ravel())
    mesh.loops.add(len(corners)); mesh.loops.foreach_set('vertex_index',np.arange(len(corners),dtype=np.int32))
    mesh.polygons.add(len(counts)); mesh.polygons.foreach_set('loop_start',(np.cumsum(counts)-counts).astype(np.int32))
    mesh.polygons.foreach_set('loop_total',counts)
    for material in source.materials: mesh.materials.append(material)
    mesh.polygons.foreach_set('material_index',arrays['material_indices'])
    mesh.update(calc_edges=True)
    for name,domain,kind,prop,values in data['attributes']:
        if domain not in ('POINT','CORNER') or name=='material_index': continue
        indices=vertices if domain=='POINT' else corners
        if kind=='FLOAT2' and domain=='CORNER' and source.uv_layers.get(name):
            layer=mesh.uv_layers.new(name=name); layer.data.foreach_set('uv',values[indices].ravel())
        else:
            target=mesh.attributes.get(name) or mesh.attributes.new(name,kind,domain)
            target.data.foreach_set(prop,values[indices].ravel())
    mesh.normals_split_custom_set(data['normals'][corners]); mesh.update()
    return mesh


def _native_mesh(row,reference,boundary,cache,selection_path):
    import bpy
    import numpy as np
    from .city_loose_parts import _capture,_piece
    from .fast_preview_clip import clipped_mesh
    source=row['source']; key=json.dumps(source,sort_keys=True)
    if key not in cache:
        if source['role']=='world': original=reference['world']
        else:
            code=source.get('source_map') or reference['primary_code']
            original=None
            if code!=reference['primary_code']: original=reference['models_by_code'].get((code,source['model']))
            if original is None and source.get('authored_slot') is not None: original=reference['by_slot'].get(source['authored_slot'])
            if original is None and source.get('source_kind'): original=reference['semantic_by_code'].get((code,source['model']))
            original=original or reference['models_by_code'].get((code,source['model']))
            if original is None: raise ValueError('Required native model is unavailable: '+source['model'])
        cache[key]=clipped_mesh(original,reference['owner'],boundary) if source.get('clipped',True) else original.data.copy()
    source_mesh=cache[key]
    transformed=None
    if row.get('mesh_frame'):
        transformed=source_mesh.copy(); _matrix_mesh(transformed,row['mesh_frame']); source_mesh=transformed
    with np.load(selection_path,allow_pickle=False) as arrays:
        if 'corner_indices' in arrays: mesh=_corners_mesh(source_mesh,arrays,np.asarray(row['mesh_origin']))
        else:
            mesh,center=_piece(source_mesh,_capture(source_mesh),arrays['face_indices'])
            values=np.empty((len(mesh.vertices),3),np.float32); mesh.vertices.foreach_get('co',values.ravel())
            values+=np.asarray(center)-row['mesh_origin']; mesh.vertices.foreach_set('co',values.ravel()); mesh.update()
        if 'editor_face_ids' in arrays:
            attribute=mesh.attributes.get('wz09_city_source_face') or mesh.attributes.new('wz09_city_source_face','INT','FACE')
            attribute.data.foreach_set('value',arrays['editor_face_ids'])
    if transformed: bpy.data.meshes.remove(transformed)
    return mesh


def bind_registration(root,row,assets,profile,output):
    from . import installation_setup as setup
    from .calibration import validate_calibration,verify_calibration_source
    code=row['source_map']; bundle=assets[code]['preview_bundle']
    value=copy.deepcopy(row['calibration'])
    # Bind the measured native-coordinate frame to the user's actual FF. No
    # navigation CSV or physical calibration is claimed by this project.
    value['source_path']=profile['sources'][code]['path']; value['source_sha256']=profile['sources'][code]['sha256']
    value['measurement_basis']='Existing measured native-to-export registration, rebound to the selected original FF; independent physical scale remains unverified.'
    registration=value['registration']
    registration.update(reference_path=bundle['reference_path'],reference_sha256=bundle['reference_sha256'],
                        resources=bundle['resources'],native_source_path=value['source_path'],native_source_sha256=value['source_sha256'])
    evidence={k:value[k] for k in ('source_path','source_sha256','axis_mapping','offset_m','landmarks','max_error_m','source_units_to_meter')}
    evidence.update({k:registration[k] for k in ('native_source_path','native_source_sha256','reference_path','reference_sha256','exporter_sha256')})
    evidence_path=output/(row['id']+'-registration.json'); setup.atomic_json(evidence_path,evidence)
    registration['landmark_evidence_path']=str(evidence_path); registration['landmark_evidence_sha256']=setup.digest(evidence_path)
    value=validate_calibration(value); verify_calibration_source(value)
    root['wz09_calibration_json']=json.dumps(value)
    root['wz09_preview_source']=bundle['reference_path']; root['wz09_preview_sha256']=bundle['reference_sha256']
    root['wz09_map_provenance_json']=json.dumps(dict(preview_bundle=bundle))
    root['wz09_supplemental_preview_bundles_json']=json.dumps([assets[code]['supplemental_bundle']])


def build_map(scene,root,row,reference,project,output):
    import bpy
    import numpy as np
    from mathutils import Matrix
    from . import compact_preview,city_view_cache,overview_preview,map_residency
    collection=bpy.data.collections.new('Details '+row['id']); scene.collection.children.link(collection)
    collection[overview_preview.SOURCE]=row['id']
    created={}; cache={}; prototypes={}
    for record in row['objects']:
        kind=record['kind']
        if kind=='native_faces':
            selection=project/'selections'/row['id']/record['selection']
            selection.resolve().relative_to(project.resolve())
            from .installation_setup import digest
            if record.get('selection_sha256') and digest(selection)!=record['selection_sha256']: raise ValueError('Native face selection checksum mismatch')
            data=_native_mesh(record,reference,row['boundary'],cache,selection)
        elif kind in ('empty_mesh','instances'):
            data=bpy.data.meshes.new('Instance placements' if kind=='instances' else 'Retained source')
            if kind=='instances':
                data.from_pydata(record['points'],[],[])
                for name,values in (('wz_rotation',record['rotations']),('wz_scale',record['scales'])):
                    attribute=data.attributes.new(name,'FLOAT_VECTOR','POINT')
                    attribute.data.foreach_set('vector',np.asarray(values,np.float32).ravel())
        else: data=None
        obj=bpy.data.objects.new(record['name'],data); collection.objects.link(obj); created[record['id']]=obj
        for key,value in record['properties'].items():
            if value is not None and key not in ('wz09_city_edit_source_id','wz09_city_edit_part_id','wz09_city_part_source'):
                obj[key]=value
        obj['wz09_library_object_id']=record['id']
        obj['wz09_preview_only']=True
        obj.matrix_parent_inverse=Matrix(record['matrix_parent_inverse']); obj.matrix_basis=Matrix(record['matrix_basis'])
        obj.hide_viewport=record.get('hidden',False)
        if kind=='instances':
            key=(record.get('source_kind'),record['model'])
            if key not in prototypes:
                source=(reference['semantic_models'].get(record['model']) if key[0] else None) or reference['models'].get(record['model'])
                if source is None: raise ValueError('Instance prototype missing: '+record['model'])
                prototype=bpy.data.objects.new('Native prototype '+record['model'],source.data)
                collection.objects.link(prototype); prototype.parent=root
                prototype['wz09_editor_overlay']=True; prototype['wz09_preview_only']=True
                prototype['wz09_native_instance_prototype']=True; prototype.hide_render=True; prototype.hide_set(True)
                prototypes[key]=prototype
            group=compact_preview._node_group(bpy)
            socket=next(s.identifier for s in group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT' and s.name=='Source')
            modifier=obj.modifiers.new('Shared native geometry','NODES'); modifier.node_group=group
            if hasattr(modifier,'properties'): getattr(modifier.properties.inputs,socket).value=prototypes[key]
            else: modifier[socket]=prototypes[key]
            obj[compact_preview.RECORDS]=json.dumps(record['records']); obj['wz09_compact_count']=len(record['points'])
            obj['wz09_compact_template']=prototypes[key].name
    for record in row['objects']:
        obj=created[record['id']]; obj.parent=created.get(record['parent']) or root
    bpy.context.view_layer.update()
    core=map_residency.write_library(scene,root,set(collection.all_objects),output/'details')
    root[map_residency.LIBRARY]=json.dumps(core)
    bpy.context.view_layer.objects.active=root
    city_view_cache.bake(bpy.context)
    proxy=bpy.context.active_object
    gray=bpy.data.materials.get('Map overview') or bpy.data.materials.new('Map overview')
    gray.diffuse_color=(.58,.6,.57,1)
    proxy.data.materials.clear(); proxy.data.materials.append(gray)
    proxy.data.polygons.foreach_set('material_index',np.zeros(len(proxy.data.polygons),np.int32))
    map_residency.simplify_proxy(proxy)
    bpy.data.batch_remove(ids=list(collection.all_objects)+[collection])
    for mesh in cache.values(): bpy.data.meshes.remove(mesh)
    return core


def build_outskirts(scene,root,row,reference,output):
    import bpy
    from mathutils import Vector
    from . import map_residency,fast_preview_clip,composite_world
    made=[]
    boundary=row['boundary']; inverse=reference['owner'].matrix_world.inverted()
    for original in reference['objects']:
        if original.type!='MESH' or not original.data.polygons: continue
        if reference['object_codes'].get(original)!=row['source_map']: continue
        info=original.get('iw4l')
        if hasattr(info,'get') and (info.get('asset_only') or info.get('source_kind')=='destruction_state_asset'): continue
        corners=[inverse@original.matrix_world@Vector(p) for p in original.bound_box]
        if all(composite_world.point_inside((p.x,p.y),boundary,_validated=True) for p in corners): continue
        derived=fast_preview_clip.clipped_mesh(original,reference['owner'],boundary,outside=True)
        if not derived.polygons: bpy.data.meshes.remove(derived); continue
        obj=bpy.data.objects.new('Outskirts '+original.name,derived)
        obj.parent=root; obj.matrix_basis=original.matrix_world.copy(); obj['wz09_preview_only']=True
        made.append(obj)
    record=map_residency.write_library(scene,root,set(made),output/'outskirts',layer='outer')
    root[map_residency.OUTER]=json.dumps(record)
    if made: bpy.data.batch_remove(ids=made)
    return record


def rebuild(root,assets_path,output,status=None):
    import bpy
    import importlib.util
    import numpy as np
    from mathutils import Matrix
    from . import installation_setup as setup,map_residency,asset_browser,asset_catalog,asset_slices,source_bundle
    root=Path(root).resolve(); output=Path(output).resolve(); output.relative_to(root)
    if output.exists(): raise FileExistsError('A fresh workspace output directory is required')
    output.mkdir(parents=True)
    recipe=setup.manifest(root); assets=setup.read_json(assets_path)['maps']; selected=setup.profile(root)
    spec=importlib.util.spec_from_file_location('wz09_native_recipe',root/'tools/native_recipe.py')
    helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    scene=bpy.data.scenes.new('WZ2009 Map Workshop'); bpy.context.window.scene=scene
    scene['wz09_library_workspace']=True; scene['wz09_light_project']=True; scene['wz09_public_project']=True
    scene.wz09_lazy_assets_enabled=True
    scene.wz09_asset_root=selected['installation'];scene.wz09_original_game_dir=selected['installation']
    scene['wz09_city_edit_registry_json']='{"meshes":{},"parts":{},"models":{}}'
    roots={}; all_records={}
    for row in recipe['maps']:
        obj=bpy.data.objects.new(row['name'],None); scene.collection.objects.link(obj); roots[row['id']]=obj
        obj['wz09_type']='map_instance';obj['wz09_id']=row['id'];obj['wz09_source_map']=row['source_map'];obj['wz09_display_name']=row['name']
        obj.matrix_world=Matrix(row['matrix_world'])
        bind_registration(obj,row,assets,selected,output)
    source_catalogs={}
    for number,code in enumerate(sorted({r['source_map'] for r in recipe['maps']}),1):
        if status: status(phase='BUILD',map=code,message='Reconstructing '+code,done=number-1,total=len(assets))
        before={value.as_pointer() for value in map_residency.all_ids()}
        native_scene=bpy.data.scenes.new('Native sources '+code)
        extras=[(assets[code]['supplemental_bundle']['reference_path'],code)]
        for row in recipe['maps']:
            if row['source_map']!=code: continue
            for obj in row['objects']:
                other=obj.get('source',{}).get('source_map')
                if other and other!=code:
                    for key in ('preview_bundle','supplemental_bundle'):
                        bundle=assets[other][key]
                        catalog_key=(other,key)
                        if catalog_key not in source_catalogs:
                            document=source_bundle.verify_preview_bundle(bundle)
                            source_catalogs[catalog_key]=asset_catalog.catalog_from_document(document,bundle,other)['assets']
                        for entry in source_catalogs[catalog_key]:
                            if entry['model']!=obj['source']['model']: continue
                            sliced=asset_slices.capture_slice(entry,bundle,root/'.local/source-slices')
                            value=(sliced['reference_path'],other)
                            if value not in extras: extras.append(value)
        reference=helper.import_reference(native_scene,assets[code]['preview_bundle']['reference_path'],extras,primary_code=code)
        bpy.context.window.scene=scene
        for row in recipe['maps']:
            if row['source_map']==code:
                all_records[row['id']]=build_map(scene,roots[row['id']],row,reference,root/'project',output)
                build_outskirts(scene,roots[row['id']],row,reference,output)
        bpy.data.batch_remove(ids=list(reference['objects'])+[reference['owner']]); bpy.data.scenes.remove(native_scene)
        _evict_temporary_data(before,scene)
    terrain=recipe.get('authored_terrain')
    if terrain:
        terrain_path=setup.local_path(root/'project',terrain['file'])
        if terrain.get('sha256') and setup.digest(terrain_path)!=terrain['sha256']: raise ValueError('Authored terrain checksum mismatch')
        with np.load(terrain_path,allow_pickle=False) as arrays:
            mesh=bpy.data.meshes.new('Authored landscape'); mesh.vertices.add(len(arrays['positions'])); mesh.vertices.foreach_set('co',arrays['positions'].ravel())
            mesh.loops.add(len(arrays['vertices'])); mesh.loops.foreach_set('vertex_index',arrays['vertices'])
            mesh.polygons.add(len(arrays['counts'])); mesh.polygons.foreach_set('loop_start',(np.cumsum(arrays['counts'])-arrays['counts']).astype(np.int32)); mesh.polygons.foreach_set('loop_total',arrays['counts'])
            mesh.update(calc_edges=True)
            for descriptor in terrain['materials']:
                material=bpy.data.materials.new(descriptor['name']); material.diffuse_color=descriptor['color']; mesh.materials.append(material)
            mesh.polygons.foreach_set('material_index',arrays['material_indices'])
            obj=bpy.data.objects.new('KARVORSK authored terrain',mesh); scene.collection.objects.link(obj); obj.matrix_world=Matrix(terrain['matrix_world'])
            obj['wz09_authoring_terrain']=True;obj['wz09_custom_geometry']=True
    scene['wz09_city_view_states_json']=json.dumps({identity:'unloaded' for identity in roots})
    scene.wz09_project_root=str(root)
    catalog=asset_browser.rebuild_catalog(bpy.context)
    path=output/'KARVORSK_EDITOR.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path),compress=False)
    result=dict(kind='wz09-ready-workspace-v1',project_sha256=setup.digest(root/'project/manifest.json'),
                path=str(path),maps=len(roots),assets=len(catalog['assets']),libraries=all_records,game_started=False)
    setup.atomic_json(root/'.local/ready.json',result)
    return result
