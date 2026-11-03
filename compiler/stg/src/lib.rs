pub mod compiler;
pub mod dot;
pub mod extract;
pub mod instrumentation;
pub mod ir;
pub mod model;
pub mod stable_id;
pub mod validate;

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use compiler::compile_project;
use extract::extract_project;
use instrumentation::{build_codegen_map, build_runtime_ids};
use model::{ArtifactManifest, SCHEMA_VERSION};
use sha2::{Digest, Sha256};

pub const RUSTY_REVISION: &str = "be1de6f175ec1bed7928904dfb694208a24043fb";
pub const RUSTY_VERSION: &str = "0.5.0";

#[derive(Clone, Debug)]
pub struct GenerateOptions {
    pub inputs: Vec<PathBuf>,
    pub project_root: PathBuf,
    pub output_dir: PathBuf,
    pub pou_filters: Vec<String>,
    pub pretty: bool,
    pub emit_dot: bool,
    pub emit_debug_sidecars: bool,
}

pub fn generate(options: &GenerateOptions) -> Result<ArtifactManifest> {
    std::fs::create_dir_all(&options.output_dir)
        .with_context(|| format!("cannot create {}", options.output_dir.display()))?;

    let compilation = compile_project(&options.inputs, &options.project_root)?;
    let mut model = extract_project(compilation, &options.pou_filters)?;
    validate::validate_project(&mut model);

    let model_path = options.output_dir.join("stg-model.json");
    let diagnostics_path = options.output_dir.join("stg-diagnostics.json");
    let statistics_path = options.output_dir.join("stg-statistics.json");
    let codegen_map_path = options.output_dir.join("stg-codegen-map.json");
    let runtime_ids_path = options.output_dir.join("stg-runtime-ids.json");
    let dot_dir = options.output_dir.join("dot");

    write_json(&model_path, &model, options.pretty)?;
    let (diagnostics, statistics) = if options.emit_debug_sidecars {
        write_json(&diagnostics_path, &model.diagnostics, options.pretty)?;
        write_json(&statistics_path, &model.statistics, options.pretty)?;
        (Some(diagnostics_path), Some(statistics_path))
    } else {
        (None, None)
    };
    let runtime_ids = build_runtime_ids(&model);
    let codegen_map = build_codegen_map(&model, &runtime_ids);
    write_json(&runtime_ids_path, &runtime_ids, options.pretty)?;
    write_json(&codegen_map_path, &codegen_map, options.pretty)?;

    let mut dot_files = Vec::new();
    if options.emit_dot {
        std::fs::create_dir_all(&dot_dir)?;
        for pou in &model.pous {
            let file_name = format!("{}.dot", stable_id::file_component(&pou.pou.qualified_name));
            let path = dot_dir.join(file_name);
            std::fs::write(&path, dot::render_pou(pou))?;
            dot_files.push(path);
        }
    }

    let model_sha256 = file_digest(&model_path)?;
    let manifest = ArtifactManifest {
        schema_version: SCHEMA_VERSION.to_string(),
        model: model_path,
        diagnostics,
        statistics,
        dot_files,
        codegen_map: codegen_map_path,
        runtime_ids: runtime_ids_path,
        model_sha256,
    };
    write_json(
        &options.output_dir.join("stg-manifest.json"),
        &manifest,
        options.pretty,
    )?;
    Ok(manifest)
}

fn write_json(path: &Path, value: &impl serde::Serialize, pretty: bool) -> Result<()> {
    let bytes = if pretty {
        serde_json::to_vec_pretty(value)?
    } else {
        serde_json::to_vec(value)?
    };
    std::fs::write(path, bytes).with_context(|| format!("cannot write {}", path.display()))
}

fn file_digest(path: &Path) -> Result<String> {
    Ok(hex::encode(Sha256::digest(std::fs::read(path)?)))
}
