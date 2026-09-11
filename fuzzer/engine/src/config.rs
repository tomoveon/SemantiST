#[derive(Debug)]
struct Config {
    target: PathBuf,
    function: String,
    timeout: Duration,
    corpus_dir: PathBuf,
    seed_dir: PathBuf,
    queue_dir: PathBuf,
    crashes_dir: PathBuf,
    findings_dir: PathBuf,
    metadata_bootstrap: Option<PathBuf>,
    semantic_task_plan: Option<PathBuf>,
    semantic_model: Option<PathBuf>,
    semantic_runtime_ids: Option<PathBuf>,
    events_file: PathBuf,
    semantic_task_budget: u64,
    semantic_task_state_file: PathBuf,
    fuzz_iterations: Option<u64>,
    rng_seed: Option<u64>,
    debug_child: bool,
}

impl Config {
    fn from_args() -> Result<Self, Error> {
        // 默认 fuzz `artifacts/build/targets/fuzz_target`。
        let mut target = PathBuf::from(DEFAULT_TARGET);
        let mut function = env::var("ST_FUNCTION").unwrap_or_else(|_| DEFAULT_FUNCTION.to_string());
        let mut run_dir = env::var("SEMANTIST_RUN_DIR").ok().map(PathBuf::from);
        let mut metadata_bootstrap = env::var("SEMANTIST_METADATA_BOOTSTRAP")
            .ok()
            .map(PathBuf::from);
        let mut semantic_task_plan = env::var("SEMANTIST_SEMANTIC_TASK_PLAN")
            .ok()
            .map(PathBuf::from);
        let mut semantic_model = env::var("SEMANTIST_STG_MODEL").ok().map(PathBuf::from);
        let mut semantic_runtime_ids = env::var("SEMANTIST_SEMANTIC_RUNTIME_IDS")
            .ok()
            .map(PathBuf::from);
        let mut timeout_ms = env::var("SEMANTIST_TIMEOUT_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        let mut semantic_task_budget = env::var("SEMANTIST_SEMANTIC_TASK_BUDGET")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(1_000);
        let mut fuzz_iterations = env::var("SEMANTIST_FUZZ_ITERATIONS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok());
        let mut rng_seed = env::var("SEMANTIST_RNG_SEED")
            .ok()
            .and_then(|value| value.parse::<u64>().ok());
        let mut debug_child = false;

        // 支持显式命令行覆盖默认值。
        let mut args = env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--target" | "-x" => {
                    target = PathBuf::from(next_arg(&mut args, &arg)?);
                }
                "--function" | "-f" => {
                    function = next_arg(&mut args, &arg)?;
                }
                "--run-dir" => {
                    run_dir = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--metadata-bootstrap" => {
                    metadata_bootstrap = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--semantic-task-plan" => {
                    semantic_task_plan = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--semantic-model" => {
                    semantic_model = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--semantic-runtime-ids" => {
                    semantic_runtime_ids = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--timeout-ms" | "-t" => {
                    let raw = next_arg(&mut args, &arg)?;
                    timeout_ms = raw.parse().map_err(|e| {
                        Error::illegal_argument(format!("invalid --timeout-ms value {raw:?}: {e}"))
                    })?;
                }
                "--semantic-task-budget" => {
                    let raw = next_arg(&mut args, &arg)?;
                    semantic_task_budget = raw.parse().map_err(|e| {
                        Error::illegal_argument(format!(
                            "invalid --semantic-task-budget value {raw:?}: {e}"
                        ))
                    })?;
                }
                "--fuzz-iterations" => {
                    let raw = next_arg(&mut args, &arg)?;
                    fuzz_iterations = Some(raw.parse().map_err(|e| {
                        Error::illegal_argument(format!(
                            "invalid --fuzz-iterations value {raw:?}: {e}"
                        ))
                    })?);
                }
                "--seed" => {
                    let raw = next_arg(&mut args, &arg)?;
                    rng_seed = Some(raw.parse().map_err(|e| {
                        Error::illegal_argument(format!("invalid --seed value {raw:?}: {e}"))
                    })?);
                }
                "--debug-child" | "-d" => {
                    debug_child = true;
                }
                "--help" | "-h" => {
                    print_usage();
                    std::process::exit(0);
                }
                other => {
                    return Err(Error::illegal_argument(format!(
                        "unknown argument {other:?}; run with --help"
                    )));
                }
            }
        }

        // function 名用于区分 corpus/crashes/findings 子目录。
        let function = sanitize_name(&function)?;
        let corpus_dir = run_dir
            .as_ref()
            .map(|dir| dir.join("unified-corpus"))
            .unwrap_or_else(|| PathBuf::from("corpus").join(&function));
        let seed_dir = corpus_dir.join("seeds");
        let queue_dir = corpus_dir.join("runtime");
        let crashes_dir = run_dir
            .as_ref()
            .map(|dir| dir.join("target-corpus"))
            .unwrap_or_else(|| PathBuf::from("crashes").join(&function));
        let findings_dir = run_dir
            .as_ref()
            .map(|dir| dir.join("findings"))
            .unwrap_or_else(|| PathBuf::from("findings").join(&function));
        let events_file = run_dir
            .as_ref()
            .map(|dir| dir.join("events.jsonl"))
            .unwrap_or_else(|| findings_dir.join("events.jsonl"));
        if metadata_bootstrap.is_none() {
            metadata_bootstrap = run_dir
                .as_ref()
                .map(|dir| dir.join("testcase-metadata.bootstrap.json"));
        }
        if semantic_task_plan.is_none() {
            semantic_task_plan = run_dir
                .as_ref()
                .map(|dir| dir.join("semantic-task-plan.json"));
        }
        if semantic_model.is_none() {
            semantic_model = run_dir
                .as_ref()
                .map(|dir| dir.join("stg").join("stg-model.json"));
        }
        if semantic_runtime_ids.is_none() {
            semantic_runtime_ids = run_dir
                .as_ref()
                .map(|dir| dir.join("stg").join("stg-runtime-ids.json"));
        }
        let semantic_task_state_file = run_dir
            .as_ref()
            .map(|dir| dir.join("semantic-task-state.json"))
            .unwrap_or_else(|| findings_dir.join("semantic-task-state.json"));

        Ok(Self {
            target,
            function,
            timeout: Duration::from_millis(timeout_ms),
            corpus_dir,
            seed_dir,
            queue_dir,
            crashes_dir,
            findings_dir,
            metadata_bootstrap,
            semantic_task_plan,
            semantic_model,
            semantic_runtime_ids,
            events_file,
            semantic_task_budget: semantic_task_budget.max(1),
            semantic_task_state_file,
            fuzz_iterations,
            rng_seed,
            debug_child,
        })
    }
}

// 读取形如 `--target PATH` 的参数值。
