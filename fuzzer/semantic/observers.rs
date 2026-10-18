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
    // (runtime ID, cycle) per event in event order, for cycles that fit i32.
    cycle_events: Vec<(u32, i32)>,
    state_signatures: Vec<StateSignature>,
    ordered_complete: bool,
    truncated: bool,
}

impl SemanticTelemetry {
    /// The cycles one probe fired in, without consecutive repeats.
    fn cycles_of(&self, runtime_id: u32) -> Vec<i32> {
        let mut cycles = Vec::new();
        for (id, cycle) in &self.cycle_events {
            if *id == runtime_id && cycles.last() != Some(cycle) {
                cycles.push(*cycle);
            }
        }
        cycles
    }
}

// Reads only the header and the event and state slots it counts; bytes past
// the end of a short buffer read as zero.
fn read_semantic_telemetry(bytes: &[u8]) -> SemanticTelemetry {
    let u32_at = |offset: usize| {
        bytes.get(offset..offset + 4).map_or(0, |word| u32::from_ne_bytes(word.try_into().unwrap()))
    };
    let u64_at = |offset: usize| {
        bytes.get(offset..offset + 8).map_or(0, |word| u64::from_ne_bytes(word.try_into().unwrap()))
    };
    let scan_count = u32_at(0);
    let event_count = u32_at(4) as usize;
    let state_count = u32_at(8) as usize;
    let flags = u32_at(12);
    let mut result = SemanticTelemetry {
        scan_count,
        ordered_complete: flags & 4 != 0 && flags & 1 == 0 && event_count <= TELEMETRY_EVENT_CAPACITY,
        truncated: flags & 3 != 0 || event_count > TELEMETRY_EVENT_CAPACITY || state_count > TELEMETRY_STATE_CAPACITY,
        ..SemanticTelemetry::default()
    };
    for event in 0..event_count.min(TELEMETRY_EVENT_CAPACITY) {
        let runtime_id = u32_at(16 + event * 8);
        let cycle = u32_at(20 + event * 8);
        result.ordered_runtime_ids.push(runtime_id);
        if let Ok(cycle) = i32::try_from(cycle) {
            result.cycle_events.push((runtime_id, cycle));
        }
    }
    if state_count > 0 {
        let state_base = 16 + TELEMETRY_EVENT_CAPACITY * 8;
        let mut seen = HashSet::<(u8, u64), IdBuildHasher>::default();
        for state in 0..state_count.min(TELEMETRY_STATE_CAPACITY) {
            let before = u64_at(state_base + state * 16);
            let after = u64_at(state_base + state * 16 + 8);
            // No cycle ordinal in the signature: repeating an unchanged state
            // must not create fresh feedback merely by lengthening an input.
            for (kind, signature) in [(0u8, before), (0, after), (1, before.rotate_left(17) ^ after)] {
                if result.state_signatures.len() < MAX_EXECUTION_STATE_SIGNATURES && seen.insert((kind, signature)) {
                    result.state_signatures.push(if kind == 0 {
                        StateSignature::State(signature)
                    } else {
                        StateSignature::Transition(signature)
                    });
                }
            }
        }
    }
    result
}

// Telemetry written by the target's semantic runtime. Readers stop at the
// event and state counts in the 16-byte header, so only the header is
// cleared before an execution, not the whole ~41 KB region.
#[derive(Debug, Deserialize, Serialize)]
struct TelemetryObserver<'a> {
    name: Cow<'static, str>,
    bytes: libafl_bolts::ownedref::OwnedMutSlice<'a, u8>,
}

impl TelemetryObserver<'static> {
    /// # Safety
    /// `bytes` (shared memory in the engine) must stay valid, and only the
    /// target may write it, while the observer exists.
    unsafe fn new(name: &'static str, bytes: &mut [u8]) -> Self {
        Self {
            name: Cow::Borrowed(name),
            bytes: unsafe { libafl_bolts::ownedref::OwnedMutSlice::from_raw_parts_mut(bytes.as_mut_ptr(), bytes.len()) },
        }
    }

}

impl TelemetryObserver<'_> {
    fn as_slice(&self) -> &[u8] {
        &self.bytes
    }
}

impl Named for TelemetryObserver<'_> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<I, S> libafl::observers::Observer<I, S> for TelemetryObserver<'_> {
    fn pre_exec(&mut self, _state: &mut S, _input: &I) -> Result<(), Error> {
        let header = self.bytes.len().min(16);
        self.bytes.as_slice_mut()[..header].fill(0);
        Ok(())
    }
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

// Stable target IDs of a runtime table, sorted and unique. Their positions are
// the dense target indices shared by the feedback and the semantic runtime.
fn stable_target_names(table: &RuntimeIdTable) -> Vec<String> {
    let mut names = table.entries.iter().map(|entry| entry.stable_id.clone()).collect::<Vec<_>>();
    names.sort();
    names.dedup();
    names
}

#[derive(Debug)]
struct SemanticIrCoverageFeedback<C, O> {
    name: Cow<'static, str>,
    map_ref: Handle<C>,
    telemetry_ref: Option<Handle<TelemetryObserver<'static>>>,
    // Per runtime ID: dense target index and whether the probe is a state
    // probe; u32::MAX for runtime IDs the table does not define.
    target_index: Vec<u32>,
    state_probe: Vec<bool>,
    target_names: Vec<String>,
    // Scratch buffers reused across executions.
    hits: Vec<u32>,
    hit_mask: Vec<bool>,
    ordered: Vec<u32>,
    covered: Vec<u32>,
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
        let target_names = stable_target_names(&table);
        let mut entries = vec![None; max_id.saturating_add(1)];
        for entry in table.entries {
            let index = entry.runtime_id as usize;
            if index >= entries.len() {
                entries.resize(index + 1, None);
            }
            entries[index] = Some(entry);
        }
        let target_index = entries.iter().map(|entry: &Option<RuntimeTargetEntry>| {
            entry.as_ref().map_or(u32::MAX, |entry| {
                target_names.binary_search(&entry.stable_id).map_or(u32::MAX, |index| index as u32)
            })
        }).collect();
        let state_probe = entries.iter().map(|entry: &Option<RuntimeTargetEntry>| {
            entry.as_ref().is_some_and(|entry| {
                entry.kind == "cycle" || matches!(entry.role.as_deref(), Some("cycle_entry" | "cycle_exit"))
            })
        }).collect();
        Self {
            name: Cow::Borrowed("SemanticIrCoverageFeedback"),
            map_ref: map_observer.handle(),
            telemetry_ref: None,
            hit_mask: vec![false; entries.len()],
            target_index,
            state_probe,
            target_names,
            hits: Vec::new(),
            ordered: Vec::new(),
            covered: Vec::new(),
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

    fn with_telemetry(mut self, observer: &TelemetryObserver<'static>) -> Self {
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
        let semantic = state.metadata_or_insert_with(|| SemanticCoverageMetadata {
            runtime_trace_offset,
            state_trace_offset,
            ..SemanticCoverageMetadata::default()
        });
        semantic.seen_targets.resize(self.target_names.len(), false);
        semantic.target_names.clone_from(&self.target_names);
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
        for runtime_id in self.hits.drain(..) {
            self.hit_mask[runtime_id as usize] = false;
        }
        for (runtime_id, value) in observer.as_iter().enumerate() {
            if *value != initial && self.target_index.get(runtime_id).is_some_and(|index| *index != u32::MAX) {
                self.hits.push(runtime_id as u32);
                self.hit_mask[runtime_id] = true;
            }
        }
        let (runtime_offset, state_offset) = state
            .metadata_map()
            .get::<SemanticCoverageMetadata>()
            .map(|metadata| (metadata.runtime_trace_offset, metadata.state_trace_offset))
            .unwrap_or_default();
        let telemetry = self.telemetry_ref.as_ref().map(|handle| {
            read_semantic_telemetry(observers.get(handle).expect("semantic telemetry observer missing").as_slice())
        });
        // Text traces remain a legacy debugging fallback; the default path
        // reads only shared memory and never opens a per-execution trace file.
        let (next_runtime_offset, legacy_cycles) = if telemetry.is_some() {
            (runtime_offset, BTreeMap::new())
        } else {
            self.runtime_trace_path.as_deref()
                .map(|path| read_runtime_cycle_trace_window(path, runtime_offset))
                .unwrap_or((runtime_offset, BTreeMap::new()))
        };
        let (next_state_offset, legacy_states) = if telemetry.is_some() {
            (state_offset, Vec::new())
        } else {
            self.state_trace_path.as_deref()
                .map(|path| read_state_signature_window(path, state_offset))
                .unwrap_or((state_offset, Vec::new()))
        };
        let mut ordered_trace_complete = telemetry.as_ref().is_some_and(|item| item.ordered_complete);
        self.ordered.clear();
        if let Some(telemetry) = &telemetry {
            for runtime_id in &telemetry.ordered_runtime_ids {
                match self.target_index.get(*runtime_id as usize) {
                    Some(index) if *index != u32::MAX => self.ordered.push(*index),
                    _ => ordered_trace_complete = false,
                }
            }
        }

        let mut trace_events = Vec::new();
        let mut interesting = {
            let semantic = state.metadata_or_insert_with(SemanticCoverageMetadata::default);
            semantic.runtime_trace_offset = next_runtime_offset;
            semantic.state_trace_offset = next_state_offset;
            semantic.last_covered.clear();
            semantic.last_new_branches.clear();
            semantic.last_new_transitions.clear();
            semantic.last_covered_index = None;
            semantic.last_cycle_targets.clear();
            semantic.last_extra_cycles.clear();
            semantic.last_ordered.clone_from(&self.ordered);
            semantic.last_ordered_trace_complete = ordered_trace_complete;
            semantic.last_scan_count = telemetry.as_ref().map_or(0, |item| item.scan_count);
            semantic.total_scan_cycles = semantic.total_scan_cycles.saturating_add(u64::from(semantic.last_scan_count));
            semantic.last_telemetry_truncated = telemetry.as_ref().is_some_and(|item| item.truncated);
            // The ordinal records real calls, independent of cycle labels in
            // the input. Retention is bounded even for an invalid harness.
            semantic.last_cycle_count = if telemetry.is_some() {
                (semantic.last_scan_count as usize).min(10_000)
            } else {
                0
            };
            semantic.last_new_states.clear();
            semantic.last_states.clear();
            let signatures = telemetry.as_ref().map_or_else(
                || legacy_states.into_iter().map(StateSignature::Legacy).collect(),
                |item| item.state_signatures.clone(),
            );
            for signature in signatures.into_iter().take(MAX_EXECUTION_STATE_SIGNATURES) {
                if semantic.seen_state_signatures.len() < MAX_GLOBAL_STATE_SIGNATURES
                    && semantic.seen_state_signatures.insert(signature.clone())
                {
                    semantic.last_new_states.push(signature.clone());
                }
                semantic.last_states.push(signature);
            }

            // (cycle, runtime ID) of every probe hit this execution.
            let cycle_count = semantic.last_cycle_count;
            let record_cycle = |semantic: &mut SemanticCoverageMetadata, cycle: i32, target: u32| {
                semantic.last_cycle_targets.push((cycle, target));
                if let Ok(cycle) = usize::try_from(cycle) {
                    if cycle >= cycle_count {
                        semantic.last_extra_cycles.push(cycle);
                    }
                }
            };
            if let Some(telemetry) = &telemetry {
                for (runtime_id, cycle) in &telemetry.cycle_events {
                    if self.hit_mask.get(*runtime_id as usize).copied().unwrap_or(false) {
                        record_cycle(semantic, *cycle, self.target_index[*runtime_id as usize]);
                    }
                }
            } else {
                for runtime_id in &self.hits {
                    for cycle in legacy_cycles.get(runtime_id).into_iter().flatten() {
                        record_cycle(semantic, *cycle, self.target_index[*runtime_id as usize]);
                    }
                }
            }
            if semantic.seen_targets.len() < self.target_names.len() {
                semantic.seen_targets.resize(self.target_names.len(), false);
            }
            for runtime_id in &self.hits {
                let target = self.target_index[*runtime_id as usize];
                semantic.last_covered.push(target);
                let new_target = !semantic.seen_targets[target as usize];
                if new_target {
                    semantic.seen_targets[target as usize] = true;
                    if self.state_probe[*runtime_id as usize] {
                        semantic.last_new_transitions.push(target);
                    } else {
                        semantic.last_new_branches.push(target);
                    }
                    semantic.last_covered_index = Some(target);
                }
                if new_target && self.trace_path.is_some() {
                    let cycles = telemetry.as_ref().map_or_else(
                        || legacy_cycles.get(runtime_id).cloned().unwrap_or_default(),
                        |item| item.cycles_of(*runtime_id),
                    );
                    trace_events.push(serde_json::json!({
                        "probe": "semantic_ir",
                        "runtime_id": runtime_id,
                        "target_id": self.target_names[target as usize],
                        "target_type": if self.state_probe[*runtime_id as usize] { "state" } else { "branch" },
                        "cycle_id": cycles.last(),
                        "cycles": cycles,
                    }));
                }
            }
            semantic.last_extra_cycles.sort_unstable();
            semantic.last_extra_cycles.dedup();
            semantic.last_cycle_targets.sort_unstable();
            semantic.last_cycle_targets.dedup();
            semantic.last_covered.sort_unstable();
            semantic.last_covered.dedup();
            self.covered.clone_from(&semantic.last_covered);
            semantic.last_interesting = !semantic.last_new_branches.is_empty()
                || !semantic.last_new_transitions.is_empty()
                || !semantic.last_new_states.is_empty();
            semantic.last_interesting
        };
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            interesting |= runtime.record_indexed_execution(&self.covered, &self.ordered, ordered_trace_complete);
        }
        state.metadata_or_insert_with(SemanticCoverageMetadata::default).last_interesting = interesting;
        if !trace_events.is_empty() {
            if let Some(path) = self.trace_path.as_deref() {
                for event in &trace_events { append_json_value(path, event)?; }
            }
        }
        if let Some(path) = self.trace_path.as_deref() {
            if let Some(semantic) = state.metadata_map().get::<SemanticCoverageMetadata>() {
                for signature in &semantic.last_new_states {
                    append_json_value(path, &serde_json::json!({
                        "probe": "semantic_state", "state_signature": signature.to_string(),
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

fn seed_profile_from_metadata(seed_id: CorpusId, metadata: &StSeedMetadata) -> SeedProfile {
    SeedProfile {
        seed_id,
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
        parent_id: metadata.parent,
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
    append_json_line(path, &metadata, "finding seed metadata")
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

// The current testcase's content hash, for a finding record.
fn parent_hash<S>(state: &S) -> Option<String>
where
    S: HasCorpus<BytesInput>,
{
    let id = state.corpus().current().as_ref().copied()?;
    let mut testcase = state.corpus().get(id).ok()?.borrow_mut();
    let input = testcase.load_input(state.corpus()).ok()?;
    Some(content_hash(input.as_ref()))
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
