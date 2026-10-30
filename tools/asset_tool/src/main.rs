//! Standalone read-only map/model converter. No gameplay entrypoint or App.
#![allow(dead_code)]
use std::{fs,path::PathBuf};
pub use assets::{PreparedWorld,MatchMaterials};
mod writer {
    include!("gltf_export.rs");
    include!("semantic_append.rs");
}

fn run()->Result<(),String> {
    let args:Vec<_>=std::env::args().collect();
    if args.len()==2 && args[1]=="--version" {
        println!("wz09-asset-tool 0.10.0; offline map/model conversion only");return Ok(())
    }
    if args.len()!=6 || !matches!(args[1].as_str(),"map"|"models") {
        return Err("Usage: wz09-asset-tool map|models ORIGINAL_MAP_FF ORIGINAL_COMMON_FF NEW_OUTPUT MAP_SHA256".into())
    }
    let source=PathBuf::from(&args[2]); let common=PathBuf::from(&args[3]); let output=PathBuf::from(&args[4]);
    if output.exists() {return Err("Output already exists; select a new editor cache directory".into())}
    let map=source.file_stem().and_then(|s|s.to_str()).ok_or("Invalid map filename")?;
    if !matches!(map,"mp_afghan"|"mp_derail"|"mp_estate"|"mp_favela"|"mp_highrise"|"mp_invasion"|
        "mp_checkpoint"|"mp_quarry"|"mp_rundown"|"mp_rust"|"mp_boneyard"|"mp_nightshift"|
        "mp_subbase"|"mp_terminal"|"mp_underpass"|"mp_brecourt") {return Err("Unsupported multiplayer map".into())}
    let progress=asset_transport::LoadProgress::new(1);
    let result=bevy::tasks::futures_lite::future::block_on(assets::load_prepared_match(Ok(source.clone()),Ok(common),progress));
    let assets::MatchLoadOutcome::Ready(prepared)=result else {return Err("Offline asset read cancelled".into())};
    if args[1]=="map" {
        let result=writer::export_prepared_world_gltf(&output,map,prepared.world,&prepared.materials)?;
        println!("{}",serde_json::json!({"scene":result.scene,"primitives":result.primitives,"triangles":result.triangles,"game_started":false}));
    } else {
        let report=writer::export_semantic_visuals(&output,map,&args[5],&prepared)?;
        fs::write(output.join("native-visuals.json"),serde_json::to_vec_pretty(&report).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        println!("{}",serde_json::json!({"meshes":report["meshes"],"triangles":report["triangles"],"game_started":false}));
    }
    Ok(())
}

fn main() {
    let result=std::thread::Builder::new().name("wz09-offline-assets".into()).stack_size(64*1024*1024)
        .spawn(run).unwrap().join().unwrap_or_else(|_|Err("Offline reader failed".into()));
    if let Err(error)=result {eprintln!("wz09-asset-tool: {error}");std::process::exit(2)}
}
