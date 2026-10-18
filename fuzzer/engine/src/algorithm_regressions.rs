use super::*;
use libafl::corpus::InMemoryCorpus;
use libafl::events::NopEventManager;
use libafl::fuzzer::{ExecuteInputResult, ExecutionProcessor};

fn runtime_table() -> RuntimeIdTable {
    RuntimeIdTable {
        schema_version: "semantist.runtime-ids/1.0.0".into(),
        entries: ["v", "w"].into_iter().enumerate().map(|(runtime_id, stable_id)| RuntimeTargetEntry {
            runtime_id: runtime_id as u32 + 1, stable_id: stable_id.into(), pou: "P".into(),
            node_id: "n".into(), semantic_edge_ids: vec![], kind: "hazard_violation".into(),
            role: None, hazard: Some("bounds".into()), source: None, modeling: "exact".into(),
        }).collect(),
    }
}

fn telemetry_bytes(scans: u32, events: &[(u32, u32)], states: &[(u64, u64)], flags: u32) -> Vec<u8> {
    let mut bytes = vec![0; TELEMETRY_SIZE];
    for (index, value) in [scans, events.len() as u32, states.len() as u32, flags | 4].iter().enumerate() {
        bytes[index * 4..index * 4 + 4].copy_from_slice(&value.to_ne_bytes());
    }
    for (index, (target, cycle)) in events.iter().enumerate() {
        let offset = 16 + index * 8;
        bytes[offset..offset + 4].copy_from_slice(&target.to_ne_bytes());
        bytes[offset + 4..offset + 8].copy_from_slice(&cycle.to_ne_bytes());
    }
    for (index, (before, after)) in states.iter().enumerate() {
        let offset = 16 + TELEMETRY_EVENT_CAPACITY * 8 + index * 16;
        bytes[offset..offset + 8].copy_from_slice(&before.to_ne_bytes());
        bytes[offset + 8..offset + 16].copy_from_slice(&after.to_ne_bytes());
    }
    bytes
}

#[test]
fn solution_collection_precedes_classification_and_deduplicates_targets() {
    let mut map = [0; 3];
    let mut telemetry = telemetry_bytes(2, &[(1, 0), (1, 1)], &[(4, 5), (5, 6)], 0);
    let observer = unsafe { StdMapObserver::new("test-semantic", &mut map) };
    let telemetry_observer = unsafe { StdMapObserver::new("test-telemetry", &mut telemetry) };
    let map_handle = observer.handle();
    let telemetry_handle = telemetry_observer.handle();
    let mut evidence = SemanticIrCoverageFeedback::new(&observer, runtime_table()).with_telemetry(&telemetry_observer);
    evidence.trace_path = None;
    evidence.runtime_trace_path = None;
    evidence.state_trace_path = None;
    let mut objective = feedback_or!(SemanticEvidenceCollector(evidence), SemanticViolationObjective::new(&observer, &runtime_table()));
    let mut feedback = SemanticNoveltyFeedback;
    let mut state = StdState::new(StdRand::with_seed(1), InMemoryCorpus::<BytesInput>::new(),
        InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap();
    let plan = semantic::SemanticTaskPlan {
        tasks: vec![semantic::SemanticTask {
            task_id: "task-v".into(), terminal_target_id: "v".into(), task_kind: "violation".into(),
            ordered_waypoints: vec!["v".into()], modeling_status: "exact".into(), ..Default::default()
        }], ..Default::default()
    };
    state.add_metadata(SemanticRuntimeMetadata::new(plan, &HashSet::from(["v".into(), "w".into()])));
    let mut observers = tuple_list!(observer, telemetry_observer);
    observers.get_mut(&map_handle).unwrap().as_slice_mut()[1] = 1;
    let mut fuzzer = StdFuzzer::new(QueueScheduler::new(), feedback, objective);
    let mut manager = NopEventManager::new();
    let first = fuzzer.check_results(&mut state, &mut manager, &BytesInput::new(b"0.X,INT,1\n1.X,INT,2\n".to_vec()), &observers, &ExitKind::Ok).unwrap();
    assert_eq!(first, ExecuteInputResult::Solution);
    assert!(state.metadata::<SemanticRuntimeMetadata>().unwrap().covered_target_ids.contains("v"));
    let initial_states = state.metadata::<SemanticCoverageMetadata>().unwrap().last_state_signatures.clone();
    let repeated = fuzzer.check_results(&mut state, &mut manager, &BytesInput::new(b"0.X,INT,3\n1.X,INT,4\n".to_vec()), &observers, &ExitKind::Ok).unwrap();
    assert_ne!(repeated, ExecuteInputResult::Solution);
    let metadata = state.metadata::<SemanticCoverageMetadata>().unwrap();
    assert_eq!(metadata.last_cycle_ids, [0, 1]);
    assert_eq!(metadata.last_scan_count, 2);
    assert_eq!(metadata.total_scan_cycles, 4);
    assert_eq!(metadata.last_state_signatures, initial_states);
    assert!(metadata.last_new_state_signatures.is_empty());
    assert!(metadata.last_ordered_trace_complete);
    assert_eq!(metadata.last_ordered_target_ids, ["v", "v"]);

    // A separate target remains eligible; then a normal empty execution must
    // not inherit cycle, target or state evidence from either solution.
    observers.get_mut(&map_handle).unwrap().as_slice_mut().copy_from_slice(&[0, 0, 1]);
    observers.get_mut(&telemetry_handle).unwrap().as_slice_mut().copy_from_slice(&telemetry_bytes(1, &[(2, 0)], &[], 0));
    let other = fuzzer.check_results(&mut state, &mut manager, &BytesInput::new(vec![9]), &observers, &ExitKind::Ok).unwrap();
    assert_eq!(other, ExecuteInputResult::Solution);
    observers.get_mut(&map_handle).unwrap().as_slice_mut().fill(0);
    observers.get_mut(&telemetry_handle).unwrap().as_slice_mut().fill(0);
    fuzzer.check_results(&mut state, &mut manager, &BytesInput::new(vec![0]), &observers, &ExitKind::Ok).unwrap();
    let metadata = state.metadata::<SemanticCoverageMetadata>().unwrap();
    assert!(metadata.last_cycle_ids.is_empty());
    assert!(metadata.last_state_signatures.is_empty());
    assert!(metadata.last_ordered_target_ids.is_empty());
    assert!(!metadata.last_ordered_trace_complete);
    assert_eq!(metadata.total_scan_cycles, 5);
}

#[test]
fn telemetry_preserves_order_and_rejects_truncated_path_proofs() {
    let bytes = telemetry_bytes(10000, &[(2, 0), (1, 0), (2, 7)], &[(1, 2)], 1);
    let observation = read_semantic_telemetry(bytes.into_iter());
    assert_eq!(observation.scan_count, 10000);
    assert_eq!(observation.ordered_runtime_ids, [2, 1, 2]);
    assert_eq!(observation.cycles_by_runtime[&2], [0, 7]);
    assert!(!observation.ordered_complete);
    assert!(observation.truncated);
    assert!(!read_semantic_telemetry(vec![0; TELEMETRY_SIZE].into_iter()).ordered_complete);
}

#[test]
fn state_signatures_ignore_sequence_length_but_distinguish_state_progress() {
    let once = read_semantic_telemetry(telemetry_bytes(1, &[(1, 0)], &[(16, 17)], 0).into_iter());
    let repeated = read_semantic_telemetry(telemetry_bytes(2, &[(1, 0), (1, 1)], &[(16, 17), (16, 17)], 0).into_iter());
    assert_eq!(once.state_signatures, repeated.state_signatures);
    let advanced = read_semantic_telemetry(telemetry_bytes(1, &[(1, 0)], &[(16, 18)], 0).into_iter());
    assert_ne!(once.state_signatures, advanced.state_signatures);
    let many = (0..TELEMETRY_STATE_CAPACITY as u64).map(|value| (value, value + 1)).collect::<Vec<_>>();
    assert!(read_semantic_telemetry(telemetry_bytes(512, &[], &many, 0).into_iter()).state_signatures.len() <= MAX_EXECUTION_STATE_SIGNATURES);
}

#[test]
fn input_deduplication_uses_bounded_fifo_retention() {
    let mut filter = ContentHashInputFilter::default();
    for value in 0..INPUT_HASH_CAPACITY as u64 { assert!(filter.admit_hash(value)); }
    assert!(!filter.admit_hash(0));
    assert!(filter.admit_hash(INPUT_HASH_CAPACITY as u64));
    assert_eq!(filter.seen_hashes.len(), INPUT_HASH_CAPACITY);
    assert_eq!(filter.insertion_order.len(), INPUT_HASH_CAPACITY);
    assert!(filter.admit_hash(0));
    assert_eq!(filter.seen_hashes.len(), INPUT_HASH_CAPACITY);
}

#[test]
fn definitions_keep_symbol_identity_and_mark_ambiguous_writes() {
    let expression = serde_json::json!({"kind": {"kind": "literal", "value": {"value": "1"}}});
    let selection = ActiveSelection {
        state_transfer_chains: vec![semantic::StateTransfer {
            operations: vec![
                serde_json::json!({"target_symbol":"P.TMP", "value_expression":expression}),
                serde_json::json!({"target_symbol":"Q.TMP", "value_expression":expression}),
                serde_json::json!({"target_symbol":"P.TMP", "value_expression":expression}),
                serde_json::json!({"target_symbol":"P.UNKNOWN"}),
                serde_json::json!({"target_symbol":"P.UNKNOWN", "value_expression":expression}),
            ], ..Default::default()
        }, semantic::StateTransfer {
            writes: vec!["P.CALL_INPUT".to_string(), "Q.TMP".to_string()],
            operations: vec![serde_json::json!({"kind":"call"})],
            ..Default::default()
        }], ..Default::default()
    };
    let context = semantic_target_context(&selection);
    assert_eq!(context.value_definitions.keys().cloned().collect::<Vec<_>>(), ["P.CALL_INPUT", "P.TMP", "P.UNKNOWN", "Q.TMP"]);
    assert!(context.value_definitions["P.CALL_INPUT"].is_null());
    assert!(context.value_definitions["P.TMP"].is_null());
    assert!(context.value_definitions["P.UNKNOWN"].is_null());
    assert!(context.value_definitions["Q.TMP"].is_null());
}
