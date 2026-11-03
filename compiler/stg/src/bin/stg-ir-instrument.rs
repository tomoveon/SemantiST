use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use semantist_stg::ir::{instrument, InstrumentOptions};

#[derive(Debug, Parser)]
#[command(
    name = "stg-ir-instrument",
    about = "Instrument RuSTy LLVM IR using compiler-propagated STG semantic metadata"
)]
struct Args {
    input_ir: PathBuf,

    #[arg(short, long)]
    output: PathBuf,

    #[arg(long)]
    runtime_ids: PathBuf,

    #[arg(long)]
    mapping: PathBuf,

    #[arg(long)]
    diagnostics: Option<PathBuf>,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let artifact = instrument(&InstrumentOptions {
        input_ir: args.input_ir,
        output_ir: args.output,
        runtime_ids: args.runtime_ids,
        mapping_output: args.mapping,
        diagnostics_output: args.diagnostics,
    })?;
    println!("{}", serde_json::to_string_pretty(&artifact.statistics)?);
    Ok(())
}
