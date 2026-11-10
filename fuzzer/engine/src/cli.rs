fn next_arg(args: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, Error> {
    args.next()
        .ok_or_else(|| Error::illegal_argument(format!("{flag} requires a value")))
}
// 避免 function 名被拿来构造奇怪路径。
fn sanitize_name(name: &str) -> Result<String, Error> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err(Error::illegal_argument("function name must not be empty"));
    }
    if !trimmed
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' || ch == '.')
    {
        return Err(Error::illegal_argument(format!(
            "function name {name:?} is not safe for a directory name"
        )));
    }
    Ok(trimmed.to_ascii_lowercase())
}

// 主程序帮助文本。
fn print_usage() {
    println!(
        "usage: semantist [--target ./artifacts/build/targets/fuzz_target] [--function INC2] [--run-dir artifacts/runs/id] [--timeout-ms 1000]"
    );
    println!(
        "  --run-dir PATH               use PATH/unified-corpus, target-corpus, findings and semantic artifacts"
    );
    println!("  --metadata-bootstrap PATH    load testcase metadata into native LibAFL metadata");
    println!("  --semantic-task-plan PATH    load the STG Stable Target ID task plan");
    println!("  --semantic-runtime-ids PATH  map semantic bitmap IDs back to stable STG targets");
    println!(
        "  --fuzz-iterations N          stop after N LibAFL iterations (useful for smoke tests)"
    );
    println!("  --seed N                     initialize the SemantiST LibAFL RNG deterministically");
    println!("  --v3-bundle PATH             load the native execution bundle");
    println!("  --v3-time-budget-ms N        limit native search time in milliseconds");
}

// fuzzer 启动前创建输出目录。
fn ensure_dirs(config: &Config) -> Result<(), Error> {
    fs::create_dir_all(&config.corpus_dir)?;
    fs::create_dir_all(&config.seed_dir)?;
    fs::create_dir_all(&config.queue_dir)?;
    fs::create_dir_all(&config.crashes_dir)?;
    fs::create_dir_all(&config.findings_dir)?;
    if let Some(parent) = config.events_file.parent() {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}
