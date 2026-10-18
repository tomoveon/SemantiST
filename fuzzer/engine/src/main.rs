include!("campaign.rs");
use libafl::{
    events::SimpleEventManager,
    executors::{forkserver::ForkserverExecutor, StdChildArgs},
};
use libafl_bolts::{shmem::ShMem, StdTargetArgs, Truncate};
#[cfg(test)]
mod algorithm_regressions;

fn main() -> Result<(), Error> {
    let started = std::time::Instant::now();
    // 1. 解析配置并确认目标程序存在。
    let config = Config::from_args()?;
    if !Path::new(&config.target).exists() {
        return Err(Error::unknown(format!(
            "{} does not exist; run ./compiler/scripts/compile_st.sh and ./compiler/scripts/build_target.sh first",
            config.target.display()
        )));
    }
    let mut setup = prepare_campaign(config, started)?;
    set_default_sanitizer_env(&setup.sanitizer_log_dir, setup.config.execution);
    // ASan reports are fatal; UBSan ones only with halt_on_error.
    setup.sanitizer_reports_end_execution = setup.config.execution != ExecutionMode::Fork
        && sanitizer_env_flag("UBSAN_OPTIONS", "halt_on_error") == Some(true);
    let config = &setup.config;

    // 2. 创建相互独立的 AFL edge map 和 STG semantic map。
    let mut shmem_provider = UnixShMemProvider::new()?;
    let mut shmem = shmem_provider.new_shmem(MAP_SIZE)?;
    let mut semantic_shmem = shmem_provider.new_shmem(setup.semantic_map_size)?;
    let mut telemetry_shmem = shmem_provider.new_shmem(TELEMETRY_SIZE)?;
    unsafe {
        shmem.write_to_env("__AFL_SHM_ID")?;
        semantic_shmem.write_to_env("SEMANTIST_SEMANTIC_SHM_ID")?;
        telemetry_shmem.write_to_env("SEMANTIST_TELEMETRY_SHM_ID")?;
    }
    env::set_var("AFL_MAP_SIZE", MAP_SIZE.to_string());
    env::set_var("SEMANTIST_SEMANTIC_MAP_SIZE", setup.semantic_map_size.to_string());
    let (time_observer, edges_observer, semantic_observer, telemetry_observer, mut feedback, mut objective) =
        semantist_feedbacks!(&setup, shmem.as_slice_mut(), semantic_shmem.as_slice_mut(), telemetry_shmem.as_slice_mut(), None);
    let handles = (edges_observer.handle(), semantic_observer.handle(), telemetry_observer.handle());
    let mut state = semantist_new_state!(&setup, &mut feedback, &mut objective);

    let monitor = SimpleMonitor::new(|s| println!("{s}"));
    let mut mgr = SimpleEventManager::new(monitor);

    // 6. scheduler 决定下一轮从 corpus 里挑哪个 testcase。
    let scheduler = TargetAwareScheduler::new(config.metadata_bootstrap.clone(), config.ablation);
    let mut fuzzer = StdFuzzer::builder()
        .scheduler(scheduler)
        .feedback(feedback)
        .objective(objective)
        .build();

    // 7. ForkserverExecutor 启动 AFL instrumentation 目标，执行每个结构化输入文件。
    let mut tokens = Tokens::new();
    let executor = ForkserverExecutor::builder()
        .program(config.target.to_string_lossy().to_string())
        .debug_child(config.debug_child)
        .shmem_provider(&mut shmem_provider)
        .autotokens(&mut tokens)
        .coverage_map_size(MAP_SIZE)
        .is_persistent(config.execution == ExecutionMode::Persistent)
        .timeout(config.timeout)
        .build(tuple_list!(
            PhaseMarkerObserver::opening(),
            time_observer,
            edges_observer,
            semantic_observer,
            telemetry_observer,
            PhaseMarkerObserver::closing()
        ))?;
    let mut executor = ProfiledExecutor { inner: executor };

    // AFL 目标可能报告真实 map size；observer 跟着截断，减少无效 map 区域。
    if let Some(dynamic_map_size) = executor.inner.coverage_map_size() {
        executor.observers_mut()[&handles.0]
            .as_mut()
            .truncate(dynamic_map_size);
    }
    semantist_campaign!(&setup, fuzzer, executor, state, mgr, handles, tokens, true, config.execution.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn testcase_metadata_is_serializable_and_native() {
        let metadata = StSeedMetadata::new(SeedSource::Initial, None);
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
    fn borrowed_seed_parsing_matches_the_allocating_parser() {
        // The parsers this module used before borrowing record fields.
        fn reference_split(name: &str) -> Option<(usize, String)> {
            let (prefix, rest) = name.split_once('.')?;
            if prefix.is_empty() || rest.is_empty() || !prefix.chars().all(|ch| ch.is_ascii_digit()) {
                return None;
            }
            Some((prefix.parse().ok()?, rest.to_string()))
        }
        fn reference_parse(text: &str) -> (Vec<(String, String, String)>, Vec<(usize, Vec<(String, String, String)>)>, bool) {
            let records = parse_structured_records(text);
            let has_cycle = records.iter().any(|record| reference_split(&record.name).is_some());
            let mut shared = Vec::new();
            let mut cycles = BTreeMap::<usize, Vec<(String, String, String)>>::new();
            for record in records {
                match reference_split(&record.name) {
                    Some((cycle, name)) => cycles.entry(cycle).or_default().push((name, record.ty, record.value)),
                    None => shared.push((record.name, record.ty, record.value)),
                }
            }
            (shared, cycles.into_iter().collect(), has_cycle)
        }
        let pieces = ["X", " 0.EN ", "1.x", "12.Y.Z", ".A", "3.", "a.B", "00.Q", "99999999999999999999999.R", ""];
        let types = ["int", " BOOL", "", "Real "];
        let mut rand = StdRand::with_seed(3);
        for _ in 0..500 {
            let mut text = String::new();
            for _ in 0..rand.below_or_zero(8) {
                let name = pieces[rand.below_or_zero(pieces.len())];
                match rand.below_or_zero(4) {
                    0 => text.push_str(&format!("{name}\n")),
                    1 => text.push_str(&format!("{name},{}\n", types[rand.below_or_zero(types.len())])),
                    _ => text.push_str(&format!("{name},{}, v,1 \n", types[rand.below_or_zero(types.len())])),
                }
            }
            let seed = parse_cycle_aware_seed(&text);
            let tuples = |records: &[StRecord]| records.iter()
                .map(|record| (record.name.clone(), record.ty.clone(), record.value.clone())).collect::<Vec<_>>();
            let actual = (
                tuples(&seed.shared),
                seed.cycles.iter().map(|(cycle, records)| (*cycle, tuples(records))).collect::<Vec<_>>(),
                has_cycle_record(&text),
            );
            assert_eq!(actual, reference_parse(&text), "{text:?}");
        }
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
                seed_id: CorpusId(1),
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
        // The stage keeps the guide it actually mutated. A retained child
        // supplies the observed prefix for the next scheduler decision.
        assert_eq!(runtime.guide_target_id.as_deref(), Some("target:branch"));
        assert!(runtime.covered_target_ids.contains("target:branch"));
        runtime.cache_seed_profile(SeedProfile {
            seed_id: CorpusId(2),
            covered_target_ids: vec!["target:branch".to_string()],
            ordered_target_ids: vec!["target:branch".to_string()],
            ordered_trace_complete: true,
            ..SeedProfile::default()
        });
        runtime.select_cached().unwrap();
        assert_eq!(runtime.guide_target_id.as_deref(), Some("target:violation"));

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
        assert!(semantic_mutate_cycle_seed(
            &mut rand,
            &mut seed,
            &context,
            SemantiSTAblation::Full,
        ));
        assert!((4..=64).contains(&seed.cycles.len()));
        for records in seed.cycles.values() {
            assert_eq!(records[0].name, "STATE");
        }
    }

    #[test]
    fn state_transfer_reads_guide_a_preterminal_cycle_without_touching_the_goal_cycle() {
        let field = ControllableSeedField {
            symbol: "FB.EN".to_string(),
            seed_field: Some("EN".to_string()),
            role: None,
            type_name: Some("BOOL".to_string()),
            configuration_source: None,
            type_fact: None,
        };
        let selection = ActiveSelection {
            guide_target_id: "target".to_string(),
            controllable_seed_fields: vec![field],
            state_transfer_chains: vec![semantic::StateTransfer {
                reads: vec!["FB.EN".to_string()],
                ..semantic::StateTransfer::default()
            }],
            ..ActiveSelection::default()
        };
        let context = semantic_target_context(&selection);
        assert_eq!(context.transition_seed_fields.len(), 1);
        let mut seed = parse_cycle_aware_seed("0.EN,BOOL,0\n1.EN,BOOL,0\n");
        let mut rand = StdRand::with_seed(7);
        assert!(mutate_transition_prefix(
            &mut rand,
            &mut seed,
            &context.transition_seed_fields
        ));
        assert_eq!(seed.cycles.get(&0).unwrap()[0].value, "1");
        assert_eq!(seed.cycles.get(&1).unwrap()[0].value, "0");
    }

    #[test]
    fn fallback_cycle_growth_respects_the_execution_bound() {
        let mut seed = parse_cycle_aware_seed("0.EN,BOOL,0\n");
        ensure_cycle_hint(&mut seed, fb_max_cycles());
        let mut rand = StdRand::with_seed(7);
        cycle_clone_cycle(&mut rand, &mut seed);
        cycle_insert_cycle(&mut rand, &mut seed);
        assert_eq!(seed.cycles.len(), fb_max_cycles());
    }

    #[test]
    fn semantic_prefix_growth_preserves_the_terminal_cycle() {
        let mut seed = parse_cycle_aware_seed("0.EN,BOOL,0\n1.EN,BOOL,1\n");
        let mut rand = StdRand::with_seed(7);
        assert!(expand_state_prefix(&mut rand, &mut seed));
        assert_eq!(seed.cycles.len(), 3);
        assert_eq!(seed.cycles.get(&0).unwrap()[0].value, "0");
        assert_eq!(seed.cycles.get(&1).unwrap()[0].value, "0");
        assert_eq!(seed.cycles.get(&2).unwrap()[0].value, "1");
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
            rng_seed: None,
            debug_child: false,
            ablation: SemantiSTAblation::Full,
            execution: ExecutionMode::Fork,
            consistency_dump: None,
            consistency_inputs: None,
        };

        assert_eq!(
            initial_corpus_dirs(&config),
            vec![
                PathBuf::from("unified-corpus/seeds"),
                PathBuf::from("unified-corpus/runtime"),
            ]
        );
    }

    #[test]
    fn runtime_seed_metadata_is_appended_in_batches() {
        let root = std::env::temp_dir().join(format!("semantist-bootstrap-batch-{}", current_nanos()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("testcase-metadata.bootstrap.json");
        let mut scheduler = TargetAwareScheduler::new(Some(path.clone()), SemantiSTAblation::Full);
        let mut feedback = SemanticNoveltyFeedback;
        let mut objective = SemanticNoveltyFeedback;
        let mut state = StdState::new(StdRand::with_seed(1), libafl::corpus::InMemoryCorpus::<BytesInput>::new(),
            libafl::corpus::InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap();
        let parent = state.corpus_mut().add(Testcase::from(BytesInput::new(b"X,INT,1\n".to_vec()))).unwrap();
        scheduler.on_add(&mut state, parent).unwrap();
        *state.corpus_mut().current_mut() = Some(parent);
        let child = state.corpus_mut().add(Testcase::from(BytesInput::new(b"X,INT,2\n".to_vec()))).unwrap();
        scheduler.on_add(&mut state, child).unwrap();
        // Seed identity in memory is the CorpusId; no content hash yet.
        let attached = state.corpus().get(child).unwrap().borrow().metadata::<StSeedMetadata>().unwrap().clone();
        assert!(attached.content_hash.is_empty());
        assert_eq!(attached.parent, Some(parent));
        assert!(!path.exists());
        scheduler.flush_bootstrap(&mut state).unwrap();
        let records = load_bootstrap_metadata(Some(&path));
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 2);
        let child_record = &records[&content_hash(b"X,INT,2\n")];
        assert_eq!(child_record.parent_id.as_deref(), Some(content_hash(b"X,INT,1\n").as_str()));
        assert!(records[&content_hash(b"X,INT,1\n")].parent_id.is_none());
        scheduler.flush_bootstrap(&mut state).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 2);
        // The inputs themselves are persisted by the LibAFL corpus only.
        assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn sanitizer_logs_are_rescanned_only_when_they_may_have_changed() {
        let root = std::env::temp_dir().join(format!("semantist-sanitizer-scan-{}", current_nanos()));
        let logs = root.join("sanitizer-logs");
        fs::create_dir_all(&logs).unwrap();
        let mut feedback = RuntimeFindingFeedback::new(root.join("findings"), root.join("target"), None,
            root.join("events.jsonl"), logs.clone());
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.take_new_sanitizer_evidence().is_none());
        std::thread::sleep(SANITIZER_LOG_SETTLE + Duration::from_millis(20));
        assert!(!feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Crash));
        let report = b"==1==ERROR: AddressSanitizer: heap-buffer-overflow\nSUMMARY: AddressSanitizer: heap-buffer-overflow (x)\n";
        fs::write(logs.join("asan.11"), report).unwrap();
        fs::write(logs.join("asan.12"), report).unwrap();
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.take_new_sanitizer_evidence().is_some());
        // The second new log is claimed by the next execution, as before.
        std::thread::sleep(SANITIZER_LOG_SETTLE + Duration::from_millis(20));
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.take_new_sanitizer_evidence().is_some());
        assert!(!feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn reused_parsed_seeds_mutate_like_fresh_parses() {
        let inputs = [
            "0.EN,BOOL,1\n0.X,INT,3\n1.EN,BOOL,0\n1.X,INT,-4\n",
            "POS,INT,0\nSTR,STRING,A\n",
            "SHARED,int,1\n0.A,REAL,1.5\n2.A,REAL,-0.5\n",
            "",
        ];
        let contexts = [
            TargetContext::default(),
            TargetContext {
                target_id: Some("target".to_string()),
                semantic_task_id: Some("task".to_string()),
                cycle_hint: Some(3),
                ..TargetContext::default()
            },
        ];
        let new_state = || {
            let (mut feedback, mut objective) = (SemanticNoveltyFeedback, SemanticNoveltyFeedback);
            StdState::new(StdRand::with_seed(9), libafl::corpus::InMemoryCorpus::<BytesInput>::new(),
                libafl::corpus::InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap()
        };
        let (mut reused_state, mut fresh_state) = (new_state(), new_state());
        let mut reused = StructuredStMutator::new(SemantiSTAblation::Full);
        let mut rand = StdRand::with_seed(5);
        for _ in 0..500 {
            let original = BytesInput::new(inputs[rand.below_or_zero(inputs.len())].as_bytes().to_vec());
            let context = &contexts[rand.below_or_zero(contexts.len())];
            let (mut cached, mut parsed) = (original.clone(), original);
            reused.mutate_with_context(&mut reused_state, &mut cached, context).unwrap();
            StructuredStMutator::new(SemantiSTAblation::Full)
                .mutate_with_context(&mut fresh_state, &mut parsed, context)
                .unwrap();
            assert_eq!(cached.as_ref(), parsed.as_ref());
        }
    }

    #[test]
    fn fatal_sanitizer_logs_are_checked_only_after_failures() {
        let root = std::env::temp_dir().join(format!("semantist-sanitizer-fatal-{}", current_nanos()));
        let logs = root.join("sanitizer-logs");
        fs::create_dir_all(&logs).unwrap();
        let mut feedback = RuntimeFindingFeedback::new(root.join("findings"), root.join("target"), None,
            root.join("events.jsonl"), logs.clone())
        .with_logs_only_after_failures(true);
        // The first execution still scans the logs left by an earlier run.
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.take_new_sanitizer_evidence().is_none());
        let report = b"==1==ERROR: AddressSanitizer: FPE\nSUMMARY: AddressSanitizer: FPE (x)\n";
        fs::write(logs.join("asan.11"), report).unwrap();
        fs::write(logs.join("asan.12"), report).unwrap();
        assert!(!feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Crash));
        assert!(feedback.take_new_sanitizer_evidence().is_some());
        // A log left unclaimed by a crash is still claimed by the next execution.
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.take_new_sanitizer_evidence().is_some());
        assert!(!feedback.sanitizer_logs_may_have_changed(&ExitKind::Ok));
        assert!(feedback.sanitizer_logs_may_have_changed(&ExitKind::Timeout));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn sanitizer_option_flags_follow_the_last_setting() {
        assert_eq!(sanitizer_option_flag("abort_on_error=1:halt_on_error=1:log_path=x", "halt_on_error"), Some(true));
        assert_eq!(sanitizer_option_flag("halt_on_error=1 halt_on_error=0", "halt_on_error"), Some(false));
        assert_eq!(sanitizer_option_flag("abort_on_error=1:log_path=x", "halt_on_error"), None);
        assert_eq!(sanitizer_option_flag("halt_on_error=true", "halt_on_error"), Some(true));
    }

    #[test]
    fn restart_state_lives_in_the_fuzzer_state() {
        let root = std::env::temp_dir().join(format!("semantist-restart-state-{}", current_nanos()));
        fs::create_dir_all(root.join("logs")).unwrap();
        let mut feedback = SemanticNoveltyFeedback;
        let mut objective = SemanticNoveltyFeedback;
        let mut state = StdState::new(StdRand::with_seed(1), libafl::corpus::InMemoryCorpus::<BytesInput>::new(),
            libafl::corpus::InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap();

        // Budget windows: a fresh stage of a restarted client resumes them.
        state.add_metadata(SemanticBudgetWindows {
            executions_by_guide: HashMap::from([("task\0guide".to_string(), 5)]),
            ..SemanticBudgetWindows::default()
        });
        let mut stage = SemanticTaskBudgetStage::new(3, root.join("task-state.json"));
        let mut manager = libafl::events::NopEventManager::new();
        Stage::<(), _, _, ()>::perform(&mut stage, &mut (), &mut (), &mut state, &mut manager).unwrap();
        assert_eq!(state.metadata::<SemanticBudgetWindows>().unwrap().executions_by_guide["task\0guide"], 5);

        // Crash signatures: a restarted client does not report one again.
        state.add_metadata(RuntimeFindingRecords {
            signatures: HashMap::from([("crash".to_string(), 1)]),
            ..RuntimeFindingRecords::default()
        });
        let mut findings = RuntimeFindingFeedback::new(root.join("findings"), root.join("target"), None,
            root.join("events.jsonl"), root.join("logs"));
        let input = BytesInput::new(b"X,INT,1\n".to_vec());
        assert!(findings.is_interesting(&mut state, &mut manager, &input, &(), &ExitKind::Crash).unwrap());
        assert!(!root.join("findings").exists());
        assert_eq!(state.metadata::<RuntimeFindingRecords>().unwrap().signatures["crash"], 1);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn semantic_metadata_survives_the_restart_state_format() {
        use libafl_bolts::staterestore::StateRestorer;
        let mut feedback = SemanticNoveltyFeedback;
        let mut objective = SemanticNoveltyFeedback;
        let mut state = StdState::new(StdRand::with_seed(1), libafl::corpus::InMemoryCorpus::<BytesInput>::new(),
            libafl::corpus::InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap();
        let goal = serde_json::json!({"kind": {"kind": "binary", "operator": ">="}, "operands": []});
        let plan: semantic::SemanticTaskPlan = serde_json::from_value(serde_json::json!({
            "schema_version": semantic::PLAN_SCHEMA,
            "target_identity": "stg_stable_target_id",
            "tasks": [{"task_id": "t", "task_kind": "violation", "pou": "P", "terminal_target_id": "v",
                "control_obligations": [{"target_id": "v", "goal_expression": goal.clone()}],
                "ordered_waypoints": ["v"]}]
        })).unwrap();
        state.add_metadata(SemanticRuntimeMetadata::new(plan, &HashSet::from(["v".to_string()])));
        state.add_metadata(TargetContext { target_id: Some("v".into()), goal_expression: Some(goal.clone()), ..TargetContext::default() });
        let mut metadata = StSeedMetadata::new(SeedSource::Fuzzing, Some(CorpusId(3)));
        metadata.semantic_coverage.insert("v".into(), goal.clone());
        let mut testcase = Testcase::from(BytesInput::new(b"X,INT,1\n".to_vec()));
        testcase.add_metadata(metadata.clone());
        let id = state.corpus_mut().add(testcase).unwrap();

        let mut provider = UnixShMemProvider::new().unwrap();
        let mut restorer: StateRestorer<_, UnixShMemProvider> = StateRestorer::new(provider.new_shmem(1 << 20).unwrap());
        restorer.save(&state).unwrap();
        let restored: StdState<libafl::corpus::InMemoryCorpus<BytesInput>, BytesInput, StdRand,
            libafl::corpus::InMemoryCorpus<BytesInput>> = restorer.restore().unwrap().unwrap();
        assert_eq!(restored.metadata::<TargetContext>().unwrap().goal_expression, Some(goal.clone()));
        assert_eq!(restored.metadata::<SemanticRuntimeMetadata>().unwrap().plan.tasks[0]
            .control_obligations[0].goal_expression, Some(goal));
        let restored_seed = restored.corpus().get(id).unwrap().borrow().metadata::<StSeedMetadata>().unwrap().clone();
        assert_eq!(restored_seed, metadata);
        assert_eq!(restored_seed.parent, Some(CorpusId(3)));
        // The bootstrap layout is the derived JSON, without the parent.
        assert!(!serde_json::to_string(&metadata).unwrap().contains("parent"));
    }

    #[test]
    fn execution_modes_parse_only_supported_names() {
        assert_eq!(ExecutionMode::parse("fork").unwrap(), ExecutionMode::Fork);
        assert_eq!(ExecutionMode::parse("persistent").unwrap(), ExecutionMode::Persistent);
        assert_eq!(ExecutionMode::Persistent.as_str(), "persistent");
        assert!(ExecutionMode::parse("inprocess").is_err());
    }

    #[test]
    fn ablation_profiles_keep_unrelated_mechanisms_enabled() {
        let init = SemantiSTAblation::parse("no_semantic_init").unwrap();
        assert!(!init.semantic_initial_generation());
        assert!(init.semantic_scheduling());
        assert!(init.targeted_mutation());

        let mutation = SemantiSTAblation::parse("no_targeted_mutation").unwrap();
        assert!(mutation.semantic_initial_generation());
        assert!(mutation.semantic_scheduling());
        assert!(!mutation.targeted_mutation());

        let transition = SemantiSTAblation::parse("no_transition_guidance").unwrap();
        assert!(!transition.transition_guidance());
        assert!(transition.prefix_growth());

        let growth = SemantiSTAblation::parse("no_prefix_growth").unwrap();
        assert!(growth.transition_guidance());
        assert!(!growth.prefix_growth());

        let combined = SemantiSTAblation::parse("no_multicycle_guidance").unwrap();
        assert!(!combined.transition_guidance());
        assert!(!combined.prefix_growth());
        assert!(combined.targeted_mutation());

        let queue = SemantiSTAblation::parse("queue_havoc").unwrap();
        assert!(!queue.semantic_initial_generation());
        assert!(!queue.semantic_scheduling());
        assert!(!queue.targeted_mutation());
        assert!(SemantiSTAblation::parse("not_a_profile").is_err());
    }
}
