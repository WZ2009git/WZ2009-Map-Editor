// This module is appended beside an UNCHANGED snapshot of gltf_export.rs.
// Geometry/material/image encoding is delegated to that existing GltfBuilder.
use asset_world::{MapXModelAssetKey,MapXModelSceneAsset};
use bevy::asset::RenderAssetUsages;
use bevy::prelude::Mesh;

fn semantic_mesh(out:&mut GltfBuilder,prepared:&assets::PreparedMatch,name:&str)->Option<usize> {
    let key=MapXModelAssetKey(name.to_owned());
    let Some(MapXModelSceneAsset::Iw4(skel))=prepared.world.map_xmodel_scene_assets.get(&key) else {
        out.refuse("SemanticModelUnavailable");return None
    };
    let mut primitives=Vec::new();
    for surface in skel.surfaces_for_lod(0) {
        let Some(&(start,count))=skel.surface_vertex_ranges.get(surface) else {out.refuse("SemanticVertexRangeMissing");continue};
        let Some(&(is,ic))=skel.surface_index_ranges.get(surface) else {out.refuse("SemanticIndexRangeMissing");continue};
        let Some(positions)=skel.positions.get(start..start+count) else {out.refuse("SemanticVertexRangeInvalid");continue};
        let Some(indices)=skel.indices.get(is..is+ic) else {out.refuse("SemanticIndexRangeInvalid");continue};
        if indices.iter().any(|i|(*i as usize)<start || (*i as usize)>=start+count) {out.refuse("SemanticIndicesOutsideSurface");continue}
        let material=prepared.world.map_xmodel_scene_assets.surface_material(&key,surface).map(|i|i.order())
            .or_else(||skel.surface_materials.get(surface).copied().flatten().and_then(|local|prepared.materials.map_ids.get(local.get()).copied().flatten()));
        let Some(material)=material else {out.refuse("MaterialUnbound");continue};
        let material=match out.material(material,&prepared.materials.population) {Ok(v)=>v,Err(reason)=>{out.refuse(reason);continue}};
        let mut mesh=Mesh::new(PrimitiveTopology::TriangleList,RenderAssetUsages::MAIN_WORLD);
        mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION,positions.to_vec());
        if let Some(v)=skel.normals.get(start..start+count) {mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL,v.to_vec());}
        if let Some(v)=skel.colors.get(start..start+count) {mesh.insert_attribute(Mesh::ATTRIBUTE_COLOR,v.to_vec());}
        if let Some(v)=skel.uvs.get(start..start+count) {mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0,v.to_vec());}
        mesh.insert_indices(Indices::U32(indices.iter().map(|i|*i-start as u32).collect()));
        match out.primitive(&mesh,material) {Ok(p)=>primitives.push(p),Err(reason)=>out.refuse(reason)}
    }
    if primitives.is_empty() {out.refuse("SemanticNoExportableSurfaces");return None}
    let index=out.meshes.len();out.meshes.push(json!({"name":name,"primitives":primitives}));Some(index)
}

fn semantic_trs(origin:[f32;3],rotation:Quat)->Option<([f32;3],[f32;4])> {
    let conversion=basis_conversion();
    let rotation=Quat::from_mat3(&(conversion*Mat3::from_quat(rotation)*conversion.transpose())).normalize();
    if !origin.iter().all(|v|v.is_finite()) || !rotation.is_finite() {return None}
    Some((convert_position(origin),rotation.to_array()))
}

pub fn export_semantic_visuals(output:&Path,map:&str,native_sha:&str,prepared:&assets::PreparedMatch)->Result<Value,String> {
    let mut out=GltfBuilder::default();
    let mut cached=HashMap::<String,Option<usize>>::new();
    let mut records=Vec::new();let mut placed=Vec::new();let mut library=Vec::new();
    for instance in &prepared.world.script_model_instances {
        let name=&instance.current_model.0;
        let mesh=*cached.entry(name.clone()).or_insert_with(||semantic_mesh(&mut out,prepared,name));
        let source=instance.id.source_ordinal();
        let trs=semantic_trs(instance.transform.translation.to_array(),instance.transform.rotation);
        let mut node=None;
        if let (Some(mesh),Some((translation,rotation)))=(mesh,trs) {
            node=Some(out.nodes.len());placed.push(out.nodes.len());
            out.nodes.push(json!({"name":format!("{name}#script-{source}"),"mesh":mesh,"translation":translation,"rotation":rotation,"scale":[1,1,1],
                "extras":{"iw4l":{"model":name,"source_kind":"script_model","source_ordinal":source,"native_source_sha256":native_sha,
                    "native_transform":{"origin":instance.transform.translation.to_array(),"quaternion_xyzw":instance.transform.rotation.to_array()},"preview_state":"retained_bind_pose","runtime_integrated":false}}}));
            out.placements+=1;
        }
        records.push(json!({"source_kind":"script_model","source_ordinal":source,"model":name,"node_index":node,"mesh_index":mesh,
            "native_origin":instance.transform.translation.to_array(),"native_quaternion_xyzw":instance.transform.rotation.to_array(),
            "status":if node.is_some(){"exported"}else{"missing_geometry_or_material"},"runtime_integrated":false}));
    }
    for instance in prepared.world.dyn_ents.models.iter().chain(&prepared.world.dyn_ents.brushes) {
        let name=instance.xmodel.as_deref();
        let mesh=name.and_then(|n|*cached.entry(n.to_owned()).or_insert_with(||semantic_mesh(&mut out,prepared,n)));
        let trs=semantic_trs(instance.origin,Quat::from_array(instance.quat));
        let mut node=None;let draw_type=format!("{:?}",instance.draw_type);
        if let (Some(name),Some(mesh),Some((translation,rotation)))=(name,mesh,trs) {
            node=Some(out.nodes.len());placed.push(out.nodes.len());
            out.nodes.push(json!({"name":format!("{name}#dyn-{draw_type}-{}",instance.index),"mesh":mesh,"translation":translation,"rotation":rotation,"scale":[1,1,1],
                "extras":{"iw4l":{"model":name,"source_kind":"dyn_ent","source_index":instance.index,"draw_type":draw_type,"native_type":instance.ty.as_u8(),
                    "native_source_sha256":native_sha,"native_transform":{"origin":instance.origin,"quaternion_xyzw":instance.quat},"preview_state":"retained_bind_pose","runtime_integrated":false}}}));
            out.placements+=1;
        }
        records.push(json!({"source_kind":"dyn_ent","source_index":instance.index,"draw_type":draw_type,"native_type":instance.ty.as_u8(),"model":name,"node_index":node,"mesh_index":mesh,
            "native_origin":instance.origin,"native_quaternion_xyzw":instance.quat,"status":if node.is_some(){"exported"}else{"missing_xmodel_geometry_or_material"},"runtime_integrated":false}));
    }
    let mut state_models=Vec::new();
    for hint in &prepared.destructible_death {
        if hint.husk_hint.is_empty() {continue}
        let name=hint.husk_hint;
        let mesh=*cached.entry(name.to_owned()).or_insert_with(||semantic_mesh(&mut out,prepared,name));
        let mut node=None;
        if let Some(mesh)=mesh {
            node=Some(out.nodes.len());library.push(out.nodes.len());
            out.nodes.push(json!({"name":format!("State asset Â· {name}"),"mesh":mesh,
                "extras":{"iw4l":{"model":name,"source_kind":"destruction_state_asset","behavior_kind":hint.kind,
                    "asset_only":true,"native_source_sha256":native_sha,"runtime_integrated":false}}}));
        }
        state_models.push(json!({"behavior_kind":hint.kind,"model":name,"clip_hint":hint.clip_hint,
            "husk_edge":hint.husk.edge_kind(),"clip_edge":hint.clip.edge_kind(),"node_index":node,"mesh_index":mesh}));
    }
    if out.primitives==0 {return Err(format!("No native semantic geometry exported: {:?}",out.refusals))}
    let library_node=out.nodes.len();out.nodes.push(json!({"name":"Semantic state asset library Â· hide in editor","children":library,"extras":{"iw4l":{"asset_library":true}}}));
    let placed_node=out.nodes.len();out.nodes.push(json!({"name":"Actual native ScriptModels and DynEnts","children":placed}));
    let root=out.nodes.len();out.nodes.push(json!({"name":format!("{map} Â· semantic visuals"),"children":[placed_node,library_node]}));
    let document=json!({"asset":{"version":"2.0","generator":"WZ2009 native semantics geometry (existing iw4l P1a writer)"},"scene":0,
        "scenes":[{"name":format!("{map} native semantic visuals"),"nodes":[root]}],"nodes":out.nodes,"meshes":out.meshes,
        "materials":out.materials,"textures":out.textures,"images":out.images,"samplers":out.samplers,
        "accessors":out.accessors,"bufferViews":out.buffer_views,"buffers":[{"uri":"scene.bin","byteLength":out.bin.len()}]});
    fs::create_dir_all(output.join("textures")).map_err(|e|e.to_string())?;
    fs::write(output.join("scene.bin"),&out.bin).map_err(|e|e.to_string())?;
    for image in &out.pending_images {write_png(&output.join("textures").join(&image.file_name),image)?;}
    fs::write(output.join("scene.gltf"),serde_json::to_vec_pretty(&document).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    Ok(json!({"kind":"wz09_native_semantic_visuals","version":1,"source_map":map,"native_source_sha256":native_sha,"coordinate_system":"gltf_y_up_meters_export_convention",
        "physical_scale_verified":false,"runtime_integrated":false,"collision_verified":false,"preview_state":"retained_bind_pose_no_script_vm",
        "scene":"scene.gltf","records":records,"state_models":state_models,"placements":out.placements,"triangles":out.triangles,
        "materials":out.materials.len(),"images":out.images.len(),"meshes":out.meshes.len(),"refusals":out.refusals,"projections":out.projections}))
}
