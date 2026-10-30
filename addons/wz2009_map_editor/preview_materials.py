"""Reversible, explicitly inferred texture-alpha previews on private bindings.

Never edits shared source meshes/materials or claims exact native IW4 coverage.
"""
import hashlib
import json
from pathlib import Path
import re
import uuid

_CLASSES = []
_STATE = 'wz09_alpha_preview_slots_json'
_SOURCE_STATE = 'wz09_source_alpha_preview_json'


def texture_alpha_plan(nodes, links):
    """Pure bounded graph analysis: unique RGBA base-colour texture -> Alpha."""
    by_id = {node['id']: node for node in nodes}
    incoming = {}
    for link in links:
        incoming.setdefault(link['to_node'], []).append(link)
    result = []
    for shader in nodes:
        if shader.get('type') != 'BSDF_PRINCIPLED':
            continue
        shader_links = incoming.get(shader['id'], [])
        if any(link['to_socket'] == 'Alpha' for link in shader_links):
            continue
        pending = [link['from_node'] for link in shader_links if link['to_socket'] == 'Base Color']
        seen, textures = set(), set()
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            if len(seen) > 128:
                raise ValueError('Base colour shader graph exceeds preview analysis limit')
            node = by_id.get(node_id, {})
            if node.get('type') == 'TEX_IMAGE':
                if node.get('channels') == 4:
                    textures.add(node_id)
                continue
            # Follow colour-processing nodes, never arbitrary shader/group code.
            if node.get('type') in {'MIX_RGB', 'MIX', 'REROUTE', 'HUE_SAT', 'GAMMA', 'INVERT', 'BRIGHTCONTRAST'}:
                pending.extend(link['from_node'] for link in incoming.get(node_id, []))
        if len(textures) == 1:
            result.append({'shader': shader['id'], 'texture': next(iter(textures))})
    return result


def _material_plan(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return []
    nodes = [{'id': node.name, 'type': node.type,
              'channels': getattr(getattr(node, 'image', None), 'channels', 0)}
             for node in material.node_tree.nodes]
    links = [{'from_node': link.from_node.name, 'from_socket': link.from_socket.name,
              'to_node': link.to_node.name, 'to_socket': link.to_socket.name}
             for link in material.node_tree.links]
    return texture_alpha_plan(nodes, links)


def _root(context):
    obj, seen = context.active_object, set()
    while obj is not None:
        key = obj.as_pointer() if hasattr(obj, 'as_pointer') else id(obj)
        if key in seen:
            raise ValueError('Asset parent cycle')
        seen.add(key)
        if obj.get('wz09_asset_placement'):
            return obj
        obj = obj.parent
    raise ValueError('Select a placed asset or one of its mesh parts')


def _parts(context, root):
    result = []
    uid = root.get('wz09_asset_placement_id')
    for obj in context.scene.objects:
        if obj.get('wz09_asset_part') and obj.get('wz09_asset_placement_id') == uid:
            if obj.library or getattr(obj, 'override_library', None) or any(scene != context.scene for scene in obj.users_scene):
                raise ValueError('Alpha preview requires scene-local placed mesh parts')
            result.append(obj)
    if not result:
        raise ValueError('Selected placement has no mesh parts')
    return result


def _dithered(material):
    prop = material.bl_rna.properties.get('surface_render_method')
    if prop is None or 'DITHERED' not in {item.identifier for item in prop.enum_items}:
        raise ValueError('This Blender version does not expose DITHERED transparency')
    material.surface_render_method = 'DITHERED'


def _source_alpha_objects(context, objects):
    objects=list(objects);result={}
    scene_keys={obj.as_pointer() for obj in context.scene.objects}
    for obj in objects:
        identities={obj.get(key) for key in ('wz09_preview_id','wz09_geometry_id') if obj.get(key)}
        if (obj.type!='MESH' or obj.library or getattr(obj,'override_library',None)
                or obj.as_pointer() not in scene_keys or any(scene!=context.scene for scene in obj.users_scene)):
            raise ValueError('Source alpha preview requires supplied scene-local mesh objects')
        if len(identities)!=1 or not all(isinstance(value,str) and value.strip() for value in identities):
            raise ValueError('Source alpha preview requires an unambiguous persistent object ID')
        identity=next(iter(identities))
        if identity in result:raise ValueError('Duplicate source alpha preview object ID')
        result[identity]=obj
    return result


def _source_alpha_evidence(source, mode, root, cache):
    try:from . import source_bundle
    except ImportError:import source_bundle
    if (not isinstance(source,dict) or set(source)!={'reference_path','reference_sha256','material_index','image_sha256'}
            or type(source.get('material_index')) is not int or source['material_index']<0
            or mode not in ('texture_alpha','shadow_hidden')):
        raise ValueError('Complete source glTF/material/image checksums and explicit alpha mode required')
    key=json.dumps(dict(source,mode=mode),sort_keys=True)
    if key in cache:return cache[key]
    reference=source_bundle.verify_file(source['reference_path'],source['reference_sha256'],'Source alpha glTF')
    if not reference.is_relative_to(root) or reference.suffix.lower()!='.gltf':
        raise ValueError('Source alpha evidence must be captured editor-local glTF')
    document=json.loads(reference.read_text(encoding='utf-8'))
    try:
        material=document['materials'][source['material_index']]
        texture=material['pbrMetallicRoughness']['baseColorTexture']['index']
        image=document['images'][document['textures'][texture]['source']]
        native=material['extras']['iw4l']
    except (KeyError,IndexError,TypeError) as exc:
        raise ValueError('Source alpha material/image binding is missing') from exc
    if (document.get('asset',{}).get('version')!='2.0' or material.get('alphaMode','OPAQUE')!='OPAQUE'
            or native.get('exact') is not False or native.get('coverage_exact') is not False
            or native.get('source_name')!=material.get('name')):
        raise ValueError('Source alpha preview requires an explicitly approximate opaque P1a material')
    image_path=source_bundle._external_resource(image.get('uri'),reference.parent)
    if image_path is None or image_path.suffix.lower()!='.png':raise ValueError('Source alpha requires a local source PNG')
    source_bundle.verify_file(str(image_path),source['image_sha256'],'Source alpha image')
    shadow=(material['name']=='mc/mtl_tree_shadow_caster' and native.get('technique_set')=='mc_shadowcaster_atest'
        and image.get('name')=='tree_shadowcaster'
        and source['image_sha256']=='0639a15e6916b764605433f18404caa4973ed290069b55d2973baa4d0b5278ee')
    if mode=='shadow_hidden' and not shadow or mode=='texture_alpha' and 'shadowcaster' in str(native.get('technique_set','')):
        raise ValueError('Native shadow cards require the exact audited separate shadow-hidden binding')
    fingerprint=hashlib.sha256(json.dumps(dict(reference_sha256=source['reference_sha256'],
        material_index=source['material_index'],image_sha256=source['image_sha256'],mode=mode),sort_keys=True).encode()).hexdigest()
    result=dict(source,reference_path=str(reference),image_path=str(image_path),material_name=material['name'],
        technique_set=native.get('technique_set'),mode=mode,binding_sha256=fingerprint,
        id=str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:source-alpha:'+fingerprint)),exact=False,
        private_preview_only=True,original_shader_recovered=False)
    cache[key]=result
    return result


def _source_alpha_graph(material, evidence, bpy, image_cache):
    try:from . import source_bundle
    except ImportError:import source_bundle
    if material is None or re.sub(r'\.\d{3,}$','',material.name)!=evidence['material_name']:
        raise ValueError('Actual source alpha material name differs from the source binding')
    plan=_material_plan(material)
    if len(plan)!=1:raise ValueError('Source alpha requires exactly one unambiguous unconnected RGBA alpha graph')
    connection=plan[0];nodes=material.node_tree.nodes
    outputs=[node for node in nodes if node.type=='OUTPUT_MATERIAL' and getattr(node,'is_active_output',True)]
    if (len(outputs)!=1 or len(outputs[0].inputs['Surface'].links)!=1
            or outputs[0].inputs['Surface'].links[0].from_node!=nodes[connection['shader']]):
        raise ValueError('Source alpha requires its actual direct Principled surface output')
    image=nodes[connection['texture']].image
    if (image is None or image.channels!=4 or image.source!='FILE' or image.is_dirty or not image.filepath):
        raise ValueError('Source alpha requires an unchanged file-backed RGBA color texture')
    image_key=(image.as_pointer(),evidence['image_sha256'])
    if image_key not in image_cache:
        path=Path(bpy.path.abspath(image.filepath,library=image.library)).resolve()
        source_bundle.verify_file(str(path),evidence['image_sha256'],'Actual source alpha texture')
        if image.packed_file is not None and hashlib.sha256(image.packed_file.data).hexdigest()!=evidence['image_sha256']:
            raise ValueError('Packed source alpha image differs from captured PNG bytes')
        image_cache[image_key]=True
    return plan


def _alpha_material_digest(material, cache, images):
    try:from .preview_render import _material_digest
    except ImportError:from preview_render import _material_digest
    return _material_digest(material,cache,images)


def apply_source_bound_alpha_preview(context, objects, bindings, *, root=None, bpy_module=None):
    """Explicit source/target batch; private material copies and OBJECT overrides.

    Each binding has mode, source{reference_path,reference_sha256,material_index,
    image_sha256}, targets[{object_id,slot_index}]. Names never identify objects.
    """
    if bpy_module is None:import bpy as bpy_module
    bpy=bpy_module;objects=_source_alpha_objects(context,objects)
    root=Path(root or context.scene.wz09_project_root).resolve()
    if not root.is_dir() or not isinstance(bindings,list) or objects and not bindings:
        raise ValueError('Source alpha preview requires project root and explicit source bindings')
    evidence_cache={};image_cache={};graph_cache={};graph_images={};plan_cache={};pending=[];already=0;seen=set();states={};proofs={};target_indices={}
    originals={}
    for binding in bindings:
        if not isinstance(binding,dict) or set(binding)!={'mode','source','targets'} or not isinstance(binding['targets'],list) or not binding['targets']:
            raise ValueError('Explicit source alpha target records are required')
        evidence=_source_alpha_evidence(binding['source'],binding['mode'],root,evidence_cache);proofs[evidence['id']]=evidence
        for target in binding['targets']:
            if not isinstance(target,dict) or set(target)!={'object_id','slot_index'} or target['object_id'] not in objects:
                raise ValueError('Source alpha target must reference a supplied persistent object ID')
            obj=objects[target['object_id']];index=target['slot_index'];key=(target['object_id'],index)
            if type(index) is not int or not 0<=index<len(obj.material_slots) or key in seen:
                raise ValueError('Invalid or duplicate source alpha target slot')
            seen.add(key);slot=obj.material_slots[index]
            if obj.as_pointer() not in states:
                state=json.loads(obj.get(_SOURCE_STATE,'{"slots":{}}'));indices=json.loads(obj.get(_STATE,'[]'))
                if (not isinstance(indices,list) or any(type(i) is not int or not 0<=i<len(obj.material_slots) for i in indices)
                        or len(indices)!=len(set(indices)) or not isinstance(state,dict) or set(state)!={'slots'} or not isinstance(state['slots'],dict)
                        or set(map(str,indices))!=set(state['slots'])):
                    raise ValueError('Existing alpha preview lacks matching source-bound restoration evidence')
                states[obj.as_pointer()]=(obj,state,indices)
            state=states[obj.as_pointer()][1];existing=state['slots'].get(str(index))
            material=obj.get('wz09_alpha_original_material_'+str(index)) if existing else slot.material
            plan_key=(material.as_pointer() if material is not None else None,evidence['binding_sha256'])
            if plan_key not in plan_cache:plan_cache[plan_key]=_source_alpha_graph(material,evidence,bpy,image_cache)
            plan=plan_cache[plan_key]
            source_digest=_alpha_material_digest(material,graph_cache,graph_images);originals[material.as_pointer()]=(material,source_digest)
            if existing:
                preview=obj.get('wz09_alpha_preview_material_'+str(index))
                if (existing['binding_sha256']!=evidence['binding_sha256'] or existing['source_material_sha256']!=source_digest
                        or preview is None or slot.link!='OBJECT' or slot.material!=preview
                        or _alpha_material_digest(preview,graph_cache,graph_images)!=existing['preview_material_sha256']):
                    raise ValueError('Source-bound alpha preview was edited or differs from the requested binding')
                already+=1;continue
            pending.append((obj,index,slot.link,material,plan,evidence,target['object_id'],source_digest))
            target_indices.setdefault(obj.as_pointer(),[]).append(index)
    created=[];copies={};assigned=[];property_snapshots={}
    try:
        for obj,index,link,material,plan,evidence,identity,source_digest in pending:
            cache_key=(evidence['binding_sha256'],source_digest)
            if cache_key in copies:continue
            preview=material.copy();created.append(preview)
            preview.name=material.name+' · Source alpha preview';preview['wz09_preview_alpha_inferred']=True
            preview['wz09_source_alpha_preview_json']=json.dumps(evidence,sort_keys=True)
            preview['wz09_source_alpha_preview_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,evidence['id']+':'+source_digest))
            _dithered(preview)
            for connection in plan:
                shader=preview.node_tree.nodes[connection['shader']];texture=preview.node_tree.nodes[connection['texture']]
                if evidence['mode']=='texture_alpha':preview.node_tree.links.new(texture.outputs['Alpha'],shader.inputs['Alpha'])
                else:shader.inputs['Alpha'].default_value=0.
            copies[cache_key]=(preview,_alpha_material_digest(preview,{},graph_images))
        for obj,index,link,material,plan,evidence,identity,source_digest in pending:
            if obj.as_pointer() not in property_snapshots:
                keys={_STATE,_SOURCE_STATE}
                keys.update(prefix+str(i) for i in target_indices[obj.as_pointer()] for prefix in (
                    'wz09_alpha_original_material_','wz09_alpha_original_link_','wz09_alpha_preview_material_'))
                property_snapshots[obj.as_pointer()]=(obj,keys,{key:obj[key] for key in keys if key in obj})
            preview,preview_digest=copies[(evidence['binding_sha256'],source_digest)]
            assigned.append((obj,index,link,material));slot=obj.material_slots[index]
            obj['wz09_alpha_original_material_'+str(index)]=material;obj['wz09_alpha_original_link_'+str(index)]=link
            obj['wz09_alpha_preview_material_'+str(index)]=preview
            slot.link='OBJECT';slot.material=preview
            if slot.link!='OBJECT' or slot.material!=preview:raise ValueError('Private alpha object-slot override readback differs')
            state=states[obj.as_pointer()][1]
            state['slots'][str(index)]=dict(binding_sha256=evidence['binding_sha256'],binding_id=evidence['id'],
                mode=evidence['mode'],source_material_sha256=source_digest,preview_material_sha256=preview_digest)
        for obj,state,_indices in states.values():
            if obj.as_pointer() in property_snapshots:
                obj[_STATE]=json.dumps(sorted(map(int,state['slots'])))
                obj[_SOURCE_STATE]=json.dumps(state,sort_keys=True)
        try:from . import source_bundle
        except ImportError:import source_bundle
        for evidence in evidence_cache.values():
            source_bundle.verify_file(evidence['reference_path'],evidence['reference_sha256'],'Source alpha glTF after batch')
            source_bundle.verify_file(evidence['image_path'],evidence['image_sha256'],'Source alpha image after batch')
        after_images={}
        for material,before in originals.values():
            if _alpha_material_digest(material,{},after_images)!=before:raise ValueError('Original source alpha material changed during batch')
    except Exception:
        for obj,index,link,material in reversed(assigned):
            slot=obj.material_slots[index];slot.link='OBJECT';slot.material=material;slot.link=link
        for obj,keys,before in property_snapshots.values():
            for key in keys:
                if key in before:obj[key]=before[key]
                elif key in obj:del obj[key]
        for material in reversed(created):
            if material.users==0:bpy.data.materials.remove(material)
        raise
    return dict(materials=len(created),slots=len(assigned),already_applied_slots=already,inferred=True,exact=False,
        private_preview_only=True,original_shader_recovered=False,source_files_before_after_equal=True,
        sources=sorted(proofs.values(),key=lambda row:row['id']),
        changes=[dict(object_id=identity,slot_index=index,mode=evidence['mode'],binding_id=evidence['id'])
                 for obj,index,link,material,plan,evidence,identity,source_digest in pending])


def apply_texture_alpha(context, *, bpy_module=None):
    if bpy_module is None:
        import bpy as bpy_module
    bpy = bpy_module
    root = _root(context)
    parts = _parts(context, root)
    pending = []
    for obj in parts:
        if obj.get(_STATE):
            continue
        for index, slot in enumerate(obj.material_slots):
            plan = _material_plan(slot.material)
            if plan:
                pending.append((obj, index, slot.link, slot.material, plan))
    if not pending:
        return {'materials': 0, 'slots': 0, 'inferred': True}
    created, assigned, copies = [], [], {}
    try:
        # Prepare all material copies first; source material nodes stay untouched.
        for _obj, _index, _link, material, plan in pending:
            key = material.as_pointer()
            if key in copies:
                continue
            preview = material.copy()
            created.append(preview)
            preview.name = material.name+' · Texture alpha preview'
            preview['wz09_preview_alpha_inferred'] = True
            _dithered(preview)
            for connection in plan:
                shader = preview.node_tree.nodes[connection['shader']]
                texture = preview.node_tree.nodes[connection['texture']]
                preview.node_tree.links.new(texture.outputs['Alpha'], shader.inputs['Alpha'])
            copies[key] = preview
        states = {}
        for obj, index, link, material, _plan in pending:
            slot = obj.material_slots[index]
            obj[f'wz09_alpha_original_material_{index}'] = material
            obj[f'wz09_alpha_original_link_{index}'] = link
            obj[f'wz09_alpha_preview_material_{index}'] = copies[material.as_pointer()]
            assigned.append((obj, index, link, material))
            slot.link = 'OBJECT'
            slot.material = copies[material.as_pointer()]
            states.setdefault(obj.as_pointer(), (obj, []))[1].append(index)
        for obj, indices in states.values():
            obj[_STATE] = json.dumps(indices)
        root['wz09_preview_alpha_inferred'] = True
        return {'materials': len(copies), 'slots': len(assigned), 'inferred': True}
    except Exception:
        for obj, index, link, material in reversed(assigned):
            slot = obj.material_slots[index]
            slot.link = 'OBJECT'
            slot.material = material
            slot.link = link
        for obj, index, _link, _material, _plan in pending:
            for key in (f'wz09_alpha_original_material_{index}', f'wz09_alpha_original_link_{index}', f'wz09_alpha_preview_material_{index}'):
                if key in obj:
                    del obj[key]
            if _STATE in obj:
                del obj[_STATE]
        for material in created:
            if material.users == 0:
                bpy.data.materials.remove(material)
        raise


def _restore_alpha_parts(parts, bpy):
    pending, copies, snapshots = [], {}, {}
    for obj in parts:
        keys={_STATE,_SOURCE_STATE}
        for index in json.loads(obj.get(_STATE, '[]')):
            if type(index) is not int or not 0 <= index < len(obj.material_slots):
                raise ValueError('Asset material slots changed; cannot restore automatically')
            material = obj.get(f'wz09_alpha_original_material_{index}')
            link = obj.get(f'wz09_alpha_original_link_{index}')
            preview = obj.get(f'wz09_alpha_preview_material_{index}')
            if material is None or link not in {'OBJECT', 'DATA'} or preview is None:
                raise ValueError('Original alpha-preview material binding is missing')
            if obj.material_slots[index].material != preview:
                raise ValueError('Preview slot was edited afterwards; restore it explicitly before automatic reset')
            pending.append((obj, index, material, link,preview,obj.material_slots[index].link))
            copies[preview.as_pointer()] = preview
            keys.update((f'wz09_alpha_original_material_{index}',f'wz09_alpha_original_link_{index}',f'wz09_alpha_preview_material_{index}'))
        snapshots[obj.as_pointer()]=(obj,keys,{key:obj[key] for key in keys if key in obj})
    changed=[]
    try:
        for obj,index,material,link,preview,preview_link in pending:
            changed.append((obj,index,preview,preview_link));slot=obj.material_slots[index]
            slot.link='OBJECT';slot.material=material;slot.link=link
        for obj,keys,_before in snapshots.values():
            for key in keys:
                if key in obj:del obj[key]
    except Exception:
        for obj,index,preview,link in reversed(changed):
            slot=obj.material_slots[index];slot.link='OBJECT';slot.material=preview;slot.link=link
        for obj,keys,before in snapshots.values():
            for key,value in before.items():obj[key]=value
        raise
    for material in copies.values():
        if material.users == 0:
            bpy.data.materials.remove(material)
    return {'slots': len(pending), 'restored': True}


def restore_source_bound_alpha_preview(context, objects, *, bpy_module=None):
    """Restore only tracked source-bound overrides on supplied local objects."""
    if bpy_module is None:import bpy as bpy_module
    parts=_source_alpha_objects(context,objects).values()
    return _restore_alpha_parts([obj for obj in parts if obj.get(_SOURCE_STATE)],bpy_module)


def restore_texture_alpha(context, *, bpy_module=None):
    if bpy_module is None:import bpy as bpy_module
    root=_root(context);result=_restore_alpha_parts(_parts(context,root),bpy_module)
    if 'wz09_preview_alpha_inferred' in root:del root['wz09_preview_alpha_inferred']
    return result


def register():
    import bpy
    global _CLASSES

    class WZ09_OT_asset_texture_alpha(bpy.types.Operator):
        bl_idname = 'wz09.asset_texture_alpha'
        bl_label = 'Texture alpha (preview)'
        bl_description = 'Selected asset copy only: preview RGBA texture alpha as transparency. Native IW4 coverage is not reconstructed'
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            try:
                result = apply_texture_alpha(context)
                # Solid mode cannot display material transparency. The explicit
                # preview action changes this viewport only, never source data.
                space = getattr(context, 'space_data', None)
                shading = getattr(space, 'shading', None)
                if getattr(space, 'type', None) == 'VIEW_3D' and shading is not None:
                    prop = shading.bl_rna.properties.get('type')
                    if prop is not None and 'MATERIAL' in {item.identifier for item in prop.enum_items}:
                        shading.type = 'MATERIAL'
                        if hasattr(shading, 'use_scene_lights'):
                            shading.use_scene_lights = False
                        if hasattr(shading, 'use_scene_world'):
                            shading.use_scene_world = False
                self.report({'INFO'}, f"{result['slots']} material slots - derived texture alpha preview")
                return {'FINISHED'}
            except (ValueError, RuntimeError, TypeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:230])
                return {'CANCELLED'}

    class WZ09_OT_asset_restore_alpha(bpy.types.Operator):
        bl_idname = 'wz09.asset_restore_alpha'
        bl_label = 'Original material'
        bl_description = 'Restore the original material link of the selected asset copy'
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            try:
                result = restore_texture_alpha(context)
                self.report({'INFO'}, f"{result['slots']} Materialslots wiederhergestellt")
                return {'FINISHED'}
            except (ValueError, RuntimeError, TypeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:230])
                return {'CANCELLED'}

    _CLASSES = [WZ09_OT_asset_texture_alpha, WZ09_OT_asset_restore_alpha]
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    import bpy
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
