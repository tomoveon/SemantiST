#[derive(Debug)]
struct Config {
    target: PathBuf,
    timeout: Duration,
    corpus_dir: PathBuf,
    seed_dir: PathBuf,
    queue_dir: PathBuf,
    crashes_dir: PathBuf,
    findings_dir: PathBuf,
    metadata_bootstrap: Option<PathBuf>,
    semantic_task_plan: Option<PathBuf>,
    semantic_runtime_ids: Option<PathBuf>,
    events_file: PathBuf,
    fuzz_iterations: Option<u64>,
    v3_full_duration: bool,
    rng_seed: Option<u64>,
    run_dir: Option<PathBuf>,
    v3_bundle: Option<PathBuf>,
    v3_time_budget: Duration,
    v3_no_goal_prefix: bool,
    v3_no_distance: bool,
    v3_no_time_guidance: bool,
    v3_no_snapshot: bool,
    v3_no_state_copy: bool,
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
        let mut semantic_runtime_ids = env::var("SEMANTIST_SEMANTIC_RUNTIME_IDS")
            .ok()
            .map(PathBuf::from);
        let mut timeout_ms = env::var("SEMANTIST_TIMEOUT_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        let mut fuzz_iterations = env::var("SEMANTIST_FUZZ_ITERATIONS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok());
        let mut rng_seed = env::var("SEMANTIST_RNG_SEED")
            .ok()
            .and_then(|value| value.parse::<u64>().ok());
        let mut v3_bundle = None;
        let mut v3_time_budget = Duration::from_secs(600);
        let mut v3_full_duration = false;
        let mut v3_no_goal_prefix = false;
        let mut v3_no_distance = false;
        let mut v3_no_time_guidance = false;
        let mut v3_no_snapshot = false;
        let mut v3_no_state_copy = false;

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
                "--semantic-runtime-ids" => {
                    semantic_runtime_ids = Some(PathBuf::from(next_arg(&mut args, &arg)?));
                }
                "--timeout-ms" | "-t" => {
                    let raw = next_arg(&mut args, &arg)?;
                    timeout_ms = raw.parse().map_err(|e| {
                        Error::illegal_argument(format!("invalid --timeout-ms value {raw:?}: {e}"))
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
                "--v3-full-duration" => v3_full_duration = true,
                "--v3-bundle" => v3_bundle = Some(PathBuf::from(next_arg(&mut args, &arg)?)),
                "--v3-time-budget-ms" => {
                    let raw = next_arg(&mut args, &arg)?;
                    v3_time_budget = Duration::from_millis(
                        raw.parse().map_err(|e| Error::illegal_argument(format!("invalid --v3-time-budget-ms {raw:?}: {e}")))?,
                    );
                }
                "--no-goal-prefix" => v3_no_goal_prefix = true,
                "--no-distance" => v3_no_distance = true,
                "--no-time-guidance" => v3_no_time_guidance = true,
                "--no-snapshot" => v3_no_snapshot = true,
                "--no-state-copy" => v3_no_state_copy = true,
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
        if semantic_runtime_ids.is_none() {
            semantic_runtime_ids = run_dir
                .as_ref()
                .map(|dir| dir.join("stg").join("stg-runtime-ids.json"));
        }
        Ok(Self {
            target,
            timeout: Duration::from_millis(timeout_ms),
            corpus_dir,
            seed_dir,
            queue_dir,
            crashes_dir,
            findings_dir,
            metadata_bootstrap,
            semantic_task_plan,
            semantic_runtime_ids,
            events_file,
            fuzz_iterations,
            v3_full_duration,
            rng_seed,
            run_dir,
            v3_bundle,
            v3_time_budget,
            v3_no_goal_prefix,
            v3_no_distance,
            v3_no_time_guidance,
            v3_no_snapshot,
            v3_no_state_copy,
        })
    }
}

// 读取形如 `--target PATH` 的参数值。
