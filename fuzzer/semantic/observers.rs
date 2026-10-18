// ABI shared with compiler/instrumentation/runtime/semantic_coverage.h.
const TELEMETRY_EVENT_CAPACITY: usize = 4096;
const TELEMETRY_STATE_CAPACITY: usize = 512;
const TELEMETRY_SIZE: usize = 16 + TELEMETRY_EVENT_CAPACITY * 8 + TELEMETRY_STATE_CAPACITY * 16;
const MAX_EXECUTION_STATE_SIGNATURES: usize = 256;
const MAX_GLOBAL_STATE_SIGNATURES: usize = 4096;

#[derive(Debug, Default)]
struct SemanticTelemetry {
    scan_count: u32,
    ordered_runtime_ids: Vec<u32>,
    cycles_by_runtime: BTreeMap<u32, Vec<i32>>,
    state_signatures: Vec<String>,
    ordered_complete: bool,
    truncated: bool,
}

fn read_semantic_telemetry(mut bytes: impl Iterator<Item = u8>) -> SemanticTelemetry {
    fn u32_from(bytes: &mut impl Iterator<Item = u8>) -> u32 {
        u32::from_ne_bytes(std::array::from_fn(|_| bytes.next().unwrap_or(0)))
    }
    fn u64_from(bytes: &mut impl Iterator<Item = u8>) -> u64 {
        u64::from_ne_bytes(std::array::from_fn(|_| bytes.next().unwrap_or(0)))
    }
    let scan_count = u32_from(&mut bytes);
    let event_count = u32_from(&mut bytes) as usize;
    let state_count = u32_from(&mut bytes) as usize;
    let flags = u32_from(&mut bytes);
    let mut result = SemanticTelemetry {
        scan_count,
        ordered_complete: flags & 4 != 0 && flags & 1 == 0 && event_count <= TELEMETRY_EVENT_CAPACITY,
        truncated: flags & 3 != 0 || event_count > TELEMETRY_EVENT_CAPACITY || state_count > TELEMETRY_STATE_CAPACITY,
        ..SemanticTelemetry::default()
    };
    for _ in 0..event_count.min(TELEMETRY_EVENT_CAPACITY) {
        let runtime_id = u32_from(&mut bytes);
        let cycle = u32_from(&mut bytes);
        result.ordered_runtime_ids.push(runtime_id);
        if let Ok(cycle) = i32::try_from(cycle) {
            let cycles = result.cycles_by_runtime.entry(runtime_id).or_default();
            if cycles.last() != Some(&cycle) { cycles.push(cycle); }
        }
    }
    if state_count > 0 {
        let skip = (TELEMETRY_EVENT_CAPACITY - event_count.min(TELEMETRY_EVENT_CAPACITY)) * 8;
        if skip > 0 { bytes.nth(skip - 1); }
        let mut seen = HashSet::new();
        for _ in 0..state_count.min(TELEMETRY_STATE_CAPACITY) {
            let before = u64_from(&mut bytes);
            let after = u64_from(&mut bytes);
            // No cycle ordinal in the signature: repeating an unchanged state
            // must not create fresh feedback merely by lengthening an input.
            for (kind, signature) in [(0u8, before), (0, after), (1, before.rotate_left(17) ^ after)] {
                if result.state_signatures.len() < MAX_EXECUTION_STATE_SIGNATURES && seen.insert((kind, signature)) {
                    result.state_signatures.push(format!("{}:{signature:016x}", if kind == 0 { "state" } else { "transition" }));
                }
            }
        }
    }
    result
}

#[derive(Debug)]
struct SemanticViolationObjective<C, O> {
    name: Cow<'static, str>,
    map_ref: Handle<C>,
    candidate_slots: HashMap<u32, usize>,
    seen_candidates: Vec<bool>,
    slot_entries: Vec<RuntimeTargetEntry>,
    pending: Option<PendingSemanticFindings>,
    phantom: std::marker::PhantomData<O>,
}

fn is_exact_semantic_violation(entry: &RuntimeTargetEntry) -> bool {
    entry.modeling == "exact"
        && matches!(
            entry.kind.as_str(),
            "hazard_violation" | "property_violation"
        )
}

impl<C, O> SemanticViolationObjective<C, O>
where
    C: Handled + Named,
{
    fn new(map_observer: &C, table: &RuntimeIdTable) -> Self {
        let mut keys = BTreeMap::new();
        let mut candidate_slots = HashMap::new();
        let mut slot_entries = Vec::new();
        for entry in table.entries.iter().filter(|entry| is_exact_semantic_violation(entry)) {
            let next = keys.len();
            let slot = *keys.entry((entry.stable_id.clone(), entry.kind.clone(), entry.hazard.clone())).or_insert(next);
            if slot == slot_entries.len() {
                slot_entries.push(entry.clone());
            }
            candidate_slots.insert(entry.runtime_id, slot);
        }
        Self {
            name: Cow::Borrowed("SemanticViolationObjective"),
            map_ref: map_observer.handle(),
            candidate_slots,
            seen_candidates: vec![false; keys.len()],
            slot_entries,
            pending: None,
            phantom: std::marker::PhantomData,
        }
    }

    /// Report each newly reached violation target so its input is persisted
    /// with the stable target ID and hazard kind.
    fn with_pending_findings(mut self, pending: PendingSemanticFindings) -> Self {
        self.pending = Some(pending);
        self
    }
}

impl<C, O> Named for SemanticViolationObjective<C, O> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<C, O, S> StateInitializer<S> for SemanticViolationObjective<C, O> {}

impl<C, EM, I, O, OT, S> Feedback<EM, I, OT, S> for SemanticViolationObjective<C, O>
where
    C: AsRef<O>,
    O: MapObserver<Entry = u8>,
    OT: MatchName,
{
    fn is_interesting(
        &mut self,
        _state: &mut S,
        _manager: &mut EM,
        _input: &I,
        observers: &OT,
        _exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        let observer = observers
            .get(&self.map_ref)
            .expect("semantic MapObserver not found for objective")
            .as_ref();
        let initial = observer.initial();
        let usable_count = observer.usable_count();
        let mut novel = false;
        // The objective only needs exact violation candidates. The evidence
        // collector already scans all targets, so avoid a second full-map pass.
        for (&runtime_id, &slot) in &self.candidate_slots {
            if self.seen_candidates[slot] {
                continue;
            }
            let Ok(index) = usize::try_from(runtime_id) else { continue; };
            if index < usable_count && observer.get(index) != initial {
                self.seen_candidates[slot] = true;
                novel = true;
                if let Some(pending) = &self.pending {
                    pending.borrow_mut().push(self.slot_entries[slot].clone());
                }
            }
        }
        Ok(novel)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct RuntimeTargetEntry {
    runtime_id: u32,
    stable_id: String,
    pou: String,
    node_id: String,
    semantic_edge_ids: Vec<String>,
    kind: String,
    role: Option<String>,
    hazard: Option<String>,
    source: Option<serde_json::Value>,
    modeling: String,
}

#[derive(Debug)]
struct SemanticIrCoverageFeedback<C, O> {
    name: Cow<'static, str>,
    map_ref: Handle<C>,
    telemetry_ref: Option<Handle<C>>,
    entries: Vec<Option<RuntimeTargetEntry>>,
    trace_path: Option<PathBuf>,
    runtime_trace_path: Option<PathBuf>,
    state_trace_path: Option<PathBuf>,
    phantom: std::marker::PhantomData<O>,
}

impl<C, O> SemanticIrCoverageFeedback<C, O>
where
    C: Handled + Named,
{
    fn new(map_observer: &C, table: RuntimeIdTable) -> Self {
        let max_id = table
            .entries
            .iter()
            .map(|entry| entry.runtime_id as usize)
            .max()
            .unwrap_or(0);
        let mut entries = vec![None; max_id.saturating_add(1)];
        for entry in table.entries {
            let index = entry.runtime_id as usize;
            if index >= entries.len() {
                entries.resize(index + 1, None);
            }
            entries[index] = Some(entry);
        }
        Self {
            name: Cow::Borrowed("SemanticIrCoverageFeedback"),
            map_ref: map_observer.handle(),
            telemetry_ref: None,
            entries,
            trace_path: env::var("SEMANTIST_SEMANTIC_TRACE_FILE")
                .ok()
                .map(PathBuf::from),
            runtime_trace_path: env::var("SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE")
                .ok()
                .map(PathBuf::from),
            state_trace_path: env::var("SEMANTIST_FB_STATE_TRACE_FILE")
                .ok()
                .map(PathBuf::from),
            phantom: std::marker::PhantomData,
        }
    }

    fn with_telemetry(mut self, observer: &C) -> Self {
        self.telemetry_ref = Some(observer.handle());
        self
    }
}

impl<C, O> Named for SemanticIrCoverageFeedback<C, O> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<C, O, S> StateInitializer<S> for SemanticIrCoverageFeedback<C, O>
where
    S: HasMetadata,
{
    fn init_state(&mut self, state: &mut S) -> Result<(), Error> {
        let runtime_trace_offset = self
            .runtime_trace_path
            .as_deref()
            .and_then(|path| fs::metadata(path).ok())
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        let state_trace_offset = self
            .state_trace_path
            .as_deref()
            .and_then(|path| fs::metadata(path).ok())
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        state.metadata_or_insert_with(|| SemanticCoverageMetadata {
            runtime_trace_offset,
            state_trace_offset,
            ..SemanticCoverageMetadata::default()
        });
        Ok(())
    }
}

impl<C, EM, I, O, OT, S> Feedback<EM, I, OT, S> for SemanticIrCoverageFeedback<C, O>
where
    C: AsRef<O>,
    O: MapObserver<Entry = u8> + for<'it> AsIter<'it, Item = u8>,
    OT: MatchName,
    S: HasMetadata,
{
    fn is_interesting(
        &mut self,
        state: &mut S,
        _manager: &mut EM,
        _input: &I,
        observers: &OT,
        _exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        let observer = observers
            .get(&self.map_ref)
            .expect("semantic MapObserver not found")
            .as_ref();
        let initial = observer.initial();
        let hits = observer
            .as_iter()
            .map(|value| *value)
            .enumerate()
            .filter(|(_, value)| *value != initial)
            .filter_map(|(runtime_id, _)| {
                self.entries
                    .get(runtime_id)
                    .and_then(Option::as_ref)
            })
            .collect::<Vec<_>>();
        let (runtime_offset, state_offset) = state
            .metadata_map()
            .get::<SemanticCoverageMetadata>()
            .map(|metadata| (metadata.runtime_trace_offset, metadata.state_trace_offset))
            .unwrap_or_default();
        let telemetry = self.telemetry_ref.as_ref().map(|handle| {
            let observer = observers.get(handle).expect("semantic telemetry observer missing").as_ref();
            read_semantic_telemetry(observer.as_iter().map(|value| *value))
        });
        // Text traces remain a legacy debugging fallback; the default path
        // reads only shared memory and never opens a per-execution trace file.
        let (next_runtime_offset, cycles_by_runtime) = if let Some(telemetry) = &telemetry {
            (runtime_offset, telemetry.cycles_by_runtime.clone())
        } else {
            self.runtime_trace_path.as_deref()
                .map(|path| read_runtime_cycle_trace_window(path, runtime_offset))
                .unwrap_or((runtime_offset, BTreeMap::new()))
        };
        let (next_state_offset, state_signatures) = if let Some(telemetry) = &telemetry {
            (state_offset, telemetry.state_signatures.clone())
        } else {
            self.state_trace_path.as_deref()
                .map(|path| read_state_signature_window(path, state_offset))
                .unwrap_or((state_offset, Vec::new()))
        };
        let mut ordered_trace_complete = telemetry.as_ref().is_some_and(|item| item.ordered_complete);
        let ordered_target_ids = telemetry.as_ref().map(|telemetry| {
            telemetry.ordered_runtime_ids.iter().filter_map(|id| {
                let entry = self.entries.get(*id as usize).and_then(Option::as_ref);
                if entry.is_none() { ordered_trace_complete = false; }
                entry.map(|entry| entry.stable_id.clone())
            }).collect::<Vec<_>>()
        }).unwrap_or_default();

        let mut trace_events = Vec::new();
        let mut hit_target_ids = Vec::new();
        let mut interesting = {
            let semantic = state.metadata_or_insert_with(SemanticCoverageMetadata::default);
            semantic.runtime_trace_offset = next_runtime_offset;
            semantic.state_trace_offset = next_state_offset;
            semantic.last_covered_target_ids.clear();
            semantic.last_new_branch_sides.clear();
            semantic.last_new_state_transitions.clear();
            semantic.last_covered_target = None;
            semantic.last_covered_targets_by_cycle.clear();
            semantic.last_cycle_ids.clear();
            semantic.last_ordered_target_ids.clone_from(&ordered_target_ids);
            semantic.last_ordered_trace_complete = ordered_trace_complete;
            semantic.last_scan_count = telemetry.as_ref().map_or(0, |item| item.scan_count);
            semantic.last_telemetry_truncated = telemetry.as_ref().is_some_and(|item| item.truncated);
            if telemetry.is_some() {
                // The ordinal records real calls, independent of cycle labels in
                // the input. Retention is bounded even for an invalid harness.
                semantic.last_cycle_ids.extend(0..(semantic.last_scan_count as usize).min(10_000));
            }
            semantic.last_new_state_signatures.clear();
            semantic.last_state_signatures = state_signatures.into_iter()
                .take(MAX_EXECUTION_STATE_SIGNATURES).collect();
            for signature in &semantic.last_state_signatures {
                if semantic.seen_state_signatures.len() < MAX_GLOBAL_STATE_SIGNATURES
                    && semantic.seen_state_signatures.insert(compact_content_hash(signature.as_bytes()))
                {
                    semantic.last_new_state_signatures.push(signature.clone());
                }
            }

            for entry in hits {
                let target_type = if entry.kind == "cycle"
                    || matches!(entry.role.as_deref(), Some("cycle_entry" | "cycle_exit"))
                {
                    "state"
                } else {
                    "branch"
                };
                let cycles = cycles_by_runtime
                    .get(&entry.runtime_id)
                    .cloned()
                    .unwrap_or_default();
                for cycle in &cycles {
                    semantic
                        .last_covered_targets_by_cycle
                        .entry(cycle.to_string())
                        .or_default()
                        .push(entry.stable_id.clone());
                    if let Ok(cycle) = usize::try_from(*cycle) {
                        semantic.last_cycle_ids.push(cycle);
                    }
                }
                semantic.last_covered_target_ids.push(entry.stable_id.clone());
                let new_target = semantic.seen_targets.insert(entry.stable_id.clone());
                if new_target {
                    if target_type == "state" {
                        semantic
                            .last_new_state_transitions
                            .push(entry.stable_id.clone());
                    } else {
                        semantic.last_new_branch_sides.push(entry.stable_id.clone());
                    }
                    semantic.last_covered_target = Some(entry.stable_id.clone());
                }
                hit_target_ids.push(entry.stable_id.clone());
                if new_target && self.trace_path.is_some() {
                    trace_events.push(serde_json::json!({
                        "probe": "semantic_ir",
                        "runtime_id": entry.runtime_id,
                        "target_id": entry.stable_id,
                        "target_type": target_type,
                        "cycle_id": cycles.last(),
                        "cycles": cycles,
                    }));
                }
            }
            semantic.last_cycle_ids.sort_unstable();
            semantic.last_cycle_ids.dedup();
            semantic.last_covered_target_ids.sort();
            semantic.last_covered_target_ids.dedup();
            for targets in semantic.last_covered_targets_by_cycle.values_mut() {
                targets.sort();
                targets.dedup();
            }
            semantic.last_interesting = !semantic.last_new_branch_sides.is_empty()
                || !semantic.last_new_state_transitions.is_empty()
                || !semantic.last_new_state_signatures.is_empty();
            semantic.last_interesting
        };
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            interesting |= runtime.record_execution(hit_target_ids, ordered_target_ids, ordered_trace_complete);
        }
        state.metadata_or_insert_with(SemanticCoverageMetadata::default).last_interesting = interesting;
        if !trace_events.is_empty() {
            if let Some(path) = self.trace_path.as_deref() {
                for event in &trace_events { append_json_value(path, event)?; }
            }
        }
        if let Some(path) = self.trace_path.as_deref() {
            if let Some(semantic) = state.metadata_map().get::<SemanticCoverageMetadata>() {
                for signature in &semantic.last_new_state_signatures {
                    append_json_value(path, &serde_json::json!({
                        "probe": "semantic_state", "state_signature": signature,
                        "scan_count": semantic.last_scan_count,
                        "cycle_id": semantic.last_scan_count.checked_sub(1),
                    }))?;
                }
            }
        }
        Ok(interesting)
    }
}

// Collect execution evidence before objective classification: LibAFL does not
// run ordinary feedback when an input is a solution. This wrapper deliberately
// never turns evidence collection itself into an objective.
#[derive(Debug)]
struct SemanticEvidenceCollector<F>(F);

impl<F: Named> Named for SemanticEvidenceCollector<F> {
    fn name(&self) -> &Cow<'static, str> { self.0.name() }
}

impl<F: StateInitializer<S>, S> StateInitializer<S> for SemanticEvidenceCollector<F> {
    fn init_state(&mut self, state: &mut S) -> Result<(), Error> { self.0.init_state(state) }
}

impl<F: Feedback<EM, I, OT, S>, EM, I, OT, S> Feedback<EM, I, OT, S>
    for SemanticEvidenceCollector<F>
{
    fn is_interesting(&mut self, state: &mut S, manager: &mut EM, input: &I,
        observers: &OT, exit_kind: &ExitKind) -> Result<bool, Error>
    {
        self.0.is_interesting(state, manager, input, observers, exit_kind)?;
        Ok(false)
    }
}

#[derive(Debug)]
struct SemanticNoveltyFeedback;
impl Named for SemanticNoveltyFeedback {
    fn name(&self) -> &Cow<'static, str> {
        static NAME: Cow<'static, str> = Cow::Borrowed("SemanticNoveltyFeedback");
        &NAME
    }
}
impl<S> StateInitializer<S> for SemanticNoveltyFeedback {}
impl<EM, I, OT, S: HasMetadata> Feedback<EM, I, OT, S> for SemanticNoveltyFeedback {
    fn is_interesting(&mut self, state: &mut S, _manager: &mut EM, _input: &I,
        _observers: &OT, _exit_kind: &ExitKind) -> Result<bool, Error>
    {
        Ok(state.metadata_map().get::<SemanticCoverageMetadata>()
            .is_some_and(|metadata| metadata.last_interesting))
    }
}

fn content_hash(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn seed_profile_from_metadata(metadata: &StSeedMetadata) -> SeedProfile {
    SeedProfile {
        content_hash: metadata.content_hash.clone(),
        covered_target_ids: if metadata.covered_target_ids.is_empty() {
            metadata.semantic_coverage.keys().cloned().collect()
        } else {
            metadata.covered_target_ids.clone()
        },
        ordered_target_ids: metadata.ordered_target_ids.clone(),
        ordered_trace_complete: metadata.ordered_trace_complete,
        covered_targets_by_cycle: metadata.covered_targets_by_cycle.clone(),
        cycle_ids: metadata.cycle_ids.clone(),
        state_signatures: metadata.state_signatures.clone(),
        target_affinity: metadata.target_affinity.clone(),
        parent_id: metadata.parent_id.clone(),
    }
}

fn unix_time_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn load_bootstrap_metadata(path: Option<&Path>) -> HashMap<String, StSeedMetadata> {
    let Some(path) = path else {
        return HashMap::new();
    };
    let Ok(contents) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    let records = if contents.trim_start().starts_with('[') {
        match serde_json::from_str::<Vec<StSeedMetadata>>(&contents) {
            Ok(records) => records,
            Err(error) => {
                eprintln!(
                    "[metadata] unable to parse bootstrap metadata {}: {}; using testcase defaults",
                    path.display(),
                    error
                );
                return HashMap::new();
            }
        }
    } else {
        contents
            .lines()
            .filter(|line| !line.trim().is_empty())
            .filter_map(|line| match serde_json::from_str::<StSeedMetadata>(line) {
                Ok(record) => Some(record),
                Err(error) => {
                    eprintln!(
                        "[metadata] skipping malformed bootstrap metadata line in {}: {}",
                        path.display(),
                        error
                    );
                    None
                }
            })
            .collect()
    };
    records
        .into_iter()
        .map(|record| (record.content_hash.clone(), record))
        .collect()
}

fn target_context<S>(state: &S) -> TargetContext
where
    S: HasMetadata,
{
    state
        .metadata_map()
        .get::<TargetContext>()
        .cloned()
        .unwrap_or_default()
}

fn semantic_target_context(selection: &ActiveSelection) -> TargetContext {
    let control = selection.control_obligation.as_ref();
    let transition_seed_fields = selection
        .state_transfer_chains
        .iter()
        .filter_map(|transfer| {
            let mut reads = transfer.reads.iter().map(String::as_str).collect::<Vec<_>>();
            for operation in &transfer.operations {
                for key in ["reads", "value_expression"] {
                    let source = if key == "reads" {
                        Some(operation)
                    } else {
                        operation.get(key)
                    };
                    reads.extend(
                        source
                            .and_then(|value| value.get("reads"))
                            .and_then(|value| value.as_array())
                            .into_iter()
                            .flatten()
                            .filter_map(|value| value.as_str()),
                    );
                }
            }
            let fields = selection
                .controllable_seed_fields
                .iter()
                .filter(|field| {
                    field.seed_field.is_some()
                        && reads.iter().any(|read| field.symbol.eq_ignore_ascii_case(read))
                })
                .cloned()
                .collect::<Vec<_>>();
            (!fields.is_empty()).then_some(fields)
        })
        .collect();
    let mut value_definitions = BTreeMap::new();
    let mut ambiguous_definitions = HashSet::new();
    for transfer in &selection.state_transfer_chains {
        let mut assigned = HashSet::new();
        let mut unknown_effect = false;
        for operation in &transfer.operations {
            unknown_effect |= matches!(operation.get("kind").and_then(|value| value.as_str()), Some("call" | "opaque"));
            let Some(symbol) = operation.get("target_symbol").and_then(|value| value.as_str()) else { continue; };
            let key = symbol.to_ascii_uppercase();
            assigned.insert(key.clone());
            if ambiguous_definitions.contains(&key) { continue; }
            // Keep a null sentinel: dropping the key could make a modified
            // input look like an untouched invocation argument again.
            if value_definitions.contains_key(&key) || operation.get("value_expression").is_none_or(|value| value.is_null()) {
                value_definitions.insert(key.clone(), serde_json::Value::Null);
                ambiguous_definitions.insert(key);
            } else if let Some(expression) = operation.get("value_expression") {
                value_definitions.insert(key, expression.clone());
            }
        }
        // Calls expose writes in the transfer summary without target_symbol
        // assignments. Those values cannot be reconstructed by this mutator.
        for symbol in &transfer.writes {
            let key = symbol.to_ascii_uppercase();
            if unknown_effect || !assigned.contains(&key) {
                value_definitions.insert(key.clone(), serde_json::Value::Null);
                ambiguous_definitions.insert(key);
            }
        }
    }
    TargetContext {
        target_id: Some(selection.guide_target_id.clone()),
        target_type: control
            .map(|obligation| obligation.kind.clone())
            .or_else(|| selection.target_kind.clone())
            .or_else(|| Some(selection.task_kind.clone())),
        control_kind: control.map(|obligation| obligation.kind.clone()),
        side: control.and_then(|obligation| obligation.role.clone()),
        semantic_task_id: Some(selection.task_id.clone()),
        terminal_target_id: Some(selection.terminal_target_id.clone()),
        active_path: selection.active_path.clone(),
        remaining_obligations: selection.remaining_obligations.clone(),
        goal_expression_id: selection.goal_expression_id.clone(),
        goal_expression: selection.goal_expression.clone(),
        value_definitions,
        transition_seed_fields,
        controllable_seed_fields: selection.controllable_seed_fields.clone(),
        hazard_prerequisites: selection.hazard_prerequisites.clone(),
        modeling_status: Some(selection.modeling_status.clone()),
        task_kind: Some(selection.task_kind.clone()),
        semantic_priority: Some(selection.priority),
        hazard_kind: selection.hazard_kind.clone(),
        cycle_hint: selection.cycle_hint,
        ..TargetContext::default()
    }
}

fn persist_bootstrap_metadata(path: Option<&Path>, metadata: StSeedMetadata) -> Result<(), Error> {
    let Some(path) = path else {
        return Ok(());
    };
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    serde_json::to_writer(&mut file, &metadata).map_err(|error| {
        Error::serialize(format!(
            "unable to serialize finding seed metadata: {error}"
        ))
    })?;
    file.write_all(b"\n")?;
    Ok(())
}

fn source_context<S>(state: &S) -> SeedSource
where
    S: HasMetadata,
{
    state
        .metadata_map()
        .get::<SeedSourceContext>()
        .map(|context| context.source.clone())
        .unwrap_or(SeedSource::Fuzzing)
}

fn seed_source_label(source: &SeedSource) -> &'static str {
    match source {
        SeedSource::Initial => "initial",
        SeedSource::SemanticGeneration => "semantic_generation",
        SeedSource::Fuzzing => "fuzzing",
    }
}

fn exit_kind_label(exit_kind: &ExitKind) -> &'static str {
    match exit_kind {
        ExitKind::Ok => "ok",
        ExitKind::Crash => "crash",
        ExitKind::Oom => "oom",
        ExitKind::Timeout => "timeout",
        ExitKind::Diff { .. } => "diff",
    }
}

fn exit_kind_is_finding(exit_kind: &ExitKind) -> bool {
    matches!(
        exit_kind,
        ExitKind::Crash | ExitKind::Oom | ExitKind::Timeout
    )
}

fn finding_severity(kind: &str) -> &'static str {
    match kind {
        "crash" | "oom" | "asan" | "ubsan" | "sanitizer" => "High",
        "timeout" => "Medium",
        _ => "Medium",
    }
}

fn parent_hash<S>(state: &S) -> Option<String>
where
    S: HasCorpus<BytesInput>,
{
    state.corpus().current().and_then(|id| {
        state
            .corpus()
            .get(id)
            .ok()
            .and_then(|cell| {
                cell.borrow()
                    .metadata_map()
                    .get::<StSeedMetadata>()
                    .cloned()
            })
            .map(|metadata| metadata.content_hash)
    })
}

const INPUT_HASH_CAPACITY: usize = 65_536;

fn compact_content_hash(bytes: &[u8]) -> u64 {
    use std::hash::Hasher;
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    hasher.write(bytes);
    hasher.finish()
}

#[derive(Debug)]
struct ContentHashInputFilter {
    seen_hashes: HashSet<u64>,
    insertion_order: std::collections::VecDeque<u64>,
}

impl Default for ContentHashInputFilter {
    fn default() -> Self {
        Self {
            seen_hashes: HashSet::with_capacity(INPUT_HASH_CAPACITY),
            insertion_order: std::collections::VecDeque::with_capacity(INPUT_HASH_CAPACITY),
        }
    }
}

impl ContentHashInputFilter {
    fn admit_hash(&mut self, hash: u64) -> bool {
        if self.seen_hashes.contains(&hash) { return false; }
        if self.seen_hashes.len() == INPUT_HASH_CAPACITY {
            if let Some(oldest) = self.insertion_order.pop_front() {
                self.seen_hashes.remove(&oldest);
            }
        }
        self.seen_hashes.insert(hash);
        self.insertion_order.push_back(hash);
        true
    }
}

impl<EM, I, S> InputFilter<EM, I, S> for ContentHashInputFilter
where
    I: AsRef<Vec<u8>>,
{
    fn should_execute(
        &mut self,
        input: &I,
        _state: &mut S,
        _manager: &mut EM,
    ) -> Result<bool, Error> {
        let _timer = PhaseTimer::start(Phase::InputFilter);
        Ok(self.admit_hash(compact_content_hash(input.as_ref())))
    }
}

#[cfg(test)]
mod semantic_objective_regressions {
    use super::*;

    fn candidate_table() -> RuntimeIdTable {
        let entries = [
            (0, "alias", "hazard_violation", "exact"),
            (2, "alias", "hazard_violation", "exact"),
            (3, "property", "property_violation", "exact"),
            (4, "approximate", "hazard_violation", "approximate"),
            (5, "branch", "branch", "exact"),
            (100, "outside-map", "hazard_violation", "exact"),
        ]
        .into_iter()
        .map(|(runtime_id, stable_id, kind, modeling)| RuntimeTargetEntry {
            runtime_id,
            stable_id: stable_id.into(),
            pou: "P".into(),
            node_id: "n".into(),
            semantic_edge_ids: vec![],
            kind: kind.into(),
            role: None,
            hazard: Some("bounds".into()),
            source: None,
            modeling: modeling.into(),
        })
        .collect();
        RuntimeIdTable {
            schema_version: "semantist.runtime-ids/1.0.0".into(),
            entries,
        }
    }

    #[test]
    fn newly_reached_violations_are_reported_once_with_their_target() {
        let mut map = vec![0u8; 6];
        let observer = unsafe { StdMapObserver::new("pending-objective", &mut map) };
        let handle = observer.handle();
        let pending = PendingSemanticFindings::default();
        let mut objective = SemanticViolationObjective::new(&observer, &candidate_table())
            .with_pending_findings(pending.clone());
        let mut observers = tuple_list!(observer);
        // Runtime IDs 0 and 2 alias one violation; 3 is a property violation.
        observers.get_mut(&handle).unwrap().as_slice_mut().copy_from_slice(&[1, 0, 1, 1, 0, 0]);
        assert!(objective.is_interesting(&mut (), &mut (), &(), &observers, &ExitKind::Ok).unwrap());
        let mut reported = pending.borrow().iter().map(|entry| entry.stable_id.clone()).collect::<Vec<_>>();
        reported.sort();
        assert_eq!(reported, vec!["alias", "property"]);
        pending.borrow_mut().clear();
        assert!(!objective.is_interesting(&mut (), &mut (), &(), &observers, &ExitKind::Ok).unwrap());
        assert!(pending.borrow().is_empty());
    }

    // Preserve the old full-map algorithm as a behavioral oracle. Compare
    // sequences, not just the first result: all candidates hit by one execution
    // must be marked, and runtime IDs sharing one semantic finding must dedup.
    fn dense_reference(
        map: &[u8],
        candidate_slots: &HashMap<u32, usize>,
        seen: &mut [bool],
    ) -> bool {
        let mut novel = false;
        for (runtime_id, value) in map.iter().enumerate() {
            if *value == 0 {
                continue;
            }
            if let Some(slot) = candidate_slots.get(&(runtime_id as u32)) {
                novel |= !seen[*slot];
                seen[*slot] = true;
            }
        }
        novel
    }

    #[test]
    fn sparse_violation_check_matches_dense_reference_across_execution_sequences() {
        for map_len in [0, 1, 4, 6] {
            let all_hits = (1usize << map_len) - 1;
            for first_hits in 0..=all_hits {
                let mut map = vec![0u8; map_len];
                let observer = unsafe { StdMapObserver::new("sparse-objective", &mut map) };
                let map_handle = observer.handle();
                let mut objective = SemanticViolationObjective::new(&observer, &candidate_table());
                let candidate_slots = objective.candidate_slots.clone();
                let mut expected_seen = vec![false; objective.seen_candidates.len()];
                let mut observers = tuple_list!(observer);

                for hits in [first_hits, first_hits ^ all_hits, first_hits, all_hits, 0] {
                    let values = (0..map_len)
                        .map(|index| {
                            if hits & (1 << index) == 0 { 0 }
                            else if index % 2 == 0 { 1 }
                            else { 255 }
                        })
                        .collect::<Vec<u8>>();
                    observers.get_mut(&map_handle).unwrap().as_slice_mut().copy_from_slice(&values);
                    let expected = dense_reference(&values, &candidate_slots, &mut expected_seen);
                    let actual = objective
                        .is_interesting(&mut (), &mut (), &(), &observers, &ExitKind::Ok)
                        .unwrap();
                    assert_eq!(actual, expected, "map_len={map_len}, first={first_hits}, hits={hits}");
                    assert_eq!(objective.seen_candidates, expected_seen);
                }
            }
        }
    }
}
