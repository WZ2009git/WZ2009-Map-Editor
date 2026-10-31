"""Isolated layer lifecycle and water/placement checks; no original file writes."""
import importlib
import json
from pathlib import Path
import sys
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'addons'))

def main(request):
    import bpy
    from mathutils import Vector
    import wz2009_map_editor as addon
    from wz2009_map_editor import map_parts as parts,map_residency as r,map_alignment as align,city_editing as city,compact_preview
    for module in (parts,align):
        if module is align and module._CLASSES:module.unregister()
        importlib.reload(module)
    classes=r._CLASSES;handle=r._HANDLE;importlib.reload(r);r._CLASSES=classes;r._HANDLE=handle
    if request['command']=='FIXTURES':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        if not hasattr(bpy.types.Scene,'wz09_sea_level'):addon.register()
        if not align._CLASSES:align.register()
        scene=bpy.context.scene
        root=bpy.data.objects.new('Fixture map',None);scene.collection.objects.link(root)
        root['wz09_type']='map_instance';root['wz09_id']=str(uuid.uuid4());root['wz09_source_map']='mp_fixture'
        root['wz09_calibration_json']=json.dumps(dict(registration=dict(native_source_sha256='a'*64)))
        root.location=(5,7,3)
        directory=ROOT/'.local/water_height_fixtures';directory.mkdir(parents=True,exist_ok=True)
        raw={}
        for layer in ('core','outer'):
            col=bpy.data.collections.new('Fixture '+layer);scene.collection.children.link(col)
            col[r.OWNER]=root['wz09_id'];col[r.LAYER]=layer;col[r.TOKEN]=uuid.uuid4().hex
            mesh=bpy.data.meshes.new('Disconnected '+layer)
            mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(10,0,0),(11,0,0),(10,1,0)],[],[(0,1,2),(3,4,5)])
            obj=bpy.data.objects.new('World '+layer,mesh);col.objects.link(obj);obj.parent=root;obj['wz09_preview_only']=True
            if layer=='outer':
                propmesh=bpy.data.meshes.new('Repeated outskirts prop');propmesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)],[],[(0,1,2)])
                for number in range(4):
                    prop=bpy.data.objects.new('Outer prop',propmesh);col.objects.link(prop);prop.parent=root
                    prop.location=(number*2,4,0);prop['wz09_preview_only']=True
                    prop['iw4l']=dict(model='fixture_prop',authored_slot=number)
                compact_preview.compact(scene)
            raw[layer]=r.write_library(scene,root,set(col.all_objects),directory,layer=layer)
            root[r.LIBRARY if layer=='core' else r.OUTER]=json.dumps(raw[layer])
            r._evict(bpy.context,root,layer)
        bpy.context.view_layer.objects.active=root
        r.load(bpy.context,root);r.load(bpy.context,root,layer='outer')
        core=r.resident(scene,root);outer=r.resident(scene,root,'outer')
        assert parts.ready(core) and parts.ready(outer)
        assert sum(bool(o.get(city.PART)) for o in outer.all_objects)==2
        assert sum(bool(o.get('wz09_individual_asset')) for o in outer.all_objects)==4
        deleted=next(o for o in outer.all_objects if o.get('wz09_individual_asset'))
        deleted_id=deleted['wz09_library_object_id'];bpy.data.objects.remove(deleted,do_unlink=True)
        original_core_ids=set(city.registry(scene)['meshes'])-set(r._local_registry(scene,root['wz09_id'],outer.all_objects)['meshes'])
        part=next(o for o in outer.all_objects if o.get(city.PART));part_id=part[city.PART]
        part.location.x+=2;part.data.vertices[0].co.z+=.75
        changed=tuple(part.data.vertices[0].co);local=part.location.copy()
        bpy.context.view_layer.update()
        before={o.as_pointer():o.matrix_world.translation.copy() for o in root.children_recursive}
        align.set_world_height(root,13);bpy.context.view_layer.update()
        assert all(abs((o.matrix_world.translation-before[o.as_pointer()]).z-10)<1e-5 for o in root.children_recursive)
        align.select_whole(bpy.context,root);assert len(bpy.context.selected_objects)==1 and bpy.context.active_object==root
        scene.wz09_alignment_map=root['wz09_id'];scene.wz09_sea_level=2
        extra=bpy.data.objects.new('New earlier map',None);scene.collection.objects.link(extra)
        extra['wz09_type']='map_instance';extra['wz09_id']=str(uuid.uuid4());extra['wz09_display_name']='A first map'
        assert align.selected_map(bpy.context)==root
        helper=align.ensure_water(bpy.context);wid=helper[align.WATER]
        scene.wz09_sea_level=4;align.ensure_water(bpy.context)
        assert helper[align.WATER]==wid and helper.location.z==4 and root.matrix_world.translation.z==13
        assert len([o for o in helper.children if o.get('wz09_alignment_surface')])==1
        record=r.save(bpy.context,root,layer='outer');r._evict(bpy.context,root,'outer')
        assert r.resident(scene,root,'outer') is None and r.resident(scene,root)==core
        assert original_core_ids<=set(city.registry(scene)['meshes'])
        assert record['editable_parts_format']==parts.FORMAT and record['layer']=='outer'
        def no_split(*args,**kwargs):raise AssertionError('Prepared outskirts were split again')
        from wz2009_map_editor import city_loose_parts
        original=city_loose_parts.face_components;city_loose_parts.face_components=no_split
        try:r.load(bpy.context,root,layer='outer')
        finally:city_loose_parts.face_components=original
        restored=next(o for o in r.resident(scene,root,'outer').all_objects if o.get(city.PART)==part_id)
        assert tuple(restored.data.vertices[0].co)==changed and (restored.location-local).length<1e-5
        assert not any(o.get('wz09_library_object_id')==deleted_id for o in r.resident(scene,root,'outer').all_objects)
        assert abs(root.matrix_world.translation.z-13)<1e-5
        return dict(fixtures=True,outer_auto_parts=True,outer_save_hide_reload=True,vertex_edit_retained=True,
                    core_registry_preserved=True,whole_map_moves_children=True,water_idempotent=True,water_does_not_move_maps=True,
                    individual_outer_props=True,deleted_prop_stays_deleted=True)
    if request['command']=='VERIFY_SAVED':
        report=json.loads((ROOT/'worklog/WATER_HEIGHT_SAVED.json').read_text(encoding='utf-8'))
        assert report['status']=='PASS'
        bpy.ops.wm.open_mainfile(filepath=report['path'],load_ui=False)
        if not align._CLASSES:align.register()
        scene=bpy.context.scene
        root=next(o for o in scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_source_map')=='mp_subbase')
        assert align.selected_map(bpy.context)==root
        helper=align.water_reference(scene)
        assert root.matrix_world.translation.z==0 and helper.matrix_world.translation.z==0
        assert len([o for o in helper.children if o.get('wz09_alignment_surface')])==1
        assert r.resident(scene,root) is not None
        assert json.loads(root[r.LIBRARY])['sha256']==report['saved_core']['sha256']
        assert r.sha(Path(report['saved_core']['path']))==report['saved_core']['sha256']
        return dict(reopened=True,subbase_z=0,water_z=0,core_objects=len(r.resident(scene,root).all_objects),path=report['path'])
    if request['command']=='REAL_OUTER':
        checkpoint=json.loads((ROOT/'worklog/WATER_HEIGHT_CHECKPOINT.json').read_text(encoding='utf-8'))['path']
        bpy.ops.wm.open_mainfile(filepath=checkpoint,load_ui=False)
        if not hasattr(bpy.types.Scene,'wz09_sea_level'):addon.register()
        scene=bpy.context.scene
        root=next(o for o in scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_source_map')=='mp_subbase')
        bpy.context.view_layer.objects.active=root
        r.load(bpy.context,root,layer='outer')
        col=r.resident(scene,root,'outer');stats=json.loads(col[parts.READY])
        meshes=[o for o in col.all_objects if o.type=='MESH' and o.data.polygons and not o.get('wz09_editor_overlay')]
        record=json.loads(root[r.OUTER]);r._evict(bpy.context,root,'outer')
        assert r.resident(scene,root,'outer') is None and r.resident(scene,root) is not None
        return dict(source='mp_subbase',stats=stats,meshes=len(meshes),record=record,outer_hidden_core_retained=True)
    raise ValueError('Unknown command')
