include!("frames.rs");
include!("v3.rs");
include!("v3_observer.rs");
include!("v3_stage.rs");
include!("v3_exec.rs");
#[path = "../../semantic/runtime_module.rs"]
mod semantic;

use libafl::{
    corpus::{Corpus, CorpusId, InMemoryOnDiskCorpus, OnDiskCorpus},
    executors::{ExitKind, HasObservers},
    feedback_or,
    feedbacks::{Feedback, MaxMapFeedback, StateInitializer, TimeFeedback},
    fuzzer::{ExecutesInput, StdFuzzer},
    inputs::BytesInput,
    monitors::SimpleMonitor,
    observers::{CanTrack, HitcountsMapObserver, MapObserver, StdMapObserver, TimeObserver},
    state::{HasCorpus, HasExecutions, StdState},
    Error, HasMetadata, SerdeAny,
};
use libafl_bolts::{
    current_nanos,
    rands::{Rand, StdRand},
    tuples::{tuple_list, Handle, Handled, MatchName, MatchNameRef},
    AsIter, AsSliceMut, Named,
};
use semantic::{ActiveSelection, ControllableSeedField, SeedProfile, SemanticRuntimeMetadata};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    borrow::Cow,
    collections::{BTreeMap, HashMap, HashSet},
    env, fs,
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const DEFAULT_TARGET: &str = "./artifacts/build/targets/fuzz_target";
const DEFAULT_FUNCTION: &str = "inc2";
const DEFAULT_TIMEOUT_MS: u64 = 1_000;
const MAP_SIZE: usize = 65_536;
const DEFAULT_FB_MAX_CYCLES: usize = 256;
const HARD_FB_MAX_CYCLES: usize = 10_000;

fn fb_max_cycles() -> usize {
    static LIMIT: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *LIMIT.get_or_init(|| {
        std::env::var("SEMANTIST_FB_MAX_CYCLES")
            .ok()
            .and_then(|value| value.trim().parse::<usize>().ok())
            .filter(|value| *value > 0)
            .unwrap_or(DEFAULT_FB_MAX_CYCLES)
            .min(HARD_FB_MAX_CYCLES)
    })
}

include!("../../semantic/json_text.rs");
include!("../../semantic/metadata.rs");
include!("runtime_feedback.rs");
include!("../../semantic/observers.rs");
include!("../../semantic/io.rs");
include!("config.rs");
include!("cli.rs");

// Values shared by the V3 executor, feedback and objective.
struct CampaignSetup {
    started: std::time::Instant,
    config: Config,
    sanitizer_log_dir: PathBuf,
    runtime_ids: RuntimeIdTable,
    target_names: Vec<String>,
    mapped_semantic_targets: HashSet<String>,
    semantic_plan: semantic::SemanticTaskPlan,
    semantic_map_size: usize,
    // Set by the backend: every sanitizer report ends the execution, so a new
    // sanitizer log can only follow a crash or timeout.
    sanitizer_reports_end_execution: bool,
}

fn prepare_campaign(config: Config, started: std::time::Instant) -> Result<CampaignSetup, Error> {
    env::set_var("SEMANTIST_FB_MAX_CYCLES", fb_max_cycles().to_string());
    if let Some(path) = config.semantic_runtime_ids.as_deref() {
        env::set_var("SEMANTIST_SEMANTIC_RUNTIME_IDS", path);
    }
    ensure_dirs(&config)?;
    let sanitizer_log_dir = config.findings_dir.join("sanitizer-logs");
    fs::create_dir_all(&sanitizer_log_dir)?;

    let runtime_ids = load_runtime_id_table(config.semantic_runtime_ids.as_deref())?;
    let target_names = stable_target_names(&runtime_ids);
    let mapped_semantic_targets =
        load_mapped_semantic_targets(config.semantic_runtime_ids.as_deref(), &runtime_ids)?;
    let semantic_plan_path = config.semantic_task_plan.as_deref().ok_or_else(|| {
        Error::illegal_argument(
            "compiler-semantic fuzzing requires --semantic-task-plan or SEMANTIST_SEMANTIC_TASK_PLAN",
        )
    })?;
    let semantic_plan = semantic::load_plan(semantic_plan_path)
        .map_err(|message| Error::illegal_argument(message))?;
    let semantic_map_size = runtime_ids
        .entries
        .iter()
        .map(|entry| entry.runtime_id as usize + 1)
        .max()
        .unwrap_or(1);
    Ok(CampaignSetup {
        started,
        config,
        sanitizer_log_dir,
        runtime_ids,
        target_names,
        mapped_semantic_targets,
        semantic_plan,
        semantic_map_size,
        sanitizer_reports_end_execution: false,
    })
}

// Observers, feedback and objective over the target's coverage and telemetry maps.
macro_rules! semantist_feedbacks {
    ($setup:expr, $edges:expr, $semantic:expr, $telemetry:expr, $covered:expr) => {{
        let setup: &CampaignSetup = $setup;
        let config = &setup.config;
        // 3. observers 负责观察目标执行：覆盖率 map 和执行时间。
        let edges_observer = unsafe {
            HitcountsMapObserver::new(StdMapObserver::new("shared_mem", $edges)).track_indices()
        };
        let semantic_observer = unsafe { StdMapObserver::new("semantic_shared_mem", $semantic) };
        // The observer clears the telemetry header before every target execution,
        // including executions later classified as solutions.
        let telemetry_observer = unsafe { TelemetryObserver::new("semantic_telemetry", $telemetry) };
        let time_observer = TimeObserver::new("time");

        // 4. feedback 决定哪些输入进入 corpus；objective 决定哪些输入是 crash/solution。
        let runtime_findings = RuntimeFindingFeedback::new(
            config.findings_dir.clone(),
            config.crashes_dir.clone(),
            config.metadata_bootstrap.clone(),
            config.events_file.clone(),
            setup.sanitizer_log_dir.clone(),
        )
        .with_start(setup.started)
        .with_logs_only_after_failures(setup.sanitizer_reports_end_execution);
        let semantic_objective = SemanticViolationObjective::new(&semantic_observer, &setup.runtime_ids)
            .with_pending_findings(runtime_findings.pending_semantic_findings());
        // A restarted in-process client resumes the violations already reported.
        let covered: Option<&std::collections::BTreeSet<String>> = $covered;
        let semantic_objective = match covered {
            Some(covered) => semantic_objective.with_covered_targets(covered),
            None => semantic_objective,
        };
        let semantic_evidence = SemanticEvidenceCollector(
            SemanticIrCoverageFeedback::new(&semantic_observer, setup.runtime_ids.clone())
                .with_telemetry(&telemetry_observer));
        let feedback = feedback_or!(
            SemanticNoveltyFeedback,
            MaxMapFeedback::new(&edges_observer),
            TimeFeedback::new(&time_observer)
        );

        // Eager OR: the runtime feedback runs after the semantic objective and
        // persists the violations it reported for this input.
        let objective = feedback_or!(semantic_evidence, semantic_objective, runtime_findings);
        (time_observer, edges_observer, semantic_observer, telemetry_observer, feedback, objective)
    }};
}

// 5. state 保存 RNG、corpus、crashes，以及 feedback 的元数据。
macro_rules! semantist_new_state {
    ($setup:expr, $feedback:expr, $objective:expr) => {{
        let setup: &CampaignSetup = $setup;
        let config = &setup.config;
        let mut state = StdState::new(
            StdRand::with_seed(config.rng_seed.unwrap_or_else(current_nanos)),
            InMemoryOnDiskCorpus::<BytesInput>::new(&config.queue_dir)?,
            OnDiskCorpus::new(&config.crashes_dir)?,
            $feedback,
            $objective,
        )?;
        state.add_metadata(SeedSourceContext {
            source: SeedSource::Initial,
        });
        let mut semantic_runtime =
            SemanticRuntimeMetadata::new(setup.semantic_plan.clone(), &setup.mapped_semantic_targets);
        semantic_runtime.set_target_names(setup.target_names.clone());
        state.add_metadata(semantic_runtime);
        state
    }};
}

// The V3 in-process engine owns execution, mutation and feedback. The legacy
// forkserver campaign and its duplicate restart wrapper were removed so there
// is one production path for the shared corpus.
