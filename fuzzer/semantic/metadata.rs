#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
enum SeedSource {
    Initial,
    SemanticGeneration,
    Fuzzing,
}

#[derive(Clone, Debug, Deserialize, PartialEq, SerdeAny, Serialize)]
struct StSeedMetadata {
    source: SeedSource,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    parent_id: Option<String>,
    content_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    coverage_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    constraints: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    target_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    covered_target: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    semantic_coverage: BTreeMap<String, serde_json::Value>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    covered_target_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    ordered_target_ids: Vec<String>,
    #[serde(default)]
    ordered_trace_complete: bool,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    covered_targets_by_cycle: BTreeMap<String, Vec<String>>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    cycle_ids: Vec<usize>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    state_signatures: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    target_affinity: BTreeMap<String, f64>,
    #[serde(default, skip_serializing_if = "is_zero")]
    new_afl_edges: u64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    new_st_branch_sides: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    new_state_transitions: Vec<String>,
    #[serde(default, skip_serializing_if = "is_false")]
    is_finding: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_severity: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_confidence: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_detail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    finding_artifact: Option<String>,
}

fn is_zero(value: &u64) -> bool {
    *value == 0
}

fn is_false(value: &bool) -> bool {
    !*value
}

impl StSeedMetadata {
    fn new(source: SeedSource, parent_id: Option<String>, bytes: &[u8]) -> Self {
        Self {
            source,
            parent_id,
            content_hash: content_hash(bytes),
            coverage_hash: None,
            constraints: vec![],
            target_id: None,
            covered_target: None,
            semantic_coverage: BTreeMap::new(),
            covered_target_ids: vec![],
            ordered_target_ids: vec![],
            ordered_trace_complete: false,
            covered_targets_by_cycle: BTreeMap::new(),
            cycle_ids: vec![],
            state_signatures: vec![],
            target_affinity: BTreeMap::new(),
            new_afl_edges: 0,
            new_st_branch_sides: vec![],
            new_state_transitions: vec![],
            is_finding: false,
            finding_source: None,
            finding_kind: None,
            finding_severity: None,
            finding_confidence: None,
            finding_detail: None,
            finding_artifact: None,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
struct RuntimeFindingRecord {
    schema_version: &'static str,
    executions: u64,
    content_hash: String,
    source: String,
    kind: String,
    severity: String,
    confidence: String,
    detail: String,
    trigger: String,
    sanitizer_log: Option<String>,
    parent_id: Option<String>,
    reproducer: String,
    target_corpus_hint: String,
    stack_status: String,
    stack: Option<String>,
    target_kind: String,
    cycle_count: usize,
    cycle_ids: Vec<usize>,
    stale_cycles: Vec<i32>,
    last_changed_cycle: Option<i32>,
    state_hashes: Vec<CycleStateHash>,
    // One record per crash signature; later inputs with the same signature
    // are counted as duplicates and not persisted.
    signature: String,
    // Monotonic seconds since the fuzzer process started (time-to-exposure).
    elapsed_seconds: f64,
    unix_time_seconds: u64,
}

// A newly reached exact STG violation target, which is a solution without a
// crash. The stable target ID ties it back to the STG model.
#[derive(Debug, Serialize)]
struct SemanticFindingRecord {
    schema_version: &'static str,
    kind: &'static str,
    source: String,
    content_hash: String,
    reproducer: String,
    target_id: String,
    target_kind: String,
    hazard: Option<String>,
    modeling: String,
    target_source: Option<serde_json::Value>,
    executions: u64,
    elapsed_seconds: f64,
    unix_time_seconds: u64,
}

#[derive(Clone, Debug, Serialize)]
struct CycleStateHash {
    cycle: i32,
    state_hash_before: String,
    state_hash_after: String,
    changed: bool,
    stale: bool,
}

#[derive(Clone, Debug, Default)]
struct SeedCycleMetadata {
    target_kind: String,
    cycle_count: usize,
    cycle_ids: Vec<usize>,
}

#[derive(Clone, Debug, Default)]
struct TraceCycleMetadata {
    stale_cycles: Vec<i32>,
    last_changed_cycle: Option<i32>,
    state_hashes: Vec<CycleStateHash>,
}

#[derive(Debug, Serialize)]
struct RuntimeFindingEvent {
    event: &'static str,
    source: String,
    kind: String,
    content_hash: String,
    reproducer: String,
    sanitizer_log: Option<String>,
    signature: String,
    executions: u64,
    elapsed_seconds: f64,
    unix_time_seconds: u64,
}

#[derive(Clone, Debug, Deserialize, SerdeAny, Serialize)]
struct SeedSourceContext {
    source: SeedSource,
}

#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct SemanticStageControl {
    skip_mutation_once: bool,
}

#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct SemanticCorpusIndexMetadata {
    ids_by_hash: HashMap<String, CorpusId>,
}

#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct TargetContext {
    target_id: Option<String>,
    target_type: Option<String>,
    control_kind: Option<String>,
    side: Option<String>,
    cycle_hint: Option<u64>,
    semantic_task_id: Option<String>,
    terminal_target_id: Option<String>,
    active_path: Vec<String>,
    remaining_obligations: Vec<String>,
    goal_expression_id: Option<String>,
    goal_expression: Option<serde_json::Value>,
    value_definitions: BTreeMap<String, serde_json::Value>,
    // Precomputed controllable inputs read by each state transfer. The hot
    // mutation path need not walk the semantic JSON graph on every attempt.
    #[serde(default)]
    transition_seed_fields: Vec<Vec<ControllableSeedField>>,
    controllable_seed_fields: Vec<ControllableSeedField>,
    hazard_prerequisites: Vec<String>,
    modeling_status: Option<String>,
    task_kind: Option<String>,
    semantic_priority: Option<f64>,
    hazard_kind: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct SemanticCoverageMetadata {
    #[serde(default)]
    last_interesting: bool,
    offset: u64,
    runtime_trace_offset: u64,
    state_trace_offset: u64,
    seen_targets: HashSet<String>,
    seen_state_signatures: HashSet<u64>,
    last_covered_target_ids: Vec<String>,
    #[serde(default)]
    last_ordered_target_ids: Vec<String>,
    #[serde(default)]
    last_ordered_trace_complete: bool,
    #[serde(default)]
    last_scan_count: u32,
    // Scan cycles the harness executed over the whole run, for scans/s.
    #[serde(default)]
    total_scan_cycles: u64,
    #[serde(default)]
    last_telemetry_truncated: bool,
    #[serde(default)]
    last_new_state_signatures: Vec<String>,
    last_new_branch_sides: Vec<String>,
    last_new_state_transitions: Vec<String>,
    last_covered_target: Option<String>,
    last_covered_targets_by_cycle: BTreeMap<String, Vec<String>>,
    last_cycle_ids: Vec<usize>,
    last_state_signatures: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
struct RuntimeIdTable {
    schema_version: String,
    entries: Vec<RuntimeTargetEntry>,
}
