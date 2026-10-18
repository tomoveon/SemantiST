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

// Multiply-rotate hashing (FxHash's scheme) for integer keys on the
// execution path; std's SipHash is built for untrusted keys and costs more.
#[derive(Clone, Copy, Debug, Default)]
struct IdHasher(u64);

impl IdHasher {
    #[inline]
    fn add(&mut self, word: u64) {
        self.0 = (self.0.rotate_left(5) ^ word).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}

impl std::hash::Hasher for IdHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks(8) {
            let mut word = [0u8; 8];
            word[..chunk.len()].copy_from_slice(chunk);
            self.add(u64::from_le_bytes(word));
        }
    }

    fn write_u8(&mut self, value: u8) {
        self.add(u64::from(value));
    }

    fn write_u32(&mut self, value: u32) {
        self.add(u64::from(value));
    }

    fn write_u64(&mut self, value: u64) {
        self.add(value);
    }

    fn write_usize(&mut self, value: usize) {
        self.add(value as u64);
    }
}

type IdBuildHasher = std::hash::BuildHasherDefault<IdHasher>;

// An abstract FB state, a transition between two, or a hash line from the
// legacy text trace. Display gives the form stored in seed metadata.
#[derive(Clone, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
enum StateSignature {
    State(u64),
    Transition(u64),
    Legacy(String),
}

impl std::fmt::Display for StateSignature {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::State(value) => write!(formatter, "state:{value:016x}"),
            Self::Transition(value) => write!(formatter, "transition:{value:016x}"),
            Self::Legacy(text) => formatter.write_str(text),
        }
    }
}

// Evidence of the last execution, plus what all executions have covered.
// Targets are indices into `target_names`; string forms are built only when
// a seed keeps this evidence (attach_metadata) or a trace line is written.
#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct SemanticCoverageMetadata {
    #[serde(default)]
    last_interesting: bool,
    offset: u64,
    runtime_trace_offset: u64,
    state_trace_offset: u64,
    #[serde(default)]
    target_names: Vec<String>,
    #[serde(default)]
    seen_targets: Vec<bool>,
    #[serde(default)]
    seen_state_signatures: HashSet<StateSignature, IdBuildHasher>,
    #[serde(default)]
    last_covered: Vec<u32>,
    #[serde(default)]
    last_ordered: Vec<u32>,
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
    last_new_states: Vec<StateSignature>,
    #[serde(default)]
    last_new_branches: Vec<u32>,
    #[serde(default)]
    last_new_transitions: Vec<u32>,
    #[serde(default)]
    last_covered_index: Option<u32>,
    // (cycle, target) pairs, sorted and unique.
    #[serde(default)]
    last_cycle_targets: Vec<(i32, u32)>,
    // Cycles 0..last_cycle_count ran; extra cycle labels beyond that range.
    #[serde(default)]
    last_cycle_count: usize,
    #[serde(default)]
    last_extra_cycles: Vec<usize>,
    #[serde(default)]
    last_states: Vec<StateSignature>,
}

impl SemanticCoverageMetadata {
    fn names(&self, indices: &[u32]) -> Vec<String> {
        indices.iter().filter_map(|index| self.target_names.get(*index as usize)).cloned().collect()
    }

    fn last_covered_target_ids(&self) -> Vec<String> {
        let mut ids = self.names(&self.last_covered);
        ids.sort();
        ids.dedup();
        ids
    }

    fn last_ordered_target_ids(&self) -> Vec<String> {
        self.names(&self.last_ordered)
    }

    fn last_new_branch_sides(&self) -> Vec<String> {
        self.names(&self.last_new_branches)
    }

    fn last_new_state_transitions(&self) -> Vec<String> {
        self.names(&self.last_new_transitions)
    }

    fn last_covered_target(&self) -> Option<String> {
        self.last_covered_index.and_then(|index| self.target_names.get(index as usize).cloned())
    }

    fn last_covered_targets_by_cycle(&self) -> BTreeMap<String, Vec<String>> {
        let mut by_cycle = BTreeMap::<String, Vec<String>>::new();
        for (cycle, index) in &self.last_cycle_targets {
            if let Some(name) = self.target_names.get(*index as usize) {
                by_cycle.entry(cycle.to_string()).or_default().push(name.clone());
            }
        }
        for targets in by_cycle.values_mut() {
            targets.sort();
            targets.dedup();
        }
        by_cycle
    }

    fn last_cycle_ids(&self) -> Vec<usize> {
        (0..self.last_cycle_count).chain(self.last_extra_cycles.iter().copied()).collect()
    }

    fn last_state_signatures(&self) -> Vec<String> {
        self.last_states.iter().map(ToString::to_string).collect()
    }
}

#[derive(Clone, Debug, Deserialize)]
struct RuntimeIdTable {
    schema_version: String,
    entries: Vec<RuntimeTargetEntry>,
}
