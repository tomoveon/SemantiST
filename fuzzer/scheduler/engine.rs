#[derive(Clone, Debug)]
struct TargetAwareScheduler {
    base: QueueScheduler,
    bootstrap_path: Option<PathBuf>,
    seed_dir: PathBuf,
    bootstrap_modified: Option<SystemTime>,
    bootstrap: HashMap<String, StSeedMetadata>,
    seen_hashes: HashSet<String>,
    semantic_ids_by_hash: HashMap<String, CorpusId>,
}

const SEMANTIC_SCHEDULING_PROBABILITY: f64 = 0.75;
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
    fn new(bootstrap_path: Option<PathBuf>, seed_dir: PathBuf) -> Self {
        let bootstrap = load_bootstrap_metadata(bootstrap_path.as_deref());
        let bootstrap_modified = bootstrap_path
            .as_deref()
            .and_then(|path| fs::metadata(path).ok())
            .and_then(|metadata| metadata.modified().ok());
        Self {
            base: QueueScheduler::new(),
            bootstrap_path,
            seed_dir,
            bootstrap_modified,
            bootstrap,
            seen_hashes: HashSet::new(),
            semantic_ids_by_hash: HashMap::new(),
        }
    }

    fn persist_runtime_seed(
        &mut self,
        bytes: &[u8],
        metadata: &StSeedMetadata,
    ) -> Result<(), Error> {
        if !is_valid_structured_seed(bytes) {
            return Ok(());
        }

        fs::create_dir_all(&self.seed_dir)?;
        let seed_path = self
            .seed_dir
            .join(format!("{}.seed", metadata.content_hash));
        if !seed_path.exists() {
            fs::write(seed_path, bytes)?;
        }

        let Some(bootstrap_path) = self.bootstrap_path.as_deref() else {
            return Ok(());
        };
        if self.bootstrap.contains_key(&metadata.content_hash) {
            return Ok(());
        }

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(bootstrap_path)?;
        serde_json::to_writer(&mut file, metadata).map_err(|error| {
            Error::serialize(format!(
                "unable to serialize runtime seed metadata: {error}"
            ))
        })?;
        file.write_all(b"\n")?;
        self.bootstrap
            .insert(metadata.content_hash.clone(), metadata.clone());
        self.bootstrap_modified = fs::metadata(bootstrap_path)
            .and_then(|metadata| metadata.modified())
            .ok();
        Ok(())
    }

    fn refresh_bootstrap<I, S>(&mut self, state: &mut S) -> Result<(), Error>
    where
        S: HasCorpus<I> + HasMetadata + HasRand,
        I: AsRef<Vec<u8>>,
    {
        let Some(path) = self.bootstrap_path.as_deref() else {
            return Ok(());
        };
        let modified = fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .ok();
        if modified.is_some() && modified == self.bootstrap_modified {
            return Ok(());
        }

        self.bootstrap = load_bootstrap_metadata(Some(path));
        self.bootstrap_modified = modified;
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            runtime.ranking_statistics.corpus_scan_count = runtime
                .ranking_statistics
                .corpus_scan_count
                .saturating_add(1);
        }
        let ids = state.corpus().ids().collect::<Vec<_>>();
        for id in ids {
            let bytes = {
                let mut testcase = state.corpus().get(id)?.borrow_mut();
                testcase.load_input(state.corpus())?.as_ref().to_vec()
            };
            let hash = content_hash(&bytes);
            let Some(metadata) = self.bootstrap.get(&hash).cloned() else {
                continue;
            };
            state
                .corpus()
                .get(id)?
                .borrow_mut()
                .add_metadata(metadata.clone());
            state
                .metadata_or_insert_with(SemanticCorpusIndexMetadata::default)
                .ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            self.semantic_ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.cache_seed_profile(seed_profile_from_metadata(&metadata));
            }
        }
        Ok(())
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
            .as_ref()
            .and_then(|hash| {
                state
                    .metadata_map()
                    .get::<SemanticCorpusIndexMetadata>()
                    .and_then(|index| index.ids_by_hash.get(hash))
                    .or_else(|| self.semantic_ids_by_hash.get(hash))
            })
            .copied();
        if selection.best_seed_id.is_some() && selected_id.is_none() {
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                if let Some(hash) = selection.best_seed_id.as_deref() {
                    runtime.invalidate_seed(hash);
                }
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
        let bytes = {
            let mut testcase = state.corpus().get(id)?.borrow_mut();
            testcase.load_input(state.corpus())?.as_ref().to_vec()
        };
        let hash = content_hash(&bytes);
        let parent_id = state.corpus().current().and_then(|parent_id| {
            state
                .corpus()
                .get(parent_id)
                .ok()
                .and_then(|cell| {
                    cell.borrow()
                        .metadata_map()
                        .get::<StSeedMetadata>()
                        .cloned()
                })
                .map(|metadata| metadata.content_hash)
        });
        let metadata = self
            .bootstrap
            .get(&hash)
            .cloned()
            .unwrap_or_else(|| StSeedMetadata::new(source_context(state), parent_id, &bytes));
        let mut metadata = metadata;
        let context = target_context(state);
        if metadata.target_id.is_none() {
            metadata.target_id = context.target_id.clone();
        }
        if let Some(semantic) = state.metadata_map().get::<SemanticCoverageMetadata>() {
            if !semantic.last_covered_target_ids.is_empty() {
                metadata.semantic_coverage.clear();
                metadata.new_st_branch_sides = semantic.last_new_branch_sides.clone();
                metadata.new_state_transitions = semantic.last_new_state_transitions.clone();
                metadata.covered_target = semantic.last_covered_target.clone().or_else(|| {
                    semantic
                        .last_new_branch_sides
                        .first()
                        .or_else(|| semantic.last_new_state_transitions.first())
                        .cloned()
                });
                metadata.covered_target_ids = semantic.last_covered_target_ids.clone();
                metadata.covered_targets_by_cycle = semantic.last_covered_targets_by_cycle.clone();
                metadata.cycle_ids = semantic.last_cycle_ids.clone();
                metadata.state_signatures = semantic.last_state_signatures.clone();
            }
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
        if !self.seen_hashes.insert(hash.clone()) {
            eprintln!("[metadata] duplicate testcase content observed: {hash}");
        }
        state
            .corpus()
            .get(id)?
            .borrow_mut()
            .add_metadata(metadata.clone());
        self.persist_runtime_seed(&bytes, &metadata)?;
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
        _id: CorpusId,
        testcase: &Option<libafl::corpus::Testcase<I>>,
    ) -> Result<(), Error> {
        let hash = testcase.as_ref().and_then(|testcase| {
            testcase
                .metadata_map()
                .get::<StSeedMetadata>()
                .map(|metadata| metadata.content_hash.clone())
        });
        if let Some(hash) = hash {
            self.semantic_ids_by_hash.remove(&hash);
            if let Some(index) = state
                .metadata_map_mut()
                .get_mut::<SemanticCorpusIndexMetadata>()
            {
                index.ids_by_hash.remove(&hash);
            }
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.invalidate_seed(&hash);
            }
        }
        Ok(())
    }

    fn on_replace(
        &mut self,
        state: &mut S,
        id: CorpusId,
        previous: &libafl::corpus::Testcase<I>,
    ) -> Result<(), Error> {
        let previous_hash = previous
            .metadata_map()
            .get::<StSeedMetadata>()
            .map(|metadata| metadata.content_hash.clone());
        if let Some(hash) = previous_hash {
            self.semantic_ids_by_hash.remove(&hash);
            if let Some(index) = state
                .metadata_map_mut()
                .get_mut::<SemanticCorpusIndexMetadata>()
            {
                index.ids_by_hash.remove(&hash);
            }
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.invalidate_seed(&hash);
            }
        }
        self.attach_metadata(state, id)?;
        let metadata = state
            .corpus()
            .get(id)?
            .borrow()
            .metadata_map()
            .get::<StSeedMetadata>()
            .cloned();
        if let Some(metadata) = metadata {
            self.semantic_ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            state
                .metadata_or_insert_with(SemanticCorpusIndexMetadata::default)
                .ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.cache_seed_profile(seed_profile_from_metadata(&metadata));
            }
        }
        Ok(())
    }
}

impl<I, S> Scheduler<I, S> for TargetAwareScheduler
where
    S: HasCorpus<I> + HasMetadata + HasRand,
    I: AsRef<Vec<u8>>,
{
    fn on_add(&mut self, state: &mut S, id: CorpusId) -> Result<(), Error> {
        self.base.on_add(state, id)?;
        self.attach_metadata(state, id)?;
        let metadata = state
            .corpus()
            .get(id)?
            .borrow()
            .metadata_map()
            .get::<StSeedMetadata>()
            .cloned();
        if let Some(metadata) = metadata {
            self.semantic_ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            state
                .metadata_or_insert_with(SemanticCorpusIndexMetadata::default)
                .ids_by_hash
                .insert(metadata.content_hash.clone(), id);
            if let Some(runtime) = state
                .metadata_map_mut()
                .get_mut::<SemanticRuntimeMetadata>()
            {
                runtime.cache_seed_profile(seed_profile_from_metadata(&metadata));
            }
        }
        Ok(())
    }

    fn next(&mut self, state: &mut S) -> Result<CorpusId, Error> {
        self.refresh_bootstrap(state)?;
        if state
            .rand_mut()
            .coinflip(SEMANTIC_SCHEDULING_PROBABILITY)
        {
            if let Some(id) = self.semantic_next(state)? {
                self.set_current_scheduled(state, Some(id))?;
                return Ok(id);
            }
        }
        state.add_metadata(TargetContext::default());
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
// 这对应 StructuredFuzzer 的 `%IX0.0,BOOL,1` 思路，只是这里的 NAME 是 ST 函数参数名。
