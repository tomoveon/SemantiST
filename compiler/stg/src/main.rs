use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use semantist_stg::{generate, GenerateOptions};

#[derive(Debug, Parser)]
#[command(
    name = "semantist-stg",
    about = "Generate compiler-semantic STG models from RuSTy Structured Text projects"
)]
struct Args {
    #[arg(required = true)]
    inputs: Vec<PathBuf>,

    #[arg(long, default_value = ".")]
    project_root: PathBuf,

    #[arg(short, long, default_value = "artifacts/stg")]
    output: PathBuf,

    #[arg(long, action = clap::ArgAction::Append)]
    pou: Vec<String>,

    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    pretty: bool,

    #[arg(long, default_value_t = false, action = clap::ArgAction::Set)]
    dot: bool,

    #[arg(long, default_value_t = false, action = clap::ArgAction::Set)]
    debug_sidecars: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let manifest = generate(&GenerateOptions {
        inputs: args.inputs,
        project_root: args.project_root,
        output_dir: args.output,
        pou_filters: args.pou,
        pretty: args.pretty,
        emit_dot: args.dot,
        emit_debug_sidecars: args.debug_sidecars,
    })?;
    println!("{}", serde_json::to_string_pretty(&manifest)?);
    Ok(())
}
