#[path = "../../semantic/runtime_module.rs"]
mod semantic;

use libafl::{
    corpus::{Corpus, CorpusId, InMemoryOnDiskCorpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::{forkserver::ForkserverExecutor, ExitKind, HasObservers, StdChildArgs},
    feedback_or,
    feedbacks::{Feedback, MapFeedbackMetadata, MaxMapFeedback, StateInitializer, TimeFeedback},
    fuzzer::{Evaluator, Fuzzer, InputFilter, StdFuzzer},
    inputs::BytesInput,
    monitors::SimpleMonitor,
    mutators::{MutationResult, Mutator, Tokens},
    observers::{CanTrack, HitcountsMapObserver, MapObserver, StdMapObserver, TimeObserver},
    schedulers::{QueueScheduler, RemovableScheduler, Scheduler},
    stages::{Restartable, Stage, StdMutationalStage},
    state::{HasCorpus, HasExecutions, HasRand, StdState},
    Error, HasMetadata, HasNamedMetadata, SerdeAny,
};
use libafl_bolts::{
    current_nanos,
    rands::{Rand, StdRand},
    shmem::{ShMem, ShMemProvider, UnixShMemProvider},
    tuples::{tuple_list, Handle, Handled, MatchName, MatchNameRef},
    AsIter, AsSliceMut, Named, StdTargetArgs, Truncate,
};
use semantic::{ActiveSelection, ControllableSeedField, SeedProfile, SemanticRuntimeMetadata};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    borrow::Cow,
    collections::{BTreeMap, HashMap, HashSet},
    env, fs,
    fs::OpenOptions,
    io::Write,
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
fn main() -> Result<(), Error> {
    // 1. 解析配置并确认目标程序存在。
    let config = Config::from_args()?;
    if let Some(path) = config.semantic_runtime_ids.as_deref() {
        env::set_var("SEMANTIST_SEMANTIC_RUNTIME_IDS", path);
    }
    if !Path::new(&config.target).exists() {
        return Err(Error::unknown(format!(
            "{} does not exist; run ./compiler/scripts/compile_st.sh and ./compiler/scripts/build_target.sh first",
            config.target.display()
        )));
    }
    ensure_dirs(&config)?;
    let sanitizer_log_dir = config.findings_dir.join("sanitizer-logs");
    fs::create_dir_all(&sanitizer_log_dir)?;
    set_default_sanitizer_env(&sanitizer_log_dir);

    let runtime_ids = load_runtime_id_table(config.semantic_runtime_ids.as_deref())?;
    let mapped_semantic_targets =
        load_mapped_semantic_targets(config.semantic_runtime_ids.as_deref(), &runtime_ids)?;
    let semantic_plan_path = config.semantic_task_plan.as_deref().ok_or_else(|| {
        Error::illegal_argument(
            "compiler-semantic fuzzing requires --semantic-task-plan or SEMANTIST_SEMANTIC_TASK_PLAN",
        )
    })?;
    let semantic_plan = semantic::load_plan(semantic_plan_path)
        .map_err(|message| Error::illegal_argument(message))?;
    let semantic_generation_plan = semantic_plan.clone();
    let semantic_map_size = runtime_ids
        .entries
        .iter()
        .map(|entry| entry.runtime_id as usize + 1)
        .max()
        .unwrap_or(1);

    // 2. 创建相互独立的 AFL edge map 和 STG semantic map。
    let mut shmem_provider = UnixShMemProvider::new()?;
    let mut shmem = shmem_provider.new_shmem(MAP_SIZE)?;
    let mut semantic_shmem = shmem_provider.new_shmem(semantic_map_size)?;
    unsafe {
        shmem.write_to_env("__AFL_SHM_ID")?;
        semantic_shmem.write_to_env("SEMANTIST_SEMANTIC_SHM_ID")?;
    }
    env::set_var("AFL_MAP_SIZE", MAP_SIZE.to_string());
    env::set_var("SEMANTIST_SEMANTIC_MAP_SIZE", semantic_map_size.to_string());
    // 3. observers 负责观察目标执行：覆盖率 map 和执行时间。
    let shmem_buf = shmem.as_slice_mut();
    let edges_observer = unsafe {
        HitcountsMapObserver::new(StdMapObserver::new("shared_mem", shmem_buf)).track_indices()
    };
    let observer_handle = edges_observer.handle();
    let semantic_observer =
        unsafe { StdMapObserver::new("semantic_shared_mem", semantic_shmem.as_slice_mut()) };
    let time_observer = TimeObserver::new("time");

    // 4. feedback 决定哪些输入进入 corpus；objective 决定哪些输入是 crash/solution。
    let semantic_objective = SemanticViolationObjective::new(&semantic_observer, &runtime_ids);
    let mut feedback = feedback_or!(
        CoverageProgressFeedback::new(&edges_observer),
        SemanticIrCoverageFeedback::new(&semantic_observer, runtime_ids),
        MaxMapFeedback::new(&edges_observer),
        TimeFeedback::new(&time_observer)
    );

    let mut objective = feedback_or!(
        semantic_objective,
        RuntimeFindingFeedback::new(
            config.findings_dir.clone(),
            config.crashes_dir.clone(),
            config.metadata_bootstrap.clone(),
            config.events_file.clone(),
            sanitizer_log_dir.clone(),
        )
    );

    // 5. state 保存 RNG、corpus、crashes，以及 feedback 的元数据。
    let mut state = StdState::new(
        StdRand::with_seed(current_nanos()),
        InMemoryOnDiskCorpus::<BytesInput>::new(&config.queue_dir)?,
        OnDiskCorpus::new(&config.crashes_dir)?,
        &mut feedback,
        &mut objective,
    )?;
    state.add_metadata(SeedSourceContext {
        source: SeedSource::Initial,
    });
    state.add_metadata(SemanticRuntimeMetadata::new(
        semantic_plan,
        &mapped_semantic_targets,
    ));

    let monitor = SimpleMonitor::new(|s| println!("{s}"));
    let mut mgr = SimpleEventManager::new(monitor);

    // 6. scheduler 决定下一轮从 corpus 里挑哪个 testcase。
    let scheduler =
        TargetAwareScheduler::new(config.metadata_bootstrap.clone(), config.seed_dir.clone());
    let mut fuzzer = StdFuzzer::builder()
        .scheduler(scheduler)
        .feedback(feedback)
        .objective(objective)
        .input_filter(ContentHashInputFilter::default())
        .build();

    // 7. ForkserverExecutor 启动 AFL instrumentation 目标，执行每个结构化输入文件。
    let mut tokens = Tokens::new();
    let mut executor = ForkserverExecutor::builder()
        .program(config.target.to_string_lossy().to_string())
        .debug_child(config.debug_child)
        .shmem_provider(&mut shmem_provider)
        .autotokens(&mut tokens)
        .coverage_map_size(MAP_SIZE)
        .timeout(config.timeout)
        .build(tuple_list!(
            time_observer,
            edges_observer,
            semantic_observer
        ))?;

    // AFL 目标可能报告真实 map size；observer 跟着截断，减少无效 map 区域。
    if let Some(dynamic_map_size) = executor.coverage_map_size() {
        executor.observers_mut()[&observer_handle]
            .as_mut()
            .truncate(dynamic_map_size);
    }

    // 8. 加载初始种子和已有 queue；如果没有种子，用一条合法结构化输入兜底。
    state.load_initial_inputs(
        &mut fuzzer,
        &mut executor,
        &mut mgr,
        &initial_corpus_dirs(&config),
    )?;
    if let Some(model_path) = config.semantic_model.as_deref() {
        let mut generator = SemanticInitialGenerator::from_stg(
            model_path,
            &config.function,
            &config.seed_dir,
            &semantic_generation_plan,
        )?;
        let proposed = generator.len();
        if proposed > 0 {
            let before = state.corpus().count();
            state.add_metadata(SeedSourceContext {
                source: SeedSource::SemanticGeneration,
            });
            state.generate_initial_inputs(
                &mut fuzzer,
                &mut executor,
                &mut generator,
                &mut mgr,
                proposed,
            )?;
            state.add_metadata(SeedSourceContext {
                source: SeedSource::Fuzzing,
            });
            let admitted = state.corpus().count().saturating_sub(before);
            append_json_value(
                &config.events_file,
                &serde_json::json!({
                    "event": "semantic_generation",
                    "provider": "libafl_generator",
                    "model": model_path,
                    "stateful": generator.stateful,
                    "proposed": proposed,
                    "admitted": admitted,
                    "executions": *state.executions(),
                    "unix_time_seconds": unix_time_seconds(),
                }),
            )?;
            println!("Semantic initial generation: proposed {proposed}, admitted {admitted}");
        }
    }
    if state.corpus().is_empty() {
        println!(
            "No non-objective seeds found in {}; replaying structured bootstrap variants.",
            config.seed_dir.display()
        );
        for candidate in bootstrap_seed_variants(&config.seed_dir) {
            fuzzer.add_input(&mut state, &mut executor, &mut mgr, candidate)?;
            if !state.corpus().is_empty() {
                break;
            }
        }
    }
    if state.corpus().is_empty() {
        if runtime_findings_exist(&config.findings_dir) {
            append_json_value(
                &config.events_file,
                &serde_json::json!({
                    "event": "startup_completed_with_findings",
                    "reason": "all startup inputs were classified as objectives",
                    "executions": *state.executions(),
                    "unix_time_seconds": unix_time_seconds(),
                }),
            )?;
            println!(
                "Startup inputs produced runtime findings before a non-objective corpus seed was admitted; stopping after confirmed objectives."
            );
            return Ok(());
        }
        return Err(Error::illegal_state(
            "all structured bootstrap inputs were objectives; no seed can initialize the corpus",
        ));
    }
    state.add_metadata(SeedSourceContext {
        source: SeedSource::Fuzzing,
    });
    state.add_metadata(tokens);

    // 9. 运行语义预算 stage 和结构化 ST 变异 stage。
    println!(
        "Starting ST forkserver fuzzer for {}",
        config.target.display()
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
    let mutator = StructuredStMutator::new();
    let budget_stage = SemanticTaskBudgetStage::new(
        config.semantic_task_budget,
        config.semantic_task_state_file.clone(),
    );
    let mut stages = tuple_list!(budget_stage, StdMutationalStage::new(mutator));
    if let Some(iterations) = config.fuzz_iterations {
        fuzzer.fuzz_loop_for(&mut stages, &mut executor, &mut state, &mut mgr, iterations)?;
    } else {
        fuzzer.fuzz_loop(&mut stages, &mut executor, &mut state, &mut mgr)?;
    }
    Ok(())
}

fn set_default_sanitizer_env(log_dir: &Path) {
    let asan_log = log_dir.join("asan");
    let ubsan_log = log_dir.join("ubsan");
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
            "abort_on_error=1:symbolize=0:print_stacktrace=0:log_path={}",
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

fn runtime_findings_exist(findings_dir: &Path) -> bool {
    let Ok(entries) = fs::read_dir(findings_dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name == "sanitizer-logs")
        {
            continue;
        }
        if path.is_file() {
            return true;
        }
        if path.is_dir() && runtime_findings_exist(&path) {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn testcase_metadata_is_serializable_and_native() {
        let metadata = StSeedMetadata::new(SeedSource::Initial, None, b"X,INT,0\n");
        let serialized = serde_json::to_string(&metadata).unwrap();
        let restored: StSeedMetadata = serde_json::from_str(&serialized).unwrap();
        assert_eq!(restored, metadata);

        let mut testcase = libafl::corpus::Testcase::new(BytesInput::new(b"X,INT,0\n".to_vec()));
        testcase.add_metadata(metadata.clone());
        assert_eq!(testcase.metadata::<StSeedMetadata>().unwrap(), &metadata);
    }

    #[test]
    fn content_hash_deduplicates_equal_seeds() {
        let mut seen = HashSet::new();
        assert!(seen.insert(content_hash(b"X,INT,0\n")));
        assert!(!seen.insert(content_hash(b"X,INT,0\n")));
        assert!(seen.insert(content_hash(b"X,INT,1\n")));
    }

    #[test]
    fn sanitizer_log_filter_ignores_lsan_ptrace_noise() {
        let noise = b"==1==LeakSanitizer has encountered a fatal error.\n\
            ==1==HINT: LeakSanitizer does not work under ptrace (strace, gdb, etc)\n";
        assert_eq!(sanitizer_log_kind("ubsan.1", noise), None);
    }

    #[test]
    fn sanitizer_log_kind_uses_contents_before_file_prefix() {
        let report = b"==2==ERROR: AddressSanitizer: ABRT on unknown address\nSUMMARY: AddressSanitizer: ABRT\n";
        assert_eq!(
            sanitizer_log_kind("ubsan.2", report),
            Some("asan".to_string())
        );
    }

    #[test]
    fn bootstrap_variants_preserve_seed_fields_and_avoid_zero_only_startup() {
        let root = std::env::temp_dir().join(format!(
            "semantist-bootstrap-{}-{}",
            std::process::id(),
            current_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("seed"), b"x,DINT,0\nflag,BOOL,0\n").unwrap();
        let variants = bootstrap_seed_variants(&root);
        fs::remove_dir_all(&root).unwrap();
        assert!(variants
            .iter()
            .any(|input| { String::from_utf8_lossy(input.as_ref()).contains("x,DINT,1") }));
        assert!(variants
            .iter()
            .all(|input| { String::from_utf8_lossy(input.as_ref()).contains("flag,BOOL,") }));
    }

    #[test]
    fn cycle_seed_parser_splits_shared_and_cycle_records() {
        let seed = parse_cycle_aware_seed(
            "C0,UINT,2\n0.RST,BOOL,1\n0.WD,BOOL,0\n2.RST,BOOL,0\n2.WD,BOOL,1\n",
        );
        assert_eq!(seed.shared.len(), 1);
        assert_eq!(seed.shared[0].name, "C0");
        assert_eq!(seed.cycles.len(), 2);
        assert_eq!(seed.cycles.get(&0).unwrap()[0].name, "RST");
        assert_eq!(seed.cycles.get(&2).unwrap()[1].name, "WD");
        assert!(has_cycle_record("0.X,INT,1\n"));
        assert!(!has_cycle_record("X,INT,1\n"));
    }

    #[test]
    fn cycle_seed_normalize_rewrites_sparse_cycle_ids() {
        let mut seed = parse_cycle_aware_seed("A,INT,1\n2.X,INT,2\n7.X,INT,3\n");
        normalize_cycle_ids(&mut seed);
        assert_eq!(
            serialize_cycle_aware_seed(&seed),
            "A,INT,1\n0.X,INT,2\n1.X,INT,3\n"
        );
    }

    #[test]
    fn cycle_patterns_toggle_enable_and_read_write_fields() {
        let mut seed = parse_cycle_aware_seed(
            "0.RST,BOOL,0\n0.E,BOOL,0\n0.RD,BOOL,0\n0.WD,BOOL,0\n\
             1.RST,BOOL,0\n1.E,BOOL,0\n1.RD,BOOL,0\n1.WD,BOOL,0\n\
             2.RST,BOOL,0\n2.E,BOOL,0\n2.RD,BOOL,0\n2.WD,BOOL,0\n\
             3.RST,BOOL,0\n3.E,BOOL,0\n3.RD,BOOL,0\n3.WD,BOOL,0\n",
        );
        cycle_enable_toggle(&mut seed);
        assert_eq!(seed.cycles.get(&0).unwrap()[1].value, "1");
        assert_eq!(seed.cycles.get(&1).unwrap()[1].value, "0");

        cycle_read_write_pattern(&mut seed);
        assert_eq!(seed.cycles.get(&0).unwrap()[0].value, "1");
        assert_eq!(seed.cycles.get(&1).unwrap()[3].value, "1");
        assert_eq!(seed.cycles.get(&3).unwrap()[2].value, "1");
    }

    #[test]
    fn semantic_dag_mutation_uses_typed_expression_operands() {
        let mut records = parse_structured_records("X,INT,0\nLIMIT,INT,7\n");
        let context = TargetContext {
            target_id: Some("stg:branch:true".to_string()),
            goal_expression: Some(serde_json::json!({
                "kind": {"kind": "binary", "operator": ">="},
                "operands": [
                    {
                        "kind": {"kind": "variable", "qualified_name": "P.X"},
                        "reads": ["P.X"],
                        "operands": []
                    },
                    {
                        "kind": {"kind": "variable", "qualified_name": "P.LIMIT"},
                        "reads": ["P.LIMIT"],
                        "operands": []
                    }
                ]
            })),
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(11);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        assert_eq!(records[0].value, "7");
    }

    #[test]
    fn stable_target_id_survives_plan_schedule_mutation_and_feedback() {
        let plan: semantic::SemanticTaskPlan = serde_json::from_value(serde_json::json!({
            "schema_version": semantic::PLAN_SCHEMA,
            "target_identity": "stg_stable_target_id",
            "tasks": [{
                "task_id": "semantic-task:stable",
                "task_kind": "violation",
                "pou": "P",
                "terminal_target_id": "target:violation",
                "candidate_paths": [{
                    "path_id": "path:stable",
                    "target_ids": ["target:branch", "target:violation"]
                }],
                "ordered_waypoints": ["target:branch", "target:violation"],
                "control_obligations": [{
                    "target_id": "target:branch",
                    "kind": "branch_outcome",
                    "role": "true",
                    "goal_expression": {
                        "kind": {"kind": "binary", "operator": ">="},
                        "operands": [
                            {
                                "kind": {"kind": "variable", "qualified_name": "P.X"},
                                "reads": ["P.X"],
                                "operands": []
                            },
                            {
                                "kind": {
                                    "kind": "literal",
                                    "value": {"kind": "integer", "value": "1"}
                                },
                                "reads": [],
                                "operands": []
                            }
                        ]
                    }
                }],
                "data_dependencies": [{"symbol": "P.X"}],
                "controllable_seed_fields": [{
                    "symbol": "P.X",
                    "seed_field": "X",
                    "role": "input",
                    "type_name": "INT",
                    "type": {
                        "kind": "integer",
                        "semantic_bit_width": 16,
                        "signed": true
                    }
                }],
                "modeling_status": "exact",
                "terminal_kind": "property_violation"
            }]
        }))
        .unwrap();
        let mut runtime = SemanticRuntimeMetadata::new(
            plan,
            &HashSet::from(["target:branch".to_string(), "target:violation".to_string()]),
        );
        let selection = runtime
            .select(&[SeedProfile {
                content_hash: "seed".to_string(),
                ..SeedProfile::default()
            }])
            .unwrap();
        let context = semantic_target_context(&selection);
        assert_eq!(context.target_id.as_deref(), Some("target:branch"));

        let mut records = parse_structured_records("X,INT,0\n");
        let mut rand = StdRand::with_seed(13);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        assert_eq!(records[0].value, "1");

        runtime.record_coverage(["target:branch".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("target:violation"));
        assert!(runtime.covered_target_ids.contains("target:branch"));

        let state_path = std::env::temp_dir().join(format!(
            "semantist-semantic-state-{}-{}.json",
            std::process::id(),
            current_nanos()
        ));
        persist_semantic_task_state(&state_path, &runtime).unwrap();
        let saved: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&state_path).unwrap()).unwrap();
        fs::remove_file(state_path).unwrap();
        assert_eq!(saved["guide_target_id"].as_str(), Some("target:violation"));
        assert_eq!(saved["ranking"][0].as_str(), Some("semantic-task:stable"));
    }

    #[test]
    fn semantic_modulo_violation_mutates_the_real_denominator() {
        let mut records = parse_structured_records("VALUE,INT,9\nDIVISOR,INT,3\n");
        let context = TargetContext {
            target_id: Some("stg:mod:violation".to_string()),
            target_type: Some("hazard_violation".to_string()),
            task_kind: Some("violation".to_string()),
            hazard_kind: Some("modulo".to_string()),
            goal_expression: Some(serde_json::json!({
                "kind": {"kind": "binary", "operator": "MOD"},
                "operands": [
                    {
                        "kind": {"kind": "variable", "qualified_name": "P.VALUE"},
                        "reads": ["P.VALUE"],
                        "operands": []
                    },
                    {
                        "kind": {"kind": "variable", "qualified_name": "P.DIVISOR"},
                        "reads": ["P.DIVISOR"],
                        "operands": []
                    }
                ]
            })),
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(12);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        assert_eq!(records[1].value, "0");

        records[1].value = "0".to_string();
        let reach_context = TargetContext {
            target_id: Some("stg:mod:reach".to_string()),
            target_type: Some("hazard_reach".to_string()),
            ..context
        };
        assert!(semantic_mutate_records(
            &mut rand,
            &mut records,
            &reach_context
        ));
        assert_eq!(records[1].value, "1");
    }

    #[test]
    fn semantic_modulo_violation_backpropagates_through_local_definition() {
        let mut records = parse_structured_records("X,INT,0\nD,INT,0\nL,INT,7\nU,INT,7\n");
        let context = TargetContext {
            target_id: Some("target:modulo-zero".to_string()),
            target_type: Some("hazard_violation".to_string()),
            hazard_kind: Some("modulo".to_string()),
            goal_expression: Some(serde_json::json!({
                "kind": {"kind": "binary", "operator": "MOD"},
                "operands": [
                    {
                        "kind": {"kind": "variable", "qualified_name": "INC2.X"},
                        "reads": ["INC2.X"],
                        "operands": []
                    },
                    {
                        "kind": {"kind": "variable", "qualified_name": "INC2.tmp"},
                        "reads": ["INC2.tmp"],
                        "operands": []
                    }
                ]
            })),
            value_definitions: BTreeMap::from([(
                "TMP".to_string(),
                serde_json::json!({
                    "kind": {"kind": "binary", "operator": "+"},
                    "operands": [
                        {
                            "kind": {"kind": "binary", "operator": "-"},
                            "operands": [
                                {
                                    "kind": {"kind": "variable", "qualified_name": "INC2.U"},
                                    "reads": ["INC2.U"],
                                    "operands": []
                                },
                                {
                                    "kind": {"kind": "variable", "qualified_name": "INC2.L"},
                                    "reads": ["INC2.L"],
                                    "operands": []
                                }
                            ]
                        },
                        {
                            "kind": {"kind": "literal", "value": {"kind": "integer", "value": "1"}},
                            "reads": [],
                            "operands": []
                        }
                    ]
                }),
            )]),
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(17);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        let l = records.iter().find(|record| record.name == "L").unwrap();
        let u = records.iter().find(|record| record.name == "U").unwrap();
        assert_eq!(
            parse_integer_value(&u.value),
            parse_integer_value(&l.value).map(|v| v - 1)
        );
    }

    #[test]
    fn semantic_mutation_satisfies_float_range_definition() {
        let mut records = parse_structured_records("IN,REAL,0.0\nMIN,REAL,10.0\nMAX,REAL,0.0\n");
        let greater_than_min = serde_json::json!({
            "kind": {"kind": "binary", "operator": ">"},
            "operands": [
                {"kind": {"kind": "variable", "qualified_name": "P.IN"}, "reads": ["P.IN"], "operands": []},
                {"kind": {"kind": "variable", "qualified_name": "P.MIN"}, "reads": ["P.MIN"], "operands": []}
            ]
        });
        let less_than_max = serde_json::json!({
            "kind": {"kind": "binary", "operator": "<"},
            "operands": [
                {"kind": {"kind": "variable", "qualified_name": "P.IN"}, "reads": ["P.IN"], "operands": []},
                {"kind": {"kind": "variable", "qualified_name": "P.MAX"}, "reads": ["P.MAX"], "operands": []}
            ]
        });
        let context = TargetContext {
            target_id: Some("target:float-range".to_string()),
            goal_expression: Some(serde_json::json!({
                "kind": {"kind": "variable", "qualified_name": "P.F1"},
                "reads": ["P.F1"],
                "operands": []
            })),
            value_definitions: BTreeMap::from([(
                "F1".to_string(),
                serde_json::json!({
                    "kind": {"kind": "binary", "operator": "AND"},
                    "operands": [greater_than_min, less_than_max]
                }),
            )]),
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(19);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        let value = |name: &str| {
            records
                .iter()
                .find(|record| record.name == name)
                .and_then(|record| parse_number_value(&record.value))
                .unwrap()
        };
        assert!(value("MIN") < value("IN"));
        assert!(value("IN") < value("MAX"));
    }

    #[test]
    fn semantic_arithmetic_boundary_stresses_float_inputs_as_a_group() {
        let mut records = parse_structured_records(
            "in_1,REAL,0.0\nin_2,REAL,0.0\nin_3,REAL,0.0\ndefault,REAL,0.0\nin_min,REAL,0.0\nin_max,REAL,1.0\nmode,BYTE,0\n",
        );
        let float_field = |name: &str| ControllableSeedField {
            symbol: format!("MULTI_IN.{name}"),
            seed_field: Some(name.to_string()),
            role: Some("input".to_string()),
            type_name: Some("REAL".to_string()),
            type_fact: Some(serde_json::json!({
                "kind": "float",
                "name": "REAL",
                "semantic_bit_width": 32,
                "bit_width": 32
            })),
            ..Default::default()
        };
        let context = TargetContext {
            target_id: Some("target:real-add-overflow".to_string()),
            target_type: Some("hazard_violation".to_string()),
            hazard_kind: Some("arithmetic_boundary".to_string()),
            goal_expression: Some(serde_json::json!({
                "kind": {"kind": "binary", "operator": "+"},
                "operands": []
            })),
            controllable_seed_fields: vec![
                float_field("in_1"),
                float_field("in_2"),
                float_field("in_3"),
                float_field("default"),
                float_field("in_min"),
                float_field("in_max"),
                ControllableSeedField {
                    symbol: "MULTI_IN.mode".to_string(),
                    seed_field: Some("mode".to_string()),
                    role: Some("input".to_string()),
                    type_name: Some("BYTE".to_string()),
                    type_fact: Some(serde_json::json!({
                        "kind": "integer",
                        "semantic_bit_width": 8,
                        "signed": false
                    })),
                    ..Default::default()
                },
            ],
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(29);
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        let value = |name: &str| {
            records
                .iter()
                .find(|record| record.name == name)
                .and_then(|record| parse_number_value(&record.value))
                .unwrap()
        };
        assert!(value("in_min") < value("in_1"));
        assert!(value("in_1") < value("in_max"));
        assert!(value("in_1").abs() >= 1.0e38);
        assert_eq!(value("in_1"), value("in_2"));
        assert_eq!(value("in_2"), value("in_3"));
        assert_eq!(value("default"), 0.0);
    }

    #[test]
    fn semantic_seed_ranking_is_weighted_but_not_exclusive() {
        let mut rand = StdRand::with_seed(23);
        let mut counts = [0_usize; 4];
        for _ in 0..2_000 {
            counts[rank_weighted_index(&mut rand, 4)] += 1;
        }
        assert!(counts.iter().all(|count| *count > 0));
        assert!(counts[0] > counts[1]);
        assert!(counts[1] > counts[2]);
        assert!(counts[2] > counts[3]);
    }

    #[test]
    fn task_budget_accumulates_across_probabilistic_task_switches() {
        let path = std::env::temp_dir().join("semantist-budget-test.jsonl");
        let mut stage = SemanticTaskBudgetStage::new(3, path);
        assert_eq!(
            stage.account_execution_progress(0, Some("guide-a".to_string())),
            0
        );
        assert_eq!(
            stage.account_execution_progress(1, Some("guide-b".to_string())),
            0
        );
        assert_eq!(
            stage.account_execution_progress(2, Some("guide-a".to_string())),
            1
        );
        assert_eq!(
            stage.account_execution_progress(4, Some("guide-b".to_string())),
            1
        );
        assert_eq!(
            stage.account_execution_progress(6, Some("guide-a".to_string())),
            3
        );
    }

    #[test]
    fn objective_accepts_only_exact_violation_targets() {
        let entry = |kind: &str, modeling: &str| RuntimeTargetEntry {
            runtime_id: 1,
            stable_id: "target".to_string(),
            pou: "P".to_string(),
            node_id: "node".to_string(),
            semantic_edge_ids: vec![],
            kind: kind.to_string(),
            role: None,
            hazard: None,
            source: None,
            modeling: modeling.to_string(),
        };
        assert!(is_exact_semantic_violation(&entry(
            "hazard_violation",
            "exact"
        )));
        assert!(is_exact_semantic_violation(&entry(
            "property_violation",
            "exact"
        )));
        assert!(!is_exact_semantic_violation(&entry(
            "hazard_reach",
            "exact"
        )));
        assert!(!is_exact_semantic_violation(&entry(
            "hazard_violation",
            "conservative"
        )));
    }

    #[test]
    fn semantic_cycle_mutation_expands_and_changes_state_cycles() {
        let mut seed = parse_cycle_aware_seed("0.STATE,INT,0\n");
        let context = TargetContext {
            target_id: Some("target_state_transition".to_string()),
            target_type: Some("state".to_string()),
            semantic_task_id: Some("semantic-task:state".to_string()),
            cycle_hint: Some(4),
            controllable_seed_fields: vec![ControllableSeedField {
                symbol: "P.STATE".to_string(),
                seed_field: Some("STATE".to_string()),
                role: Some("state".to_string()),
                type_name: Some("INT".to_string()),
                configuration_source: None,
                type_fact: None,
            }],
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(2);
        assert!(semantic_mutate_cycle_seed(&mut rand, &mut seed, &context));
        assert!((4..=64).contains(&seed.cycles.len()));
        for records in seed.cycles.values() {
            assert_eq!(records[0].name, "STATE");
        }
    }

    #[test]
    fn runtime_filter_rejects_duplicate_seed_contents() {
        let mut filter = ContentHashInputFilter::default();
        let mut state = ();
        let mut manager = ();
        let input = BytesInput::new(b"X,INT,0\n".to_vec());
        assert!(filter
            .should_execute(&input, &mut state, &mut manager)
            .unwrap());
        assert!(!filter
            .should_execute(&input, &mut state, &mut manager)
            .unwrap());
    }

    #[test]
    fn runtime_finding_cycle_metadata_is_inferred_from_seed() {
        let metadata = infer_seed_cycle_metadata(b"LIST,STRING,;a;b\n0.RST,BOOL,1\n2.RST,BOOL,0\n");
        assert_eq!(metadata.target_kind, "FUNCTION_BLOCK");
        assert_eq!(metadata.cycle_count, 2);
        assert_eq!(metadata.cycle_ids, vec![0, 2]);

        let function_metadata = infer_seed_cycle_metadata(b"X,INT,0\n");
        assert_eq!(function_metadata.target_kind, "FUNCTION");
        assert_eq!(function_metadata.cycle_count, 0);
        assert!(function_metadata.cycle_ids.is_empty());
    }

    #[test]
    fn initial_inputs_use_unified_seeds_and_runtime_queue() {
        let config = Config {
            target: PathBuf::from("target"),
            function: "test".to_string(),
            timeout: Duration::from_secs(1),
            corpus_dir: PathBuf::from("corpus"),
            seed_dir: PathBuf::from("unified-corpus/seeds"),
            queue_dir: PathBuf::from("unified-corpus/runtime"),
            crashes_dir: PathBuf::from("target-corpus"),
            findings_dir: PathBuf::from("findings"),
            metadata_bootstrap: None,
            semantic_task_plan: None,
            semantic_model: None,
            semantic_runtime_ids: None,
            events_file: PathBuf::from("events.jsonl"),
            semantic_task_budget: 1_000,
            semantic_task_state_file: PathBuf::from("semantic-task-state.json"),
            fuzz_iterations: None,
            debug_child: false,
        };

        assert_eq!(
            initial_corpus_dirs(&config),
            vec![
                PathBuf::from("unified-corpus/seeds"),
                PathBuf::from("unified-corpus/runtime"),
            ]
        );
    }
}
