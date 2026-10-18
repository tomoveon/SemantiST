#[path = "../../semantic/runtime_module.rs"]
mod semantic;

use libafl::{
    corpus::{Corpus, CorpusId, InMemoryOnDiskCorpus, OnDiskCorpus, Testcase},
    events::{Event, EventFirer, EventWithStats},
    executors::{ExitKind, HasObservers},
    feedback_or,
    feedbacks::{Feedback, MaxMapFeedback, StateInitializer, TimeFeedback},
    fuzzer::{Evaluator, ExecutesInput, Fuzzer, HasScheduler, StdFuzzer},
    inputs::BytesInput,
    monitors::{
        stats::{AggregatorOps, UserStats, UserStatsValue},
        SimpleMonitor,
    },
    mutators::{MutationResult, Mutator, Tokens},
    observers::{CanTrack, HitcountsMapObserver, MapObserver, StdMapObserver, TimeObserver},
    schedulers::{QueueScheduler, RemovableScheduler, Scheduler},
    stages::{Restartable, Stage, StdMutationalStage},
    state::{HasCorpus, HasExecutions, HasRand, StdState},
    Error, HasMetadata, SerdeAny,
};
use libafl_bolts::{
    current_nanos,
    rands::{Rand, StdRand},
    shmem::{ShMemProvider, UnixShMemProvider},
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

include!("../../semantic/metadata.rs");
include!("../../semantic/observers.rs");
include!("../../semantic/io.rs");
include!("../../scheduler/engine.rs");
include!("../../corpus/generation.rs");
include!("mutations.rs");
include!("config.rs");
include!("cli.rs");
include!("../../stages/semantic_budget.rs");
include!("profile.rs");
include!("consistency.rs");

// Everything a campaign derives from its configuration before the target runs;
// shared by the forkserver binary (main.rs) and the in-process library.
struct CampaignSetup {
    started: std::time::Instant,
    config: Config,
    sanitizer_log_dir: PathBuf,
    runtime_ids: RuntimeIdTable,
    target_names: Vec<String>,
    mapped_semantic_targets: HashSet<String>,
    semantic_plan: semantic::SemanticTaskPlan,
    semantic_map_size: usize,
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
    })
}

// Observers, feedback and objective over the three maps a target writes:
// AFL edges, the STG semantic bitmap and the semantic telemetry. Both
// execution backends build exactly these; only where the maps live differs.
macro_rules! semantist_feedbacks {
    ($setup:expr, $edges:expr, $semantic:expr, $telemetry:expr) => {{
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
        .with_start(setup.started);
        let semantic_objective = SemanticViolationObjective::new(&semantic_observer, &setup.runtime_ids)
            .with_pending_findings(runtime_findings.pending_semantic_findings());
        let semantic_evidence = SemanticEvidenceCollector(
            SemanticIrCoverageFeedback::new(&semantic_observer, setup.runtime_ids.clone())
                .with_telemetry(&telemetry_observer));
        let feedback = feedback_or!(
            ProfiledFeedback::new(SemanticNoveltyFeedback, Phase::FeedbackSemanticNovelty),
            ProfiledFeedback::new(MaxMapFeedback::new(&edges_observer), Phase::FeedbackEdges),
            ProfiledFeedback::new(TimeFeedback::new(&time_observer), Phase::FeedbackTime)
        );

        // Eager OR: the runtime feedback runs after the semantic objective and
        // persists the violations it reported for this input.
        let objective = feedback_or!(
            ProfiledFeedback::new(semantic_evidence, Phase::ObjectiveSemanticEvidence),
            ProfiledFeedback::new(semantic_objective, Phase::ObjectiveSemanticViolation),
            ProfiledFeedback::new(runtime_findings, Phase::ObjectiveRuntimeFindings)
        );
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

// Consistency dump, start-up corpus (skipped when a restarted in-process
// fuzzer resumes its state) and the fuzz loop, for either executor.
macro_rules! semantist_campaign {
    ($setup:expr, $fuzzer:ident, $executor:ident, $state:ident, $mgr:ident, $handles:expr, $tokens:expr, $fresh:expr, $backend:expr) => {{
        let setup: &CampaignSetup = $setup;
        let config = &setup.config;
        let fuzz_started = setup.started;
        let (observer_handle, semantic_handle, telemetry_handle) = $handles;
        if let Some(dump_path) = config.consistency_dump.as_deref() {
            let list = config.consistency_inputs.as_deref().ok_or_else(|| {
                Error::illegal_argument("--consistency-dump requires --consistency-inputs")
            })?;
            let mut records = String::new();
            for (index, path) in consistency_input_paths(list)?.into_iter().enumerate() {
                let input = BytesInput::new(fs::read(&path)?);
                let exit_kind = $fuzzer.execute_input(&mut $state, &mut $executor, &mut $mgr, &input)?;
                let observers = $executor.observers();
                let record = consistency_record(
                    index,
                    &path,
                    &exit_kind,
                    &observers[&observer_handle].as_ref().to_vec(),
                    &observers[&semantic_handle].to_vec(),
                    observers[&telemetry_handle].as_slice(),
                );
                records.push_str(&record.to_string());
                records.push('\n');
            }
            fs::write(dump_path, records)?;
            return Ok(());
        }

        if $fresh {
            // 8. 加载初始种子和已有 queue；如果没有种子，用一条合法结构化输入兜底。
            $state.load_initial_inputs(
                &mut $fuzzer,
                &mut $executor,
                &mut $mgr,
                &initial_corpus_dirs(&config),
            )?;
            if let Some(model_path) = config
                .ablation
                .semantic_initial_generation()
                .then_some(config.semantic_model.as_deref())
                .flatten()
            {
                let mut generator = SemanticInitialGenerator::from_stg(
                    model_path,
                    &config.function,
                    &config.seed_dir,
                    &setup.semantic_plan,
                )?;
                let proposed = generator.len();
                if proposed > 0 {
                    let before = $state.corpus().count();
                    $state.add_metadata(SeedSourceContext {
                        source: SeedSource::SemanticGeneration,
                    });
                    $state.generate_initial_inputs(
                        &mut $fuzzer,
                        &mut $executor,
                        &mut generator,
                        &mut $mgr,
                        proposed,
                    )?;
                    $state.add_metadata(SeedSourceContext {
                        source: SeedSource::Fuzzing,
                    });
                    let admitted = $state.corpus().count().saturating_sub(before);
                    append_json_value(
                        &config.events_file,
                        &serde_json::json!({
                            "event": "semantic_generation",
                            "provider": "libafl_generator",
                            "model": model_path,
                            "stateful": generator.stateful,
                            "proposed": proposed,
                            "admitted": admitted,
                            "executions": *$state.executions(),
                            "unix_time_seconds": unix_time_seconds(),
                        }),
                    )?;
                    println!("Semantic initial generation: proposed {proposed}, admitted {admitted}");
                }
            }
            if $state.corpus().is_empty() {
                println!(
                    "No non-objective seeds found in {}; replaying structured bootstrap variants.",
                    config.seed_dir.display()
                );
                for candidate in bootstrap_seed_variants(&config.seed_dir) {
                    $fuzzer.add_input(&mut $state, &mut $executor, &mut $mgr, candidate)?;
                    if !$state.corpus().is_empty() {
                        break;
                    }
                }
            }
            if $state.corpus().is_empty() {
                // Every startup input was an objective. Those findings are already
                // recorded near t=0; queue the base seed anyway so the campaign still
                // explores the rest of the target instead of ending here.
                queue_without_evaluation(&mut $fuzzer, &mut $state, startup_corpus_seed(&config.seed_dir))?;
                append_json_value(
                    &config.events_file,
                    &serde_json::json!({
                        "event": "startup_objectives_seeded_corpus",
                        "reason": "all startup inputs were classified as objectives",
                        "executions": *$state.executions(),
                        "elapsed_seconds": fuzz_started.elapsed().as_secs_f64(),
                        "unix_time_seconds": unix_time_seconds(),
                    }),
                )?;
                println!("All startup inputs were objectives; continuing from the base seed.");
            }
            $state.add_metadata(SeedSourceContext {
                source: SeedSource::Fuzzing,
            });
        }
        $state.add_metadata($tokens);

        // 9. 运行语义预算 stage 和结构化 ST 变异 stage。
        println!(
            "Starting ST fuzzer for {} ({} execution)",
            config.target.display(),
            $backend
        );
        println!("Function: {}", config.function);
        println!(
            "Corpus: {}, seeds: {}, queue: {}, target: {}, findings: {}, events: {}, timeout: {:?}",
            config.corpus_dir.display(),
            config.seed_dir.display(),
            config.queue_dir.display(),
            config.crashes_dir.display(),
            config.findings_dir.display(),
            config.events_file.display(),
            config.timeout
        );
        println!(
            "Strategy mix: semantic scheduling {:.0}% / queue {:.0}%, semantic mutation {:.0}% / structured havoc {:.0}%",
            SEMANTIC_SCHEDULING_PROBABILITY * 100.0,
            (1.0 - SEMANTIC_SCHEDULING_PROBABILITY) * 100.0,
            SEMANTIC_MUTATION_PROBABILITY * 100.0,
            (1.0 - SEMANTIC_MUTATION_PROBABILITY) * 100.0,
        );
        println!("Ablation profile: {}", config.ablation.as_str());
        let mutator = StructuredStMutator::new(config.ablation);
        let budget_stage = SemanticTaskBudgetStage::new(
            config.semantic_task_budget,
            config.semantic_task_state_file.clone(),
        );
        let mut stages = tuple_list!(
            ProfiledStage::new(budget_stage, Phase::StageSemanticBudget),
            ProfiledStage::new(StdMutationalStage::new(mutator), Phase::StageMutational)
        );
        phase_profile_begin(Some(config.events_file.with_file_name("phase-profile.txt")));
        if let Some(iterations) = config.fuzz_iterations {
            $fuzzer.fuzz_loop_for(&mut stages, &mut $executor, &mut $state, &mut $mgr, iterations)?;
            flush_scheduler_records(&mut $fuzzer, &$state)?;
            if let Some(runtime) = $state.metadata_map().get::<SemanticRuntimeMetadata>() {
                persist_semantic_task_state(&config.semantic_task_state_file, runtime)?;
            }
        } else {
            $fuzzer.fuzz_loop(&mut stages, &mut $executor, &mut $state, &mut $mgr)?;
        }
        phase_profile_flush(true);
        println!("SemantiST final executions: {}, elapsed_seconds: {:.6}",
            $state.executions(), fuzz_started.elapsed().as_secs_f64());
        Ok(())
    }};
}

fn set_default_sanitizer_env(log_dir: &Path, execution: ExecutionMode) {
    let asan_log = log_dir.join("asan");
    let ubsan_log = log_dir.join("ubsan");
    // One process runs many inputs outside fork mode. A recoverable UBSan
    // report would then be deduplicated per source location across inputs,
    // and later reports appended to an earlier input's log, so it ends the
    // process instead, as an ASan report does.
    let halt = if execution == ExecutionMode::Fork { "" } else { ":halt_on_error=1" };
    set_env_if_missing(
        "ASAN_OPTIONS",
        &format!(
            "abort_on_error=1:symbolize=0:detect_leaks=0:log_path={}",
            asan_log.display()
        ),
    );
    set_env_if_missing(
        "UBSAN_OPTIONS",
        &format!(
            "abort_on_error=1:symbolize=0:print_stacktrace=1{halt}:log_path={}",
            ubsan_log.display()
        ),
    );
    set_env_if_missing("LSAN_OPTIONS", "detect_leaks=0");
}

fn set_env_if_missing(key: &str, value: &str) {
    if env::var_os(key).is_none() {
        env::set_var(key, value);
    }
}

fn flush_scheduler_records<F, S>(fuzzer: &mut F, state: &S) -> Result<(), Error>
where
    F: HasScheduler<BytesInput, S, Scheduler = TargetAwareScheduler>,
    S: HasCorpus<BytesInput>,
{
    fuzzer.scheduler_mut().flush_bootstrap(state)
}

fn queue_without_evaluation<F, S>(fuzzer: &mut F, state: &mut S, input: BytesInput) -> Result<CorpusId, Error>
where
    F: HasScheduler<BytesInput, S>,
    S: HasCorpus<BytesInput>,
{
    let id = state.corpus_mut().add(Testcase::from(input))?;
    fuzzer.scheduler_mut().on_add(state, id)?;
    Ok(id)
}

fn startup_corpus_seed(seed_dir: &Path) -> BytesInput {
    let mut paths = fs::read_dir(seed_dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .collect::<Vec<_>>();
    paths.sort();
    paths
        .into_iter()
        .filter_map(|path| fs::read(path).ok())
        .find(|bytes| is_valid_structured_seed(bytes))
        .map(BytesInput::new)
        .or_else(|| bootstrap_seed_variants(seed_dir).into_iter().next())
        .unwrap_or_else(|| BytesInput::new(b"IN,INT,0\n".to_vec()))
}

