use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use plc_diagnostics::diagnostician::Diagnostician;
use plc_driver::pipelines::{AnnotatedProject, BuildPipeline, Pipeline};
use plc_source::SourceContainer;
use sha2::{Digest, Sha256};

use crate::model::InputMetadata;

pub struct CompilerSemanticProject {
    pub source: AnnotatedProject,
    pub lowered: AnnotatedProject,
    pub project_root: PathBuf,
    pub inputs: Vec<InputMetadata>,
    pub source_conventions: SourceConventions,
}

#[derive(Clone, Debug, Default)]
pub struct SourceConventions {
    configuration_input_blocks: Vec<SourceConventionRange>,
}

#[derive(Clone, Debug)]
struct SourceConventionRange {
    file: String,
    start_line: usize,
    end_line: usize,
}

impl SourceConventions {
    pub fn is_configuration_input(&self, file: &str, line: usize) -> bool {
        self.configuration_input_blocks
            .iter()
            .any(|range| range.file == file && range.start_line <= line && line <= range.end_line)
    }

    pub fn has_configuration_annotations(&self) -> bool {
        !self.configuration_input_blocks.is_empty()
    }
}

pub fn compile_project(inputs: &[PathBuf], project_root: &Path) -> Result<CompilerSemanticProject> {
    if inputs.is_empty() {
        bail!("at least one Structured Text source is required");
    }

    let root = project_root
        .canonicalize()
        .with_context(|| format!("cannot resolve project root {}", project_root.display()))?;
    let requested = inputs
        .iter()
        .map(|path| {
            if path.is_absolute() {
                path.clone()
            } else {
                root.join(path)
            }
        })
        .map(|path| {
            path.canonicalize()
                .with_context(|| format!("cannot resolve input {}", path.display()))
        })
        .collect::<Result<Vec<_>>>()?;

    if requested.len() == 1 {
        let candidate = &requested[0];
        let config = if candidate.is_dir() {
            Some(candidate.join("plc.json"))
        } else if candidate
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("plc.json"))
        {
            Some(candidate.clone())
        } else {
            None
        };
        if let Some(config) = config {
            if !config.is_file() {
                bail!("RuSTy project has no plc.json at {}", config.display());
            }
            return compile_build_config(&config, &root);
        }
    }

    let metadata = requested
        .iter()
        .map(|path| input_metadata(path, &root))
        .collect::<Result<Vec<_>>>()?;
    let source_conventions = scan_source_conventions(&requested, &root)?;

    let source =
        run_pipeline(&requested, false).context("RuSTy source AST/type annotation failed")?;
    let lowered =
        run_pipeline(&requested, true).context("RuSTy typed/lowered AST pipeline failed")?;

    Ok(CompilerSemanticProject {
        source,
        lowered,
        project_root: root,
        inputs: metadata,
        source_conventions,
    })
}

fn compile_build_config(config: &Path, root: &Path) -> Result<CompilerSemanticProject> {
    let (source, discovered) =
        run_build_config_pipeline(config, false).context("RuSTy project source pipeline failed")?;
    let (lowered, _) =
        run_build_config_pipeline(config, true).context("RuSTy project lowered pipeline failed")?;
    let mut files = BTreeSet::from([config.to_path_buf()]);
    files.extend(discovered);
    let source_conventions =
        scan_source_conventions(&files.iter().cloned().collect::<Vec<_>>(), root)?;
    let inputs = files
        .iter()
        .filter(|path| path.is_file())
        .map(|path| input_metadata(path, root))
        .collect::<Result<Vec<_>>>()?;
    Ok(CompilerSemanticProject {
        source,
        lowered,
        project_root: root.to_path_buf(),
        inputs,
        source_conventions,
    })
}

fn scan_source_conventions(paths: &[PathBuf], root: &Path) -> Result<SourceConventions> {
    let mut conventions = SourceConventions::default();
    for path in paths.iter().filter(|path| path.is_file()) {
        let Ok(source) = std::fs::read_to_string(path) else {
            continue;
        };
        let relative = path.strip_prefix(root).unwrap_or(path);
        let file = normalize_path(relative);
        let mut block_start = None;
        for (index, line) in source.lines().enumerate() {
            let line_number = index + 1;
            let uppercase = line.to_ascii_uppercase();
            let trimmed = uppercase.trim_start();
            if trimmed.starts_with("VAR_INPUT")
                && uppercase.contains("(*")
                && uppercase.contains("CONSTANT")
                && uppercase.contains("*)")
            {
                block_start = Some(line_number + 1);
            } else if block_start.is_some() && trimmed.starts_with("END_VAR") {
                let start_line = block_start.take().expect("checked above");
                if start_line < line_number {
                    conventions
                        .configuration_input_blocks
                        .push(SourceConventionRange {
                            file: file.clone(),
                            start_line,
                            end_line: line_number - 1,
                        });
                }
            }
        }
    }
    conventions.configuration_input_blocks.sort_by(|a, b| {
        (&a.file, a.start_line, a.end_line).cmp(&(&b.file, b.start_line, b.end_line))
    });
    Ok(conventions)
}

fn run_pipeline(sources: &[PathBuf], lower: bool) -> Result<AnnotatedProject> {
    let mut pipeline =
        BuildPipeline::from_sources("semantist-stg", sources.to_vec(), Diagnostician::buffered())
            .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    if lower {
        pipeline.register_default_mut_participants();
    }

    let parsed = pipeline
        .parse()
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    let indexed = pipeline
        .index(parsed)
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    let annotated = pipeline
        .annotate(indexed)
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;

    if lower {
        annotated
            .validate(&pipeline.context, &mut pipeline.diagnostician)
            .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    }
    Ok(annotated)
}

fn run_build_config_pipeline(
    config: &Path,
    lower: bool,
) -> Result<(AnnotatedProject, BTreeSet<PathBuf>)> {
    let args = vec![
        "plc".to_string(),
        "build".to_string(),
        config.to_string_lossy().into_owned(),
        "--error-format=none".to_string(),
    ];
    let mut pipeline = BuildPipeline::new(&args)?;
    let mut discovered = BTreeSet::new();
    discovered.extend(
        pipeline
            .project
            .get_sources()
            .iter()
            .filter_map(SourceContainer::get_location)
            .map(Path::to_path_buf),
    );
    discovered.extend(
        pipeline
            .project
            .get_includes()
            .iter()
            .filter_map(SourceContainer::get_location)
            .map(Path::to_path_buf),
    );
    for library in pipeline.project.get_libraries() {
        discovered.extend(
            library
                .get_includes()
                .iter()
                .filter_map(SourceContainer::get_location)
                .map(Path::to_path_buf),
        );
    }
    if lower {
        pipeline.register_default_mut_participants();
    }
    let parsed = pipeline
        .parse()
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    let indexed = pipeline
        .index(parsed)
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    let annotated = pipeline
        .annotate(indexed)
        .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    if lower {
        annotated
            .validate(&pipeline.context, &mut pipeline.diagnostician)
            .map_err(|diagnostic| anyhow::anyhow!("{diagnostic}"))?;
    }
    Ok((annotated, discovered))
}

fn input_metadata(path: &Path, root: &Path) -> Result<InputMetadata> {
    let bytes = std::fs::read(path)?;
    let relative = path.strip_prefix(root).unwrap_or(path);
    Ok(InputMetadata {
        relative_path: normalize_path(relative),
        sha256: hex::encode(Sha256::digest(bytes)),
    })
}

pub fn normalize_path(path: &Path) -> String {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}
