#[derive(Debug)]
struct SemanticViolationObjective<C, O> {
    name: Cow<'static, str>,
    map_ref: Handle<C>,
    objective_runtime_ids: HashSet<u32>,
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
        let objective_runtime_ids = table
            .entries
            .iter()
            .filter(|entry| is_exact_semantic_violation(entry))
            .map(|entry| entry.runtime_id)
            .collect();
        Self {
            name: Cow::Borrowed("SemanticViolationObjective"),
            map_ref: map_observer.handle(),
            objective_runtime_ids,
            phantom: std::marker::PhantomData,
        }
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
    O: MapObserver<Entry = u8> + for<'it> AsIter<'it, Item = u8>,
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
        Ok(observer
            .as_iter()
            .map(|value| *value)
            .enumerate()
            .filter(|(_, value)| *value != initial)
            .any(|(runtime_id, _)| {
                u32::try_from(runtime_id)
                    .ok()
                    .is_some_and(|runtime_id| self.objective_runtime_ids.contains(&runtime_id))
            }))
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
                    .cloned()
            })
            .collect::<Vec<_>>();
        let (runtime_offset, state_offset) = state
            .metadata_map()
            .get::<SemanticCoverageMetadata>()
            .map(|metadata| (metadata.runtime_trace_offset, metadata.state_trace_offset))
            .unwrap_or_default();
        let (next_runtime_offset, cycles_by_runtime) = self
            .runtime_trace_path
            .as_deref()
            .map(|path| read_runtime_cycle_trace_window(path, runtime_offset))
            .unwrap_or((runtime_offset, BTreeMap::new()));
        let (next_state_offset, state_signatures) = self
            .state_trace_path
            .as_deref()
            .map(|path| read_state_signature_window(path, state_offset))
            .unwrap_or((state_offset, Vec::new()));

        let mut trace_events = Vec::new();
        let mut hit_target_ids = Vec::new();
        let interesting = {
            let semantic = state.metadata_or_insert_with(SemanticCoverageMetadata::default);
            semantic.runtime_trace_offset = next_runtime_offset;
            semantic.state_trace_offset = next_state_offset;
            semantic.last_covered_target_ids.clear();
            semantic.last_new_branch_sides.clear();
            semantic.last_new_state_transitions.clear();
            semantic.last_covered_target = None;
            semantic.last_covered_targets_by_cycle.clear();
            semantic.last_cycle_ids.clear();
            semantic.last_state_signatures = state_signatures
                .into_iter()
                .filter(|signature| semantic.seen_state_signatures.insert(signature.clone()))
                .collect();

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
                if semantic.seen_targets.insert(entry.stable_id.clone()) {
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
                trace_events.push(serde_json::json!({
                    "runtime_id": entry.runtime_id,
                    "target_id": entry.stable_id,
                    "target_type": target_type,
                    "cycles": cycles,
                }));
            }
            semantic.last_cycle_ids.sort_unstable();
            semantic.last_cycle_ids.dedup();
            semantic.last_covered_target_ids.sort();
            semantic.last_covered_target_ids.dedup();
            for targets in semantic.last_covered_targets_by_cycle.values_mut() {
                targets.sort();
                targets.dedup();
            }
            !semantic.last_new_branch_sides.is_empty()
                || !semantic.last_new_state_transitions.is_empty()
                || !semantic.last_state_signatures.is_empty()
        };
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            runtime.record_coverage(hit_target_ids);
        }
        if !trace_events.is_empty() {
            if let Some(path) = self.trace_path.as_deref() {
                append_json_value(
                    path,
                    &serde_json::json!({
                        "probe": "semantic_ir_batch",
                        "hits": trace_events,
                    }),
                )?;
            }
        }
        Ok(interesting)
    }
}

#[derive(Debug)]
struct CoverageProgressFeedback<C, O> {
    name: Cow<'static, str>,
    map_name: Cow<'static, str>,
    map_ref: Handle<C>,
    phantom: std::marker::PhantomData<O>,
}

impl<C, O> CoverageProgressFeedback<C, O>
where
    C: Handled + Named,
{
    fn new(map_observer: &C) -> Self {
        Self {
            name: Cow::Borrowed("CoverageProgressFeedback"),
            map_name: map_observer.name().clone(),
            map_ref: map_observer.handle(),
            phantom: std::marker::PhantomData,
        }
    }
}

impl<C, O> Named for CoverageProgressFeedback<C, O> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<C, O, S> StateInitializer<S> for CoverageProgressFeedback<C, O>
where
    S: HasMetadata,
{
    fn init_state(&mut self, state: &mut S) -> Result<(), Error> {
        state.metadata_or_insert_with(CoverageProgressMetadata::default);
        Ok(())
    }
}

impl<C, EM, I, O, OT, S> Feedback<EM, I, OT, S> for CoverageProgressFeedback<C, O>
where
    C: AsRef<O>,
    O: MapObserver<Entry = u8> + for<'it> AsIter<'it, Item = u8>,
    OT: MatchName,
    S: HasMetadata + HasNamedMetadata,
{
    fn is_interesting(
        &mut self,
        state: &mut S,
        _manager: &mut EM,
        _input: &I,
        observers: &OT,
        _exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        let coverage_advanced = {
            let Some(map_state) = state
                .named_metadata_map()
                .get::<MapFeedbackMetadata<u8>>(&self.map_name)
            else {
                return Ok(false);
            };
            let observer = observers
                .get(&self.map_ref)
                .expect("MapObserver not found for coverage progress")
                .as_ref();
            let initial = observer.initial();
            observer
                .as_iter()
                .map(|value| *value)
                .enumerate()
                .filter(|(_, value)| *value != initial)
                .any(|(idx, value)| {
                    let existing = map_state.history_map.get(idx).copied().unwrap_or(initial);
                    value > existing
                })
        };

        if coverage_advanced {
            let progress = state.metadata_or_insert_with(CoverageProgressMetadata::default);
            progress.epoch = progress.epoch.saturating_add(1);
        }
        Ok(false)
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
    let value_definitions = selection
        .state_transfer_chains
        .iter()
        .flat_map(|transfer| transfer.operations.iter())
        .filter_map(|operation| {
            let symbol = operation.get("target_symbol")?.as_str()?;
            let expression = operation.get("value_expression")?.clone();
            Some((
                symbol
                    .rsplit(['.', ':'])
                    .next()
                    .unwrap_or(symbol)
                    .to_ascii_uppercase(),
                expression,
            ))
        })
        .collect();
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

#[derive(Debug, Default)]
struct ContentHashInputFilter {
    seen_hashes: HashSet<String>,
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
        Ok(self.seen_hashes.insert(content_hash(input.as_ref())))
    }
}
