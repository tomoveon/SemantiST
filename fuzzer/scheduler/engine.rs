#[derive(Clone, Debug)]
struct TargetAwareScheduler {
    base: QueueScheduler,
    bootstrap_path: Option<PathBuf>,
    // Records of earlier runs, by content hash. Only a resumed run hashes its
    // new seeds, to reuse their metadata.
    bootstrap: HashMap<String, StSeedMetadata>,
    bootstrap_flushed: std::time::Instant,
    semantic_scheduling_enabled: bool,
}

const SEMANTIC_SCHEDULING_PROBABILITY: f64 = 0.75;
const BOOTSTRAP_FLUSH_INTERVAL: Duration = Duration::from_secs(5);
const TARGET_EXPLORATION_PROBABILITY: f64 = 0.15;
const SEED_EXPLORATION_PROBABILITY: f64 = 0.20;

fn rank_weighted_index<R>(rand: &mut R, len: usize) -> usize
where
    R: Rand,
{
    let len = len.min(4);
    if len <= 1 {
        return 0;
    }
    let weights = [8_usize, 4, 2, 1];
    let total = weights[..len].iter().sum::<usize>();
    let mut draw = rand.below_or_zero(total);
    for (index, weight) in weights[..len].iter().enumerate() {
        if draw < *weight {
            return index;
        }
        draw -= *weight;
    }
    len - 1
}

fn rank_weighted_or_explore_index<R>(
    rand: &mut R,
    len: usize,
    explore_probability: f64,
) -> usize
where
    R: Rand,
{
    if len > 1 && rand.coinflip(explore_probability) {
        rand.below_or_zero(len)
    } else {
        rank_weighted_index(rand, len)
    }
}

impl TargetAwareScheduler {
    fn new(bootstrap_path: Option<PathBuf>, ablation: SemantiSTAblation) -> Self {
        let bootstrap = load_bootstrap_metadata(bootstrap_path.as_deref());
        Self {
            base: QueueScheduler::new(),
            bootstrap_path,
            bootstrap,
            bootstrap_flushed: std::time::Instant::now(),
            semantic_scheduling_enabled: ablation.semantic_scheduling(),
        }
    }

    // One append per interval and one after a bounded run; the records are
    // a report artifact, never read back during this run. Content hashes are
    // computed here, for the records written, not when seeds are added.
    fn flush_bootstrap<I, S>(&mut self, state: &mut S) -> Result<(), Error>
    where
        S: HasCorpus<I> + HasMetadata,
        I: AsRef<Vec<u8>>,
    {
        self.bootstrap_flushed = std::time::Instant::now();
        let Some(path) = self.bootstrap_path.as_deref() else {
            return Ok(());
        };
        let Some(pending) = state.metadata_map_mut().remove::<PendingBootstrapRecords>() else {
            return Ok(());
        };
        let state = &*state;
        let _timer = PhaseTimer::start(Phase::IoSeedPersist);
        let hash_of = |id: CorpusId| -> Option<String> {
            let mut testcase = state.corpus().get(id).ok()?.borrow_mut();
            let input = testcase.load_input(state.corpus()).ok()?;
            Some(content_hash(input.as_ref().as_ref()))
        };
        let mut records = Vec::new();
        for id in pending.ids {
            // A seed removed before the flush leaves no record.
            let Some(hash) = hash_of(id) else { continue; };
            let Some(mut metadata) = state.corpus().get(id).ok()
                .and_then(|cell| cell.borrow().metadata_map().get::<StSeedMetadata>().cloned()) else { continue; };
            metadata.content_hash = hash;
            metadata.parent_id = metadata.parent.and_then(hash_of);
            serde_json::to_writer(&mut records, &metadata).map_err(|error| {
                Error::serialize(format!(
                    "unable to serialize runtime seed metadata: {error}"
                ))
            })?;
            records.push(b'\n');
        }
        append_bytes(path, &records)
    }

    fn semantic_next<I, S>(&mut self, state: &mut S) -> Result<Option<CorpusId>, Error>
    where
        S: HasCorpus<I> + HasMetadata + HasRand,
        I: AsRef<Vec<u8>>,
    {
        let task_ids = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
            .map(SemanticRuntimeMetadata::selectable_task_ids)
            .unwrap_or_default();
        if task_ids.is_empty() {
            return Ok(None);
        }
        let task_rank = rank_weighted_or_explore_index(
            state.rand_mut(),
            task_ids.len(),
            TARGET_EXPLORATION_PROBABILITY,
        );
        let task_id = &task_ids[task_rank];
        let broaden_seed = state
            .rand_mut()
            .coinflip(SEED_EXPLORATION_PROBABILITY);
        let seed_count = state
            .metadata_map()
            .get::<SemanticRuntimeMetadata>()
            .map(|runtime| runtime.seed_candidate_count(task_id, broaden_seed))
            .unwrap_or(0);
        let seed_rank = if broaden_seed {
            state.rand_mut().below_or_zero(seed_count)
        } else {
            rank_weighted_index(state.rand_mut(), seed_count)
        };
        let Some(selection) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
            .and_then(|runtime| {
                runtime.activate_task_with_seed_mode(task_id, seed_rank, broaden_seed)
            })
        else {
            return Ok(None);
        };
        let context = semantic_target_context(&selection);
        let selected_id = selection
            .best_seed_id
            .filter(|id| state.corpus().get(*id).is_ok());
        if let (Some(missing), None) = (selection.best_seed_id, selected_id) {
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.invalidate_seed(missing);
            }
            return Ok(None);
        }
        state.add_metadata(context);
        Ok(selected_id)
    }

    fn attach_metadata<I, S>(&mut self, state: &mut S, id: CorpusId) -> Result<(), Error>
    where
        S: HasCorpus<I> + HasMetadata,
        I: AsRef<Vec<u8>>,
    {
        let (valid, recorded) = {
            let mut testcase = state.corpus().get(id)?.borrow_mut();
            let bytes = testcase.load_input(state.corpus())?.as_ref();
            let recorded = (!self.bootstrap.is_empty())
                .then(|| self.bootstrap.get(&content_hash(bytes)).cloned())
                .flatten();
            (is_valid_structured_seed(bytes), recorded)
        };
        let from_bootstrap = recorded.is_some();
        let mut metadata = recorded
            .unwrap_or_else(|| StSeedMetadata::new(source_context(state), *state.corpus().current()));
        let context = target_context(state);
        if metadata.target_id.is_none() {
            metadata.target_id = context.target_id.clone();
        }
        if let Some(semantic) = state.metadata_map().get::<SemanticCoverageMetadata>() {
            // An execution with no semantic hits must clear stale bootstrap
            // evidence as well; profiles describe this input's actual run.
                metadata.semantic_coverage.clear();
                metadata.new_st_branch_sides = semantic.last_new_branch_sides();
                metadata.new_state_transitions = semantic.last_new_state_transitions();
                metadata.covered_target = semantic.last_covered_target().or_else(|| {
                    metadata
                        .new_st_branch_sides
                        .first()
                        .or_else(|| metadata.new_state_transitions.first())
                        .cloned()
                });
                metadata.covered_target_ids = semantic.last_covered_target_ids();
                metadata.ordered_target_ids = semantic.last_ordered_target_ids();
                metadata.ordered_trace_complete = semantic.last_ordered_trace_complete;
                metadata.covered_targets_by_cycle = semantic.last_covered_targets_by_cycle();
                metadata.cycle_ids = semantic.last_cycle_ids();
                metadata.state_signatures = semantic.last_state_signatures();
        }
        if let Some(runtime) = state.metadata_map().get::<SemanticRuntimeMetadata>() {
            let covered = metadata
                .covered_target_ids
                .iter()
                .cloned()
                .collect::<HashSet<_>>();
            metadata.target_affinity = runtime
                .plan
                .tasks
                .iter()
                .filter_map(|task| {
                    if task.ordered_waypoints.is_empty() {
                        return None;
                    }
                    let overlap = task
                        .ordered_waypoints
                        .iter()
                        .filter(|target_id| covered.contains(*target_id))
                        .count();
                    Some((
                        task.task_id.clone(),
                        overlap as f64 / task.ordered_waypoints.len() as f64,
                    ))
                })
                .collect();
        }
        state
            .corpus()
            .get(id)?
            .borrow_mut()
            .add_metadata(metadata);
        // A seed carried over from an earlier run's records is already there.
        if valid && !from_bootstrap && self.bootstrap_path.is_some() {
            state.metadata_or_insert_with(PendingBootstrapRecords::default).ids.push(id);
        }
        Ok(())
    }

    fn cache_profile<I, S>(&mut self, state: &mut S, id: CorpusId) -> Result<(), Error>
    where
        S: HasCorpus<I> + HasMetadata,
    {
        let metadata = state
            .corpus()
            .get(id)?
            .borrow()
            .metadata_map()
            .get::<StSeedMetadata>()
            .cloned();
        if let (Some(metadata), Some(runtime)) =
            (metadata, state.metadata_map_mut().get_mut::<SemanticRuntimeMetadata>())
        {
            runtime.cache_seed_profile(seed_profile_from_metadata(id, &metadata));
        }
        Ok(())
    }
}

impl<I, S> RemovableScheduler<I, S> for TargetAwareScheduler
where
    S: HasCorpus<I> + HasMetadata,
    I: AsRef<Vec<u8>>,
{
    fn on_remove(
        &mut self,
        state: &mut S,
        id: CorpusId,
        _testcase: &Option<libafl::corpus::Testcase<I>>,
    ) -> Result<(), Error> {
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            runtime.invalidate_seed(id);
        }
        Ok(())
    }

    fn on_replace(
        &mut self,
        state: &mut S,
        id: CorpusId,
        _previous: &libafl::corpus::Testcase<I>,
    ) -> Result<(), Error> {
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            runtime.invalidate_seed(id);
        }
        self.attach_metadata(state, id)?;
        self.cache_profile(state, id)
    }
}

impl<I, S> Scheduler<I, S> for TargetAwareScheduler
where
    S: HasCorpus<I> + HasMetadata + HasRand,
    I: AsRef<Vec<u8>>,
{
    fn on_add(&mut self, state: &mut S, id: CorpusId) -> Result<(), Error> {
        let _timer = PhaseTimer::start(Phase::SchedulerOnAdd);
        self.base.on_add(state, id)?;
        self.attach_metadata(state, id)?;
        self.cache_profile(state, id)
    }

    fn next(&mut self, state: &mut S) -> Result<CorpusId, Error> {
        let _timer = PhaseTimer::start(Phase::SchedulerNext);
        if self.bootstrap_flushed.elapsed() >= BOOTSTRAP_FLUSH_INTERVAL {
            self.flush_bootstrap(state)?;
        }
        if let Some(runtime) = state.metadata_map_mut().get_mut::<SemanticRuntimeMetadata>() {
            runtime.tick_selection();
        }
        if self.semantic_scheduling_enabled
            && state
            .rand_mut()
            .coinflip(SEMANTIC_SCHEDULING_PROBABILITY)
        {
            if let Some(id) = self.semantic_next(state)? {
                self.set_current_scheduled(state, Some(id))?;
                return Ok(id);
            }
        }
        state.add_metadata(TargetContext::default());
        if let Some(runtime) = state.metadata_map_mut().get_mut::<SemanticRuntimeMetadata>() {
            runtime.suspend_selection();
        }
        self.base.next(state)
    }

    fn set_current_scheduled(
        &mut self,
        state: &mut S,
        next_id: Option<CorpusId>,
    ) -> Result<(), Error> {
        *state.corpus_mut().current_mut() = next_id;
        Ok(())
    }
}

// 结构化 ST mutator：保持输入为 `NAME,TYPE,VALUE` 的文本语法。
// NAME 对应 ST 函数参数名。
