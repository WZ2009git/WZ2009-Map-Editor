"""Bind supplemental native ScriptModel/DynEnt visuals by exact FF record.

This extends the existing glTF/catalog/placement path. Original static preview
provenance stays intact; materials and meshes remain shared visual data. Nothing
here implements destruction, native collision, animation or a game runtime.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

try:
    from . import native_semantics as ns
    from .asset_catalog import catalog_from_document
    from .source_bundle import capture_gltf_bundle,verify_preview_bundle
    from .authoring_helpers import resolve_owner_map
except ImportError:
    import native_semantics as ns
    from asset_catalog import catalog_from_document
    from source_bundle import capture_gltf_bundle,verify_preview_bundle
    from authoring_helpers import resolve_owner_map

_CLASSES=[]


def source_identity(record):
    kind=record.get('source_kind')
    if kind=='script_model':
        ordinal=record.get('source_ordinal')
        if type(ordinal) is not int or ordinal<0:raise ValueError('Native script-model ordinal is invalid')
        return kind,ordinal
    if kind=='dyn_ent':
        index,draw=record.get('source_index'),record.get('draw_type')
        if type(index) is not int or index<0 or draw not in {'Model','Brush'}:
            raise ValueError('Native dynamic-entity draw type/index is invalid')
        return kind,draw,index
    raise ValueError('Unsupported native semantic visual source kind')


def evidence_source(record,data):
    """Resolve a checked array pointer; never bind by model/object name alone."""
    evidence=record.get('evidence',{})
    pointer=evidence.get('source_record','').partition(';')[0].strip()
    match=re.fullmatch(r'(script_models|dyn_ents)\[(\d+)\]',pointer)
    if evidence.get('status')!='source_bound' or not match:return None
    array,index=match.group(1),int(match.group(2))
    if index>=len(data[array]):raise ValueError('Native action evidence index is outside its source array')
    source=data[array][index]
    return source_identity(source),source


def validate_document(manifest,document,semantics,bundle):
    """Pure identity checks after caller has checksum-verified the glTF bundle."""
    if (manifest.get('kind')!='wz09_native_semantic_visuals' or manifest.get('version')!=1
            or manifest.get('coordinate_system')!='gltf_y_up_meters_export_convention'):
        raise ValueError('Expected native semantic visuals version 1 with explicit P1a coordinates')
    if manifest.get('source_map')!=semantics['source_map'] or manifest.get('native_source_sha256')!=semantics['source_sha256']:
        raise ValueError('Visual export belongs to another original FF or map')
    if any(manifest.get(key) is not False for key in ('runtime_integrated','physical_scale_verified','collision_verified','game_started','game_workspace_written')):
        raise ValueError('Native visual export cannot grant runtime/collision/physical verification')
    catalog=catalog_from_document(document,bundle,semantics['source_map'])
    models={}
    for entry in catalog['assets']:
        if entry['model'] in models and models[entry['model']]!=entry['mesh_index']:
            raise ValueError('Native visual export has ambiguous meshes for one source model')
        models[entry['model']]=entry['mesh_index']
    source={source_identity(r):r for array in ('script_models','dyn_ents') for r in semantics[array]}
    nodes,meshes=document.get('nodes',[]),document.get('meshes',[])
    records=manifest.get('records')
    if not isinstance(records,list):raise ValueError('Native visual records must be an array')
    seen=set();exported={}
    for record in records:
        identity=source_identity(record)
        if identity in seen:raise ValueError('Duplicate native visual source identity')
        seen.add(identity)
        original=source.get(identity)
        if original is None or original.get('model')!=record.get('model'):
            raise ValueError('Native visual ordinal/model differs from original semantic record')
        if record.get('status') in {'missing_geometry_or_material','missing_xmodel_geometry_or_material'}:
            if record.get('node_index') is not None:
                raise ValueError('Missing native visual must not declare a render node')
            mi=record.get('mesh_index')
            if mi is not None and (type(mi) is not int or not 0<=mi<len(meshes)):
                raise ValueError('Retained unplaced native mesh index is invalid')
            continue
        if record.get('status')!='exported':raise ValueError('Unknown native visual export status')
        ni,mi=record.get('node_index'),record.get('mesh_index')
        if type(ni) is not int or not 0<=ni<len(nodes) or type(mi) is not int or not 0<=mi<len(meshes):
            raise ValueError('Native visual node/mesh index is invalid')
        node=nodes[ni];metadata=node.get('extras',{}).get('iw4l',{})
        if (source_identity(metadata)!=identity or metadata.get('native_source_sha256')!=semantics['source_sha256']
                or node.get('mesh')!=mi or metadata.get('model')!=record['model'] or meshes[mi].get('name')!=record['model']):
            raise ValueError('Native glTF node identity is not bound to the semantic source')
        for a,b in ((record.get('native_origin'),original.get('origin_source')),
                    (record.get('native_origin'),metadata.get('native_transform',{}).get('origin'))):
            if not isinstance(a,list) or not isinstance(b,list) or len(a)!=3 or len(b)!=3 or any(
                    type(v) not in (int,float) or not math.isfinite(v) for v in a+b) or any(abs(x-y)>1e-4 for x,y in zip(a,b)):
                raise ValueError('Native visual origin differs from source coordinates')
        q=record.get('native_quaternion_xyzw')
        if not isinstance(q,list) or len(q)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in q) or sum(v*v for v in q)<1e-12:
            raise ValueError('Native visual quaternion is invalid')
        authored_q=metadata.get('native_transform',{}).get('quaternion_xyzw')
        if (not isinstance(authored_q,list) or len(authored_q)!=4
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in authored_q)
                or any(abs(a-b)>1e-5 for a,b in zip(q,authored_q))):
            raise ValueError('Native visual quaternion differs from glTF source metadata')
        exported[identity]=record
    if seen!=set(source):raise ValueError('Native visual inventory omits original source records; missing geometry must be reported explicitly')
    for state in manifest.get('state_models',[]):
        ni,mi=state.get('node_index'),state.get('mesh_index')
        if ni is None and mi is None:continue
        if type(ni) is not int or not 0<=ni<len(nodes) or type(mi) is not int or not 0<=mi<len(meshes):
            raise ValueError('Destruction state node/mesh index is invalid')
        node=nodes[ni];metadata=node.get('extras',{}).get('iw4l',{})
        if (metadata.get('source_kind')!='destruction_state_asset' or metadata.get('asset_only') is not True
                or metadata.get('native_source_sha256')!=semantics['source_sha256'] or node.get('mesh')!=mi
                or metadata.get('model')!=state['model'] or meshes[mi].get('name')!=state['model']):
            raise ValueError('Destruction state asset is not bound to the same native export')
    return {'catalog':catalog,'exported':exported,'source_records':source}


def read_visuals(path,semantics_path):
    path=Path(path).resolve()
    if not path.is_file() or path.stat().st_size>64*1024*1024:raise ValueError('Native visual manifest missing or too large')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    report=None;report_path=None
    if manifest.get('kind')=='offline_native_semantic_visual_export':
        report,report_path=manifest,path
        path=Path(report['native_manifest']).resolve()
        if not path.is_relative_to(report_path.parent):raise ValueError('Native visual manifest escapes its report output folder')
        manifest=json.loads(path.read_text(encoding='utf-8'))
    else:
        candidate=path.parent.parent/'visual-export.json'
        if candidate.is_file():
            possible=json.loads(candidate.read_text(encoding='utf-8'))
            if Path(possible.get('native_manifest','')).resolve()==path:
                report,report_path=possible,candidate
    semantics=ns.read_semantics(semantics_path)
    scene=manifest.get('scene')
    if not isinstance(scene,str) or not scene or Path(scene).is_absolute():raise ValueError('Native scene must be a relative local glTF path')
    scene=(path.parent/scene).resolve()
    if not scene.is_relative_to(path.parent):raise ValueError('Native glTF scene escapes its export folder')
    if report is not None:
        if (report.get('status')!='PASS' or report.get('kind')!='offline_native_semantic_visual_export'
                or report.get('native_manifest_sha256')!=ns.file_sha256(path)
                or report.get('semantics_sha256')!=ns.file_sha256(semantics_path)
                or report.get('native_source_sha256')!=semantics['source_sha256']):
            raise ValueError('Native visual report hash/source binding differs')
        bundle=report['preview_bundle']
        if Path(bundle['reference_path']).resolve()!=scene:raise ValueError('Native visual report points to another glTF scene')
    else:bundle=capture_gltf_bundle(scene,include_images=True)
    document=verify_preview_bundle(bundle)
    checked=validate_document(manifest,document,semantics,bundle)
    return dict(checked,manifest=manifest,semantics=semantics,bundle=bundle,
                manifest_path=str(path),manifest_sha256=ns.file_sha256(path),
                report_path=str(report_path) if report_path else None,report_sha256=ns.file_sha256(report_path) if report_path else None,
                semantics_path=str(Path(semantics_path).resolve()),semantics_sha256=ns.file_sha256(semantics_path))


def _transform(owner,scene,semantic_sha):
    raw=owner.get('wz09_native_transform_json')
    if raw:result=ns.validate_transform(json.loads(raw))
    else:
        result=None
        for obj in scene.objects:
            if resolve_owner_map(obj)!=owner:continue
            for field in ('wz09_action_json','wz09_audio_json'):
                if not obj.get(field):continue
                evidence=json.loads(obj[field]).get('evidence',{})
                if evidence.get('source_sha256')!=semantic_sha:continue
                pointer=evidence.get('source_record','')
                if '; transform=' in pointer:
                    payload=pointer.partition('; transform=')[2]
                    result=ns.validate_transform(json.JSONDecoder().raw_decode(payload)[0]);break
            if result is not None:break
    if result is None:raise ValueError('Import native semantics with an explicit source transform first')
    # Supplemental meshes are already in the existing P1a export convention.
    # Other source transforms need an explicitly derived geometry conversion.
    if (not math.isclose(result['source_units_to_editor_unit'],.0254,rel_tol=1e-6)
            or [a.lstrip('+') for a in result['axis_mapping']]!=['x','y','z']
            or any(abs(v)>1e-5 for v in result['offset_m'])):
        raise ValueError('Supplemental visual geometry requires matching reviewed P1a XYZ export coordinates; custom transforms are not silently applied')
    return result


def _metadata(obj):
    value=obj.get('iw4l')
    return value if hasattr(value,'get') else {}


def _pose_check(obj,source,transform,owner=None):
    from mathutils import Euler,Vector
    position=ns._position(source['origin_source'],transform,[0,0,0],[0,0,0])
    rotation=ns._rotation(source.get('angles_deg',[0,0,0]),transform,[0,0,0],
        source.get('quaternion_xyzw') if source['source_kind']=='dyn_ent' else None)
    matrix=obj.matrix_world if owner is None else owner.matrix_world.inverted()@obj.matrix_world
    expected=Euler([math.radians(v) for v in rotation],'XYZ').to_quaternion()
    if (matrix.translation-Vector(position)).length>1e-3 or abs(matrix.to_quaternion().dot(expected))<1-1e-5:
        raise ValueError('Native visual/action source pose mismatch; edited actions need an explicit binding decision')


def _template(ctx,owner,node,entry,digest,bpy,*,parts=None):
    from mathutils import Matrix
    from .asset_browser import _mesh_parts
    parts=_mesh_parts(ctx.scene,node) if parts is None else parts
    inverse=node.matrix_world.inverted()
    root=bpy.data.objects.new('Source library · '+entry['model'],None)
    ctx.scene.collection.objects.link(root)
    root.parent=owner;root.matrix_parent_inverse=Matrix.Identity(4);root.matrix_basis=Matrix.Identity(4)
    root['iw4l']={'model':entry['model']}
    root['wz09_native_visual_library']=True
    root['wz09_native_visual_source_sha']=digest
    for part in parts:
        obj=part.copy();ctx.scene.collection.objects.link(obj)
        for key in list(obj.keys()):del obj[key]
        obj.parent=root;obj.matrix_parent_inverse=Matrix.Identity(4)
        obj.matrix_basis=inverse@part.matrix_world
        obj['wz09_preview_only']=True;obj['wz09_preview_id']=str(uuid.uuid4())
        obj['wz09_editor_overlay']=True;obj.hide_set(True);obj.hide_render=True
    root['wz09_preview_only']=True;root['wz09_preview_id']=str(uuid.uuid4())
    root['wz09_editor_overlay']=True;root.hide_set(True);root.hide_render=True
    return root


def import_into_scene(context,path,*,semantics_path=None):
    """Import supplementary native visuals without replacing static map source."""
    import bpy
    from mathutils import Matrix
    from . import asset_browser
    if context.mode!='OBJECT':raise ValueError('Leave Edit Mode before importing native visuals')
    owner=resolve_owner_map(context.active_object)
    if owner is None:raise ValueError('Select the matching destination map')
    inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}')).get(str(owner['wz09_id']))
    if not inventory:raise ValueError('Load actual native semantics for this map first')
    semantics_path=semantics_path or inventory['path']
    if ns.file_sha256(semantics_path)!=inventory['sha256']:raise ValueError('Imported native semantics changed')
    prepared=read_visuals(path,semantics_path)
    data,bundle=prepared['semantics'],prepared['bundle']
    if data['source_map']!=owner.get('wz09_source_map') or data['source_sha256']!=inventory['original_ff_sha256']:
        raise ValueError('Native visual source does not match selected map inventory')
    digest=bundle['reference_sha256']
    imports=json.loads(owner.get('wz09_native_visual_imports_json','{}'))
    def update_index():
        if prepared['report_path']:
            index=json.loads(context.scene.get('wz09_native_visual_index_json','{}'))
            index[str(owner['wz09_id'])]={'path':prepared['report_path'],'sha256':prepared['report_sha256'],
                'source_map':data['source_map'],'native_source_sha256':data['source_sha256']}
            context.scene['wz09_native_visual_index_json']=json.dumps(index,sort_keys=True)
    if digest in imports:
        if imports[digest]['manifest_sha256']!=prepared['manifest_sha256']:raise ValueError('Native visual manifest changed for an already imported scene')
        imports[digest]=dict(imports[digest],report_path=prepared['report_path'],report_sha256=prepared['report_sha256'])
        owner['wz09_native_visual_imports_json']=json.dumps(imports,sort_keys=True)
        update_index()
        return dict(imports[digest],existing_skipped=True)
    transform=_transform(owner,context.scene,prepared['semantics_sha256'])
    actions={}
    for obj in context.scene.objects:
        if not obj.get('wz09_action_element') or resolve_owner_map(obj)!=owner:continue
        record=json.loads(obj['wz09_action_json'])
        if record.get('evidence',{}).get('source_sha256')!=prepared['semantics_sha256']:continue
        source=evidence_source(record,data)
        if source is None:continue
        identity,native=source
        if identity in actions:raise ValueError('Several actions refer to the same exact native source record')
        if obj.get('wz09_asset_placement'):raise ValueError('Native action already has another visual binding; detach explicitly before importing a new visual source')
        actions[identity]=(obj,record,native)
    before={obj.as_pointer() for obj in bpy.data.objects}
    old_selection=list(context.selected_objects);old_active=context.view_layer.objects.active
    originals=[]
    owner_fields=('wz09_supplemental_preview_bundles_json','wz09_native_visual_imports_json')
    owner_previous={key:owner[key] for key in owner_fields if key in owner}
    old_catalog=context.scene.get('wz09_asset_catalog_json')
    old_visual_index=context.scene.get('wz09_native_visual_index_json')
    try:
        result=bpy.ops.import_scene.gltf(filepath=bundle['reference_path'])
        if result!={'FINISHED'}:raise ValueError('Native supplemental glTF import failed')
        imported=[obj for obj in bpy.data.objects if obj.as_pointer() not in before]
        if not imported:raise ValueError('Native supplemental import produced no objects')
        imported_keys={obj.as_pointer() for obj in imported}
        scene_keys={obj.as_pointer() for obj in context.scene.objects}
        children_index={}
        for obj in imported:
            if obj.parent is not None:children_index.setdefault(obj.parent.as_pointer(),[]).append(obj)
        parts_cache={}
        def parts_for(obj):
            key=obj.as_pointer()
            if key not in parts_cache:
                parts_cache[key]=asset_browser._mesh_parts(context.scene,obj,
                    _children_index=children_index,_scene_keys=scene_keys)
            return parts_cache[key]
        nodes={};state_nodes=[]
        for obj in imported:
            metadata=_metadata(obj)
            if metadata.get('source_kind') in {'script_model','dyn_ent'}:
                identity=source_identity(metadata)
                if identity in nodes:raise ValueError('Native glTF import duplicated a source identity')
                if metadata.get('native_source_sha256')!=data['source_sha256']:raise ValueError('Imported native visual has a different FF binding')
                nodes[identity]=obj
            elif metadata.get('source_kind')=='destruction_state_asset':
                if metadata.get('native_source_sha256')!=data['source_sha256']:raise ValueError('Destruction state belongs to another original FF')
                state_nodes.append(obj)
            obj['wz09_preview_only']=True;obj['wz09_preview_id']=str(uuid.uuid4())
            obj['wz09_native_visual_source_sha']=digest
        if set(nodes)!=set(prepared['exported']):raise ValueError('Blender native node inventory differs from verified visual manifest')
        context.view_layer.update()
        for identity,node in nodes.items():
            _pose_check(node,prepared['source_records'][identity],transform)
            if identity in actions:_pose_check(actions[identity][0],actions[identity][2],transform,owner)
        # Reuse repaired visual winding, operating only on the new import.
        from .preview_geometry import repair_preview_winding
        repair_preview_winding(context.scene,objects=imported)
        for obj in imported:
            if obj.parent is None or obj.parent.as_pointer() not in imported_keys:
                local=obj.matrix_world.copy();obj.parent=owner
                obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=local
        context.view_layer.update()
        entries={entry['mesh_index']:entry for entry in prepared['catalog']['assets']}
        candidates={}
        for obj in [*nodes.values(),*state_nodes]:candidates.setdefault(_metadata(obj).get('model'),obj)
        template_count=0
        for entry in entries.values():
            candidate=candidates.get(entry['model'])
            if candidate is None:raise ValueError('Catalog source model was not imported from the native visual bundle')
            _template(context,owner,candidate,entry,digest,bpy,parts=parts_for(candidate));template_count+=1
        bound=0
        integrity_pending=[];integrity_cache={}
        for identity,node in nodes.items():
            if identity not in actions:continue
            target,record,native=actions[identity]
            if target.get('wz09_type'):raise ValueError('Native source action unexpectedly uses a separately typed gameplay marker')
            entry=entries[prepared['exported'][identity]['mesh_index']]
            fields=('wz09_asset_placement','wz09_asset_placement_id','wz09_asset_id','wz09_owner_map',
                    'wz09_asset_origin_offset_m','wz09_asset_record_json','wz09_action_json','wz09_collision_verified','wz09_asset_parts_json')
            originals.append((target,{key:target[key] for key in fields if key in target},fields))
            uid=str(uuid.uuid4())
            target['wz09_asset_placement']=True;target['wz09_asset_placement_id']=uid
            target['wz09_asset_id']=entry['id'];target['wz09_owner_map']=str(owner['wz09_id'])
            target['wz09_asset_origin_offset_m']=[0.,0.,0.]
            target['wz09_asset_record_json']=asset_browser._encoded({'asset':entry,'preview_bundle':bundle})
            target['wz09_collision_verified']=False
            matrix=node.matrix_world.copy();node.parent=target;node.matrix_parent_inverse=Matrix.Identity(4)
            node.matrix_world=matrix
            for part in parts_for(node):
                part['wz09_asset_part']=True;part['wz09_asset_placement_id']=uid
            node['wz09_asset_part']=True;node['wz09_asset_placement_id']=uid
            integrity_pending.append((target,tuple(parts_for(node))))
            record=copy.deepcopy(record);record['asset_placement_id']=uid
            visual_key=('visual_binding:script_model:'+str(identity[1]) if identity[0]=='script_model'
                        else 'visual_binding:dyn_ent:'+str(identity[1])+':'+str(identity[2]))
            resolved={visual_key,'xmodel:'+entry['model']}
            record['missing_dependencies']=[value for value in record['missing_dependencies'] if value not in resolved]
            if not any(value.startswith(('native_collision_binding:','native_collision:','cmodel:')) for value in record['dependencies']):
                collision='native_collision_binding:'+':'.join(str(value) for value in identity)
                record['dependencies']=sorted(set(record['dependencies'])|{collision})
                record['missing_dependencies']=sorted(set(record['missing_dependencies'])|{collision})
            target['wz09_action_json']=asset_browser._encoded(record);bound+=1
        context.view_layer.update()
        for target,parts in integrity_pending:asset_browser.capture_placement_integrity(target,parts,cache=integrity_cache)
        # Destruction states are real source assets, not automatically placed
        # wrecks or proof that the runtime transitions into those states.
        for node in state_nodes:
            for obj in [node,*parts_for(node)]:
                obj.hide_set(True);obj.hide_render=True;obj['wz09_editor_overlay']=True
        supplemental=json.loads(owner.get('wz09_supplemental_preview_bundles_json','[]'))
        if not any(item.get('reference_sha256')==digest for item in supplemental):supplemental.append(bundle)
        owner['wz09_supplemental_preview_bundles_json']=asset_browser._encoded(supplemental)
        summary={'manifest_path':prepared['manifest_path'],'manifest_sha256':prepared['manifest_sha256'],
            'report_path':prepared['report_path'],'report_sha256':prepared['report_sha256'],
            'source_sha256':digest,'native_ff_sha256':data['source_sha256'],'visuals_imported':len(nodes),
            'actions_bound':bound,'source_assets':template_count,'runtime_integrated':False,'collision_verified':False}
        imports[digest]=summary;owner['wz09_native_visual_imports_json']=asset_browser._encoded(imports)
        asset_browser.rebuild_catalog(context)
        update_index()
        context.view_layer.update()
        return dict(summary,existing_skipped=False)
    except Exception:
        for obj in list(bpy.data.objects):
            if obj.as_pointer() not in before:bpy.data.objects.remove(obj,do_unlink=True)
        for obj,previous,fields in originals:
            for key in fields:
                if key in previous:obj[key]=previous[key]
                elif key in obj:del obj[key]
        for key in owner_fields:
            if key in owner_previous:owner[key]=owner_previous[key]
            elif key in owner:del owner[key]
        if old_catalog is None:
            if 'wz09_asset_catalog_json' in context.scene:del context.scene['wz09_asset_catalog_json']
        else:context.scene['wz09_asset_catalog_json']=old_catalog
        if old_visual_index is None:
            if 'wz09_native_visual_index_json' in context.scene:del context.scene['wz09_native_visual_index_json']
        else:context.scene['wz09_native_visual_index_json']=old_visual_index
        raise
    finally:
        for obj in context.selected_objects:obj.select_set(False)
        for obj in old_selection:
            if obj.name in context.view_layer.objects:obj.select_set(True)
        if old_active is not None and old_active.name in context.view_layer.objects:context.view_layer.objects.active=old_active


def draw(layout,context):
    owner=resolve_owner_map(context.active_object)
    if owner is None:return
    box=layout.box();box.label(text='Native models and states',icon='OUTLINER_OB_MESH')
    box.operator('wz09.native_visuals_import',icon='IMPORT')
    imports=json.loads(owner.get('wz09_native_visual_imports_json','{}'))
    if imports:box.label(text=f"{sum((r['actions_bound'] for r in imports.values()))} native actions bound to visuals")
    box.label(text='Model binding does not verify explosion or collision behavior.',icon='INFO')


def register():
    import bpy
    from bpy.props import StringProperty
    from bpy_extras.io_utils import ImportHelper
    class WZ09_OT_native_visuals_import(bpy.types.Operator,ImportHelper):
        bl_idname='wz09.native_visuals_import';bl_label='Add native models';bl_options={'REGISTER','UNDO'}
        filename_ext='.json'
        filter_glob:StringProperty(default='visual-export.json;native-visuals.json;*.json',options={'HIDDEN'})
        def execute(self,context):
            try:
                result=import_into_scene(context,self.filepath)
                self.report({'INFO'},f"{result['visuals_imported']} native Visuals · {result['actions_bound']} Aktionen gebunden")
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError,TypeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    _CLASSES.append(WZ09_OT_native_visuals_import)
    bpy.utils.register_class(WZ09_OT_native_visuals_import)


def unregister():
    import bpy
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
