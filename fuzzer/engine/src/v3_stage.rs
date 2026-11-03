use std::collections::BTreeSet;
use std::collections::VecDeque;
use std::time::Instant;

// Rust goal-directed stage.  It plans canonical frame sequences, submits them
// to the LibAFL executor through PrefixCachingExecutor, and records goal hits.
// All constants come from the execution bundle (STG literals, task thresholds,
// ABI types); there are no target- or suite-specific templates here.


#[derive(Clone, Debug)]
pub struct V3Input {
    pub frames: Vec<Frame>,
    pub shared: BTreeMap<String, String>,
}

#[derive(Clone, Debug)]
pub struct V3Prefix {
    pub prefix_id: String,
    pub parent_id: Option<String>,
    pub task_id: Option<String>,
    pub input: V3Input,
    pub path: Vec<u8>,
    pub distance: f64,
    pub source: &'static str,
    pub uses: u64,
}

#[derive(Clone, Debug, Default)]
pub struct StageStats {
    pub executions: u64,
    pub scans: u64,
    pub suffix_scans: u64,
    pub cache_hits: u64,
    pub saved_scans: u64,
    pub snapshot_attempts: u64,
    pub snapshot_validations: u64,
    pub snapshot_invalidations: u64,
    pub candidates: u64,
    pub state_copy_candidates: u64,
    pub hold_actions: u64,
    pub wait_actions: u64,
    pub state_copies: u64,
    pub random_prefix_picks: u64,
    pub goal_prefix_picks: u64,
    pub solver_candidates: u64,
    pub solver_seeds_loaded: u64,
    pub solver_seeds_executed: u64,
    pub solver_seeds_retained: u64,
    pub llm_candidates: u64,
    pub action_counts: HashMap<&'static str, u64>,
    pub reached: BTreeSet<String>,
}

pub struct V3GoalStage {
    pub bundle: V3Bundle,
    pub rng: StdRand,
    pub seeds: Vec<V3Input>,
    next_seed: usize,
    next_solver_seed: u64,
    pending_solver_seeds: VecDeque<(u64, V3Input, Option<String>, Option<String>)>,
    current_solver_seed: bool,
    current_solver_seed_id: Option<u64>,
    current_solver_task_id: Option<String>,
    current_solver_query_id: Option<String>,
    next_prefix_number: u64,
    pub prefixes: Vec<V3Prefix>,
    retained_prefix_limit: usize,
    pub stats: StageStats,
    pub literals: Vec<i64>,
    pub floats: Vec<f64>,
    pub max_depth: usize,
    pub no_distance: bool,
    pub no_goal_prefix: bool,
    pub no_time_guidance: bool,
    pub no_state_copy: bool,
    pub no_snapshot: bool,
    pub active_cache: Vec<u8>,
    active_task_id: Option<String>,
    task_cursor: usize,
    task_attempts: HashMap<String, u64>,
    task_stagnation: HashMap<String, u64>,
    task_best_prefix: HashMap<String, String>,
    task_novel_hits: HashMap<String, u64>,
    task_novel_states: HashMap<String, u64>,
    task_distance_change: HashMap<String, f64>,
    best_distance: HashMap<String, f64>,
    last_state: BTreeMap<String, i128>,
    current: Option<V3Input>,
    current_path: Vec<u8>,
    started: Instant,
    last_stats: Instant,
    metrics_file: PathBuf,
    run_dir: PathBuf,
    // Append-only handoff consumed by the live KLEE suffix worker.  Prefix
    // identity remains the in-memory node/order ID; this file is transport.
    solver_queue_file: PathBuf,
    last_semantic: HashSet<u32>,
    seen_semantic_hits: HashSet<u32>,
    seen_task_reaches: HashSet<(String, u32)>,
    seen_states: HashSet<String>,
    pub ended: String,
}

impl V3GoalStage {
    pub fn new(
        bundle: V3Bundle,
        run_dir: PathBuf,
        seed_dir: &Path,
        literals: Vec<i64>,
        floats: Vec<f64>,
        seed: u64,
        no_distance: bool,
        no_goal_prefix: bool,
        no_time_guidance: bool,
        no_state_copy: bool,
        no_snapshot: bool,
    ) -> Self {
        let seeds = load_seeds(seed_dir, &bundle);
        let max_depth = if bundle.tasks.unknown_cycle_effects { 4 } else { 300 };
        let planned = bundle
            .tasks
            .tasks
            .iter()
            .flat_map(|task| task.seed_prefixes.iter().cloned()
                .map(move |frames| (task.task_id.clone(), frames)))
            .collect::<Vec<_>>();
        let retained_prefix_limit = planned.iter().filter(|(_, frames)| !frames.is_empty())
            .count().saturating_add(513);
        let metrics_file = run_dir.join("metrics").join("events.jsonl");
        let solver_queue_file = run_dir.join("prefixes").join("klee-queue.jsonl");
        let next_prefix_number = fs::read_dir(run_dir.join("prefixes").join("frames"))
            .ok()
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .filter_map(|entry| entry.file_name().to_string_lossy().strip_prefix("p-")
                .and_then(|rest| rest.strip_suffix(".txt"))
                .and_then(|number| number.parse::<u64>().ok()))
            .max().unwrap_or(0) + 1;
        fs::create_dir_all(metrics_file.parent().unwrap_or(&run_dir)).ok();
        Self {
            rng: StdRand::with_seed(seed),
            seeds,
            next_seed: 0,
            next_solver_seed: 1,
            pending_solver_seeds: VecDeque::new(),
            current_solver_seed: false,
            current_solver_seed_id: None,
            current_solver_task_id: None,
            current_solver_query_id: None,
            next_prefix_number,
            prefixes: {
                let mut prefixes = vec![V3Prefix { prefix_id: "p-00000000".into(), parent_id: None, task_id: None, input: V3Input { frames: Vec::new(), shared: BTreeMap::new() }, path: Vec::new(), distance: f64::INFINITY, source: "seed", uses: 0 }];
                for (planned_index, (task_id, frames)) in planned.into_iter().enumerate() {
                    let input = V3Input {
                        frames: frames.into_iter().enumerate().map(|(frame_index, values)| Frame { index: frame_index as u32, values, dt_ns: None }).collect(),
                        shared: BTreeMap::new(),
                    };
                    if !input.frames.is_empty() {
                        prefixes.push(V3Prefix { prefix_id: format!("p-plan-{planned_index:08}"), parent_id: Some("p-00000000".into()), task_id: Some(task_id), path: serialize(&bundle, &input.frames, &input.shared), input, distance: 0.0, source: "planning", uses: 0 });
                    }
                }
                prefixes
            },
            retained_prefix_limit,
            bundle,
            stats: StageStats::default(),
            literals,
            floats,
            max_depth,
            no_distance,
            no_goal_prefix,
            no_time_guidance,
            no_state_copy,
            no_snapshot,
            active_cache: Vec::new(),
            active_task_id: None,
            task_cursor: 0,
            task_attempts: HashMap::new(),
            task_stagnation: HashMap::new(),
            task_best_prefix: HashMap::new(),
            task_novel_hits: HashMap::new(),
            task_novel_states: HashMap::new(),
            task_distance_change: HashMap::new(),
            best_distance: HashMap::new(),
            last_state: BTreeMap::new(),
            current: None,
            current_path: Vec::new(),
            started: Instant::now(),
            last_stats: Instant::now(),
            metrics_file,
            run_dir,
            solver_queue_file,
            last_semantic: HashSet::new(),
            seen_semantic_hits: HashSet::new(),
            seen_task_reaches: HashSet::new(),
            seen_states: HashSet::new(),
            ended: "budget".to_string(),
        }
    }

    fn below(&mut self, upper: u64) -> u64 {
        if upper <= 1 {
            return 0;
        }
        self.rng
            .below(std::num::NonZeroUsize::new(upper as usize).expect("nonzero")) as u64
    }

    pub fn elapsed(&self) -> Duration {
        self.started.elapsed()
    }

    pub fn current_bytes(&self) -> &[u8] {
        &self.current_path
    }

    pub fn current_input(&self) -> Option<&V3Input> {
        self.current.as_ref()
    }

    pub fn select_task(&mut self) -> Option<String> {
        let len = self.bundle.tasks.tasks.len();
        if len == 0 {
            self.active_task_id = None;
            return None;
        }
        for _ in 0..len {
            let index = self.task_cursor % len;
            self.task_cursor += 1;
            let task = &self.bundle.tasks.tasks[index];
            let limit = task.budget.as_ref().and_then(|budget| budget.get("attempts"))
                .and_then(serde_json::Value::as_u64).unwrap_or(u64::MAX);
            if self.stats.reached.contains(&task.task_id)
                || self.task_attempts.get(&task.task_id).copied().unwrap_or(0) >= limit {
                continue;
            }
            self.active_task_id = Some(task.task_id.clone());
            return self.active_task_id.clone();
        }
        self.active_task_id = None;
        None
    }

    pub fn all_goals_reached(&self) -> bool {
        let observable = self.bundle.tasks.tasks.iter()
            .filter(|task| task.goal.runtime_id.is_some())
            .collect::<Vec<_>>();
        !observable.is_empty() && observable.iter().all(|task|
            self.stats.reached.contains(&task.task_id))
    }

    fn ingest_solver_seeds(&mut self) {
        let directory = self.run_dir.join("solver").join("reinject");
        loop {
            let path = directory.join(format!("solver-seed-{:08}.seed", self.next_solver_seed));
            let Ok(bytes) = fs::read(&path) else { break };
            self.next_solver_seed += 1;
            let input = std::str::from_utf8(&bytes).ok()
                .and_then(|text| parse_testcase(text, &self.bundle.schema()).ok())
                .filter(|case| case.init_profile == "cold"
                    && !case.frames.is_empty() && case.frames.len() <= self.max_depth);
            if let Some(case) = input {
                let metadata = fs::read(path.with_extension("json")).ok()
                    .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok());
                let task_id = metadata.as_ref()
                    .and_then(|value| value.get("task_id"))
                    .and_then(|value| value.as_str())
                    .filter(|task_id| self.bundle.tasks.tasks.iter()
                        .any(|task| task.task_id == *task_id))
                    .map(str::to_owned);
                let query_id = metadata.as_ref()
                    .and_then(|value| value.get("query_id"))
                    .and_then(|value| value.as_str())
                    .map(str::to_owned);
                self.pending_solver_seeds.push_back((self.next_solver_seed - 1,
                    V3Input { frames: case.frames, shared: case.shared },
                    task_id.clone(), query_id.clone()));
                self.stats.solver_seeds_loaded += 1;
                append_event(&self.metrics_file, serde_json::json!({
                    "kind": "solver_seed_loaded", "seed_id": self.next_solver_seed - 1,
                    "task_id": task_id, "query_id": query_id,
                })).expect("failed to append solver seed event");
            } else {
                append_event(&self.metrics_file, serde_json::json!({
                    "kind": "solver_seed_rejected", "seed_id": self.next_solver_seed - 1,
                    "reason": "invalid frame sequence",
                })).expect("failed to append solver seed event");
            }
        }
    }

    pub fn next_input(&mut self) -> Vec<u8> {
        self.ingest_solver_seeds();
        self.current_solver_seed = false;
        self.current_solver_seed_id = None;
        self.current_solver_task_id = None;
        self.current_solver_query_id = None;
        if let Some((seed_id, mut seed, task_id, query_id)) = self.pending_solver_seeds.pop_front() {
            if self.bundle.tasks.single_cycle && seed.frames.len() > 1 {
                seed.frames = vec![seed.frames.pop().expect("nonempty solver seed")];
            }
            let bytes = serialize(&self.bundle, &seed.frames, &seed.shared);
            self.current_path = bytes.clone();
            self.current = Some(seed);
            self.current_solver_seed = true;
            self.current_solver_seed_id = Some(seed_id);
            self.current_solver_task_id = task_id;
            self.current_solver_query_id = query_id;
            if let Some(task_id) = self.current_solver_task_id.as_ref() {
                self.active_task_id = Some(task_id.clone());
            }
            self.stats.solver_seeds_executed += 1;
            *self.stats.action_counts.entry("solver_seed").or_insert(0) += 1;
            return bytes;
        }
        if let Some(mut seed) = self.seeds.get(self.next_seed).cloned() {
            self.next_seed += 1;
            if self.bundle.tasks.single_cycle && seed.frames.len() > 1 {
                seed.frames = vec![seed.frames.pop().expect("nonempty seed")];
            }
            let bytes = serialize(&self.bundle, &seed.frames, &seed.shared);
            self.current_path = bytes.clone();
            self.current = Some(seed);
            *self.stats.action_counts.entry("seed").or_insert(0) += 1;
            return bytes;
        }
        let base = self.choose_prefix();
        let mut frames = base.input.frames.clone();
        if self.bundle.tasks.single_cycle && frames.len() > 1 {
            frames = vec![frames.pop().expect("nonempty prefix")];
        }
        let shared = base.input.shared.clone();
        let mut used_state_copy = false;
        if frames.is_empty() {
            frames.push(self.default_frame());
        }
        if self.bundle.tasks.single_cycle {
            self.mutate_field(&mut frames);
            let bytes = serialize(&self.bundle, &frames, &shared);
            if base.source == "solver" {
                append_event(&self.metrics_file, serde_json::json!({
                    "kind": "solver_prefix_mutated", "prefix_id": base.prefix_id,
                    "depth": frames.len(), "action": "mutate", "changed": bytes != base.path,
                })).expect("failed to append solver mutation event");
            }
            self.current = Some(V3Input { frames, shared });
            self.current_path = bytes.clone();
            *self.stats.action_counts.entry("mutate").or_insert(0) += 1;
            return bytes;
        }
        let mut actions = vec!["mutate", "extend", "pulse", "pulse_train", "hold", "wait", "duplicate", "delete", "splice", "sweep"];
        if self.bundle.tasks.tasks.iter().any(|task|
            Some(&task.task_id) == self.active_task_id.as_ref() && !task.timers.is_empty())
        {
            actions.push("timer_train");
        }
        if !self.no_time_guidance && self.bundle.tasks.tasks.iter().any(|task|
            Some(&task.task_id) == self.active_task_id.as_ref() && task.timers.is_empty()
                && task.abstraction.iter().any(|entry| entry.thresholds.iter().any(|value| *value >= 100)))
        {
            actions.push("long_hold");
        }
        let action = actions[self.below(actions.len() as u64) as usize];
        match action {
            "mutate" => self.mutate_field(&mut frames),
            "extend" => {
                let mut frame = frames.last().cloned().unwrap_or_else(|| self.default_frame());
                self.mutate_frame(&mut frame);
                frames.push(frame);
            }
            "pulse" => {
                let fields = self.boolean_fields();
                if !fields.is_empty() {
                    let field = fields[self.below(fields.len() as u64) as usize].clone();
                    let mut low = frames.last().cloned().unwrap_or_else(|| self.default_frame());
                    low.values.insert(field.clone(), "0".to_string());
                    let mut high = low.clone();
                    high.values.insert(field, "1".to_string());
                    frames.push(low);
                    frames.push(high);
                }
            }
            "pulse_train" => self.pulse_train(&mut frames),
            "timer_train" => self.timer_train(&mut frames),
            "long_hold" => self.long_hold(&mut frames),
            "hold" | "wait" => {
                if !self.no_time_guidance {
                    if action == "hold" { self.stats.hold_actions += 1; } else { self.stats.wait_actions += 1; }
                    let count = if self.below(8) == 0 {
                        1u64 << self.below(8)
                    } else {
                        1 + self.below(8)
                    };
                    let last = frames.last().cloned().unwrap_or_else(|| self.default_frame());
                    for _ in 0..count {
                        frames.push(last.clone());
                    }
                } else {
                    self.mutate_field(&mut frames);
                }
            }
            "duplicate" => {
                if !frames.is_empty() {
                    let index = self.below(frames.len() as u64) as usize;
                    let copy = frames[index].clone();
                    frames.insert(index + 1, copy);
                }
            }
            "delete" => {
                if frames.len() > 1 {
                    let index = self.below(frames.len() as u64) as usize;
                    frames.remove(index);
                }
            }
            "splice" => {
                if frames.len() > 2 {
                    let a = self.below(frames.len() as u64) as usize;
                    let b = self.below(frames.len() as u64) as usize;
                    frames.swap(a, b);
                }
            }
            "sweep" => {
                frames.clear();
                let mut base = self.default_frame();
                frames.push(base.clone());
                for field_name in self.edge_fields() {
                    let mut low = base.clone();
                    low.values.insert(field_name.clone(), "0".to_string());
                    let mut high = low.clone();
                    high.values.insert(field_name, "1".to_string());
                    frames.push(low);
                    frames.push(high.clone());
                    base = high;
                }
                for field in self.integer_fields() {
                    for value in self.field_literals(&field) {
                        base.values.insert(field.clone(), value);
                        frames.push(base.clone());
                    }
                }
            }
            _ => self.mutate_field(&mut frames),
        }
        if !self.no_state_copy && !self.last_state.is_empty() && self.below(3) == 0 {
            if self.maybe_state_copy(&mut frames) {
                used_state_copy = true;
            }
        }
        if frames.len() > self.max_depth {
            frames.truncate(self.max_depth);
        }
        let bytes = serialize(&self.bundle, &frames, &shared);
        if base.source == "solver" {
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "solver_prefix_mutated", "prefix_id": base.prefix_id,
                "depth": frames.len(), "action": action, "changed": bytes != base.path,
            })).expect("failed to append solver mutation event");
        }
        self.current = Some(V3Input { frames, shared });
        self.current_path = bytes.clone();
        if used_state_copy {
            self.stats.state_copy_candidates += 1;
            self.stats.state_copies += 1;
        }
        *self.stats.action_counts.entry(action).or_insert(0) += 1;
        bytes
    }

    pub fn current_solver_task_id(&self) -> Option<&str> {
        self.current_solver_task_id.as_deref()
    }

    pub fn current_input_frames(&self) -> Option<&[Frame]> {
        self.current.as_ref().map(|input| input.frames.as_slice())
    }

    fn remember(&mut self, input: V3Input, path: Vec<u8>, distance: f64, source: &'static str) -> bool {
        if self.prefixes.len() >= self.retained_prefix_limit {
            if !matches!(source, "solver" | "relevant_reach") {
                return false;
            }
            if let Some(index) = self.prefixes.iter().position(|prefix|
                prefix.source == "random" || prefix.source == "fuzz" || prefix.source == "state_novelty") {
                self.prefixes.remove(index);
            } else {
                return false;
            }
        }
        let prefix_id = format!("p-{}", self.next_prefix_number);
        self.next_prefix_number += 1;
        let parent_id = self.parent_for(&input);
        let frames_dir = self.run_dir.join("prefixes").join("frames");
        fs::create_dir_all(&frames_dir).ok();
        let frames_path = frames_dir.join(format!("{prefix_id}.txt"));
        if fs::write(&frames_path, &path).is_ok() {
            let record = serde_json::json!({
                "prefix_id": prefix_id.clone(),
                "contract_id": self.bundle.contract_id,
                "init_profile": "cold",
                "depth": input.frames.len(),
                "parent": parent_id.clone(),
                "frames_file": format!("prefixes/frames/{prefix_id}.txt"),
                "logical_time_ns": logical_time_ns(&self.bundle, &input.frames),
                "distance": distance,
                "abstraction": {},
                "coverage_summary": {},
                "execution_cost_us": 0,
                "restore_cost_us": 0,
                "reuse_count": 0,
                "provenance": {"source": source, "attempt_id": prefix_id, "query_id": null},
            });
            let index = self.run_dir.join("prefixes").join("prefixes.jsonl");
            append_event(&index, record).expect("failed to append prefix record");
            // A task distance improvement or its matching hazard-reach probe
            // establishes a concrete, task-related solver start.
            if matches!(source, "fuzz" | "relevant_reach") && !self.bundle.tasks.single_cycle
                && self.active_task_id.as_ref().is_some_and(|task| !self.stats.reached.contains(task))
                && (distance.is_finite() || source == "relevant_reach") {
                let queue_record = serde_json::json!({
                "queue": "klee_suffix",
                "prefix_id": prefix_id.clone(),
                "parent": parent_id.clone(),
                "task_id": self.active_task_id.clone(),
                "contract_id": self.bundle.contract_id,
                "init_profile": "cold",
                "depth": input.frames.len(),
                "frames_file": format!("prefixes/frames/{prefix_id}.txt"),
                "logical_time_ns": logical_time_ns(&self.bundle, &input.frames),
                "distance": distance,
                "native_execution": self.stats.executions,
                "consecutive_no_progress": self.active_task_id.as_ref()
                    .and_then(|task| self.task_stagnation.get(task))
                    .copied().unwrap_or(0),
                "trigger": if source == "relevant_reach" { "native_hazard_reach" }
                           else { "task_distance_improved" },
                "task_unresolved": true,
                "provenance": {"source": source, "attempt_id": prefix_id, "query_id": null},
                });
                append_event(&self.solver_queue_file, queue_record)
                    .expect("failed to append KLEE prefix queue record");
            }
        }
        self.prefixes.push(V3Prefix { prefix_id, parent_id, task_id: self.active_task_id.clone(),
                                      input, path, distance, source, uses: 0 });
        true
    }

    fn parent_for(&self, input: &V3Input) -> Option<String> {
        self.prefixes
            .iter()
            .filter(|prefix| prefix.input.frames.len() < input.frames.len()
                && prefix.input.shared == input.shared
                && prefix.input.frames.iter().zip(input.frames.iter()).all(|(left, right)| left == right))
            .max_by_key(|prefix| prefix.input.frames.len())
            .map(|prefix| prefix.prefix_id.clone())
            .or_else(|| Some("p-00000000".into()))
    }

    fn choose_prefix(&mut self) -> V3Prefix {
        if self.prefixes.is_empty() || self.below(100) < 20 {
            return V3Prefix { prefix_id: "p-00000000".into(), parent_id: None, task_id: None,
                              input: V3Input { frames: Vec::new(), shared: BTreeMap::new() },
                              path: Vec::new(), distance: f64::INFINITY, source: "seed", uses: 0 };
        }
        let mut candidates: Vec<usize> = self.prefixes.iter().enumerate()
            .filter(|(_, prefix)| !prefix.input.frames.is_empty()
                && (prefix.task_id.as_ref() == self.active_task_id.as_ref()
                    || prefix.source == "solver"))
            .map(|(index, _)| index).collect();
        if candidates.is_empty() {
            candidates = self.prefixes.iter().enumerate()
                .filter(|(_, prefix)| !prefix.input.frames.is_empty())
                .map(|(index, _)| index).collect();
        }
        if candidates.is_empty() {
            return self.prefixes[0].clone();
        }
        let selected = if self.no_goal_prefix || self.no_distance {
            self.stats.random_prefix_picks += 1;
            let index = self.below(candidates.len() as u64) as usize;
            candidates[index]
        } else {
            candidates.sort_by_key(|&index| self.prefixes[index].uses);
            let least_used = self.prefixes[candidates[0]].uses;
            let mut fair = candidates.into_iter().take_while(|&index|
                self.prefixes[index].uses == least_used).collect::<Vec<_>>();
            fair.sort_by(|&left, &right| {
                let left_solver = self.prefixes[left].source == "solver";
                let right_solver = self.prefixes[right].source == "solver";
                right_solver.cmp(&left_solver).then_with(|| self.prefixes[left].distance
                    .partial_cmp(&self.prefixes[right].distance)
                    .unwrap_or(std::cmp::Ordering::Equal))
            });
            let take = fair.len().min(8);
            self.stats.goal_prefix_picks += 1;
            fair[self.below(take as u64) as usize]
        };
        self.prefixes[selected].uses = self.prefixes[selected].uses.saturating_add(1);
        self.prefixes[selected].clone()
    }

    fn default_frame(&self) -> Frame {
        let mut values = BTreeMap::new();
        for entry in self.bundle.abi.action_fields() {
            values.insert(entry.name.clone(), default_for(&entry.spec));
        }
        Frame { index: 0, values, dt_ns: None }
    }

    fn edge_fields(&self) -> Vec<String> {
        let mut names = BTreeSet::new();
        for task in self.bundle.tasks.tasks.iter().filter(|task| task.goal_class == "defect") {
            for name in &task.edge_inputs {
                names.insert(name.clone());
            }
        }
        names.into_iter().collect()
    }

    fn integer_fields(&self) -> Vec<String> {
        self.bundle
            .abi
            .action_fields()
            .iter()
            .filter(|field| {
                field.spec.kind == "scalar"
                    && !matches!(field.spec.name.as_str(), "BOOL" | "REAL" | "LREAL")
            })
            .map(|field| field.name.clone())
            .collect()
    }

    fn field_literals(&self, field: &str) -> Vec<String> {
        let mut values = BTreeSet::new();
        let bounds = self.bundle.abi.action_fields().iter().find(|entry| entry.name == field)
            .and_then(|entry| integer_bounds(&entry.spec));
        for task in self.bundle.tasks.tasks.iter().filter(|task| task.goal_class == "defect") {
            for entry in &task.abstraction {
                if entry.field == field {
                    for value in &entry.thresholds {
                        let value = i128::from(*value);
                        if bounds.is_none_or(|(min, max)| value >= min && value <= max) {
                            values.insert(value.to_string());
                        }
                    }
                }
            }
        }
        if values.is_empty() {
            values.extend(self.literals.iter().map(|value| i128::from(*value))
                          .filter(|value| bounds.is_none_or(|(min, max)| *value >= min && *value <= max))
                          .map(|value| value.to_string()));
        }
        values.into_iter().take(8).collect()
    }

    fn boolean_fields(&self) -> Vec<String> {
        let fields = self.bundle
            .abi
            .action_fields()
            .iter()
            .filter(|entry| entry.spec.kind == "scalar" && entry.spec.name == "BOOL")
            .map(|entry| entry.name.clone())
            .collect::<Vec<_>>();
        if let Some(task) = self.bundle.tasks.tasks.iter()
            .find(|task| Some(&task.task_id) == self.active_task_id.as_ref())
        {
            let edges = fields.iter().filter(|name| task.edge_inputs.contains(name))
                .cloned().collect::<Vec<_>>();
            if !edges.is_empty() {
                return edges;
            }
            let controls = fields.iter().filter(|name| task.controllable_fields.contains(name))
                .cloned().collect::<Vec<_>>();
            if !controls.is_empty() {
                return controls;
            }
        }
        fields
    }

    fn pulse_train(&mut self, frames: &mut Vec<Frame>) {
        let fields = self.boolean_fields();
        let edges = fields.iter().filter(|name| {
            let lower = name.to_ascii_lowercase();
            !lower.contains("reset") && !lower.contains("clear")
        }).cloned().collect::<Vec<_>>();
        let choices = if edges.is_empty() { &fields } else { &edges };
        if choices.is_empty() {
            return;
        }
        let field = choices[self.below(choices.len() as u64) as usize].clone();
        let reset = self.bundle.abi.action_fields().iter()
            .find(|entry| entry.spec.kind == "scalar" && entry.spec.name == "BOOL"
                && entry.name != field && {
                    let lower = entry.name.to_ascii_lowercase();
                    lower.contains("reset") || lower.contains("clear")
                })
            .map(|entry| entry.name.clone());
        let with_reset = reset.is_some() && self.below(3) == 0;
        let count = [2, 4, 8, 16][self.below(4) as usize];
        let activate_guards = self.below(2) == 0;
        let timer_dt = self.bundle.tasks.tasks.iter()
            .find(|task| Some(&task.task_id) == self.active_task_id.as_ref() && !task.timers.is_empty())
            .map(|task| task.timers.iter().filter_map(|timer| timer.pt_const_ns)
                .max().unwrap_or(1_000_000_000).max(self.bundle.scan_period_ns));
        self.append_pulse_train(frames, &field, if with_reset { reset.as_deref() } else { None },
                                count, activate_guards, timer_dt);
    }

    fn append_pulse_train(&self, frames: &mut Vec<Frame>, field: &str, reset: Option<&str>,
                          count: usize, activate_guards: bool, frame_dt_ns: Option<u64>) {
        let mut base = frames.last().cloned().unwrap_or_else(|| self.default_frame());
        if let Some(dt_ns) = frame_dt_ns {
            base.dt_ns = Some(dt_ns);
        }
        if activate_guards {
            for entry in self.bundle.abi.action_fields() {
                if entry.spec.kind != "scalar" || matches!(entry.spec.name.as_str(), "BOOL" | "REAL" | "LREAL") {
                    continue;
                }
                if base.values.get(&entry.name).is_some_and(|value| value == "0")
                    && integer_bounds(&entry.spec).is_some_and(|(min, max)| min <= 1 && 1 <= max)
                {
                    base.values.insert(entry.name.clone(), "1".to_string());
                }
            }
        }
        if let Some(reset_field) = reset {
            base.values.insert(reset_field.to_string(), "0".to_string());
            let mut high = base.clone();
            high.values.insert(field.to_string(), "1".to_string());
            frames.push(high);
            base.values.insert(field.to_string(), "0".to_string());
            let mut reset_high = base.clone();
            reset_high.values.insert(reset_field.to_string(), "1".to_string());
            frames.push(reset_high);
        }
        base.values.insert(field.to_string(), "0".to_string());
        for _ in 0..count {
            frames.push(base.clone());
            let mut high = base.clone();
            high.values.insert(field.to_string(), "1".to_string());
            frames.push(high);
        }
    }

    fn timer_train(&mut self, frames: &mut Vec<Frame>) {
        let Some((controls, timer_dt, abstraction)) = self.bundle.tasks.tasks.iter()
            .find(|task| Some(&task.task_id) == self.active_task_id.as_ref())
            .map(|task| (task.controllable_fields.clone(),
                          task.timers.iter().filter_map(|timer| timer.pt_const_ns).max(),
                          task.abstraction.clone()))
        else { return };
        let fields = self.integer_fields().into_iter()
            .filter(|field| controls.contains(field))
            .collect::<Vec<_>>();
        if fields.is_empty() {
            return;
        }
        let field = fields[self.below(fields.len() as u64) as usize].clone();
        let bounds = self.bundle.abi.action_fields().iter()
            .find(|entry| entry.name == field)
            .and_then(|entry| integer_bounds(&entry.spec));
        let high = abstraction.iter().filter(|entry| entry.field == field)
            .flat_map(|entry| entry.thresholds.iter().map(|value| i128::from(*value)))
            .filter(|value| bounds.is_none_or(|(min, max)| *value >= min && *value <= max))
            .max()
            .or_else(|| self.field_literals(&field).into_iter()
                .filter_map(|value| value.parse::<i128>().ok()).max())
            .unwrap_or(1).max(1);
        let value = if self.below(2) == 0 { high } else { high.saturating_add(1) };
        let value = bounds.map_or(value, |(min, max)| value.clamp(min, max));
        let dt_ns = timer_dt.unwrap_or(1_000_000_000).max(self.bundle.scan_period_ns);
        let cycles = [1, 2, 4, 5][self.below(4) as usize];
        self.append_timer_train(frames, &field, &value.to_string(), dt_ns, cycles);
    }

    fn append_timer_train(&self, frames: &mut Vec<Frame>, field: &str, high_value: &str,
                          dt_ns: u64, cycles: usize) {
        let mut low = frames.last().cloned().unwrap_or_else(|| self.default_frame());
        low.values.insert(field.to_string(), "0".to_string());
        low.dt_ns = Some(self.bundle.scan_period_ns.max(1));
        let mut high = low.clone();
        high.values.insert(field.to_string(), high_value.to_string());
        high.dt_ns = Some(dt_ns);
        for _ in 0..cycles {
            frames.push(high.clone());
            frames.push(high.clone());
            frames.push(low.clone());
        }
    }

    fn long_hold(&mut self, frames: &mut Vec<Frame>) {
        let Some(task) = self.bundle.tasks.tasks.iter()
            .find(|task| Some(&task.task_id) == self.active_task_id.as_ref())
        else { return };
        let mut base = frames.last().cloned().unwrap_or_else(|| self.default_frame());
        for entry in self.bundle.abi.action_fields() {
            if !task.controllable_fields.contains(&entry.name) || entry.spec.kind != "scalar" {
                continue;
            }
            if entry.spec.name == "BOOL" {
                let lower = entry.name.to_ascii_lowercase();
                let value = if lower.contains("reset") || lower.contains("clear") { "0" } else { "1" };
                base.values.insert(entry.name.clone(), value.to_string());
            } else if let Some((min, max)) = integer_bounds(&entry.spec) {
                let threshold = task.abstraction.iter().filter(|item| item.field == entry.name)
                    .flat_map(|item| item.thresholds.iter().map(|value| i128::from(*value)))
                    .filter(|value| *value >= 100 && *value >= min && *value <= max)
                    .min();
                if let Some(value) = threshold {
                    base.values.insert(entry.name.clone(), value.to_string());
                }
            }
        }
        let count = [32, 64, 128][self.below(3) as usize];
        frames.extend(std::iter::repeat_n(base, count));
    }

    fn mutate_field(&mut self, frames: &mut [Frame]) {
        if frames.is_empty() {
            return;
        }
        let index = self.below(frames.len() as u64) as usize;
        self.mutate_frame(&mut frames[index]);
    }

    fn mutate_frame(&mut self, frame: &mut Frame) {
        let mut fields = self.bundle.abi.action_fields();
        if let Some(task) = self.bundle.tasks.tasks.iter().find(|task| Some(&task.task_id) == self.active_task_id.as_ref()) {
            let targeted = fields.iter().filter(|field| task.controllable_fields.contains(&field.name)).cloned().collect::<Vec<_>>();
            if !targeted.is_empty() { fields = targeted; }
        }
        if fields.is_empty() {
            return;
        }
        let index = self.below(fields.len() as u64) as usize;
        let field = fields[index].clone();
        let current = frame.values.get(&field.name).cloned().unwrap_or_else(|| default_for(&field.spec));
        let value = match field.spec.kind.as_str() {
            "scalar" if field.spec.name == "BOOL" => {
                if self.below(2) == 0 { "1".to_string() } else { "0".to_string() }
            }
            "scalar" if field.spec.name == "REAL" || field.spec.name == "LREAL" => {
                let choices = self.float_candidates();
                let base = current.parse::<f64>().unwrap_or(0.0);
                let chosen = choices[self.below(choices.len() as u64) as usize];
                let value = if self.below(2) == 0 { chosen } else { base + chosen };
                value.to_string()
            }
            "scalar" => {
                let (min, max) = integer_bounds(&field.spec).unwrap_or((i64::MIN as i128, i64::MAX as i128));
                let base = current.parse::<i128>().unwrap_or(0).clamp(min, max);
                let choice = self.below(8);
                let value = match choice {
                    0 => 0,
                    1 => 1,
                    2 => if min < 0 { -1 } else { max },
                    3 => base.saturating_add(1),
                    4 => base.saturating_sub(1),
                    5 => base.saturating_mul(2),
                    6 => {
                        if self.literals.is_empty() {
                            base
                        } else {
                            let index = self.below(self.literals.len() as u64) as usize;
                            i128::from(self.literals[index])
                        }
                    }
                    _ => if self.below(2) == 0 { min } else { max },
                };
                value.clamp(min, max).to_string()
            }
            "array" => self.mutate_array_value(&field.spec, &current),
            "pointer" => self.mutate_pointer_value(&field.spec, &current),
            "string" => self.mutate_string_value(&field.spec, &current),
            _ => current,
        };
        frame.values.insert(field.name.clone(), value);
    }

    fn mutate_pointer_value(&mut self, spec: &V3Spec, current: &str) -> String {
        // Pointer testcase values describe the backing object consumed by the
        // C harness, rather than a process address. Keep that object within
        // the pointee ABI size so mutation cannot create an invalid frame.
        let byte_size = spec.element.as_deref()
            .and_then(|element| element.byte_size.or(element.size))
            .or(spec.byte_size.or(spec.size))
            .unwrap_or(1)
            .max(1) as usize;
        let mut bytes = decode_hex_bytes(current, byte_size);
        match self.below(4) {
            0 => bytes.fill(0),
            1 => bytes.fill(0xff),
            2 => {
                let index = self.below(byte_size as u64) as usize;
                bytes[index] ^= 0xff;
            }
            _ => {
                let index = self.below(byte_size as u64) as usize;
                bytes[index] = bytes[index].wrapping_add(1);
            }
        }
        encode_hex_bytes(&bytes)
    }

    fn mutate_string_value(&mut self, spec: &V3Spec, current: &str) -> String {
        let wide = spec.name.eq_ignore_ascii_case("WSTRING");
        let inferred = if wide { current.encode_utf16().count() } else { current.chars().count() };
        let capacity = spec.length.unwrap_or(inferred as u32).min(4096) as usize;
        if capacity == 0 {
            return String::new();
        }
        match self.below(4) {
            0 => String::new(),
            1 => "A".to_string(),
            2 => "Z".repeat(capacity),
            _ => {
                let limit = capacity.saturating_sub(1);
                let mut value = String::new();
                let mut used = 0usize;
                for character in current.chars() {
                    let units = if wide { character.len_utf16() } else { 1 };
                    if used + units > limit {
                        break;
                    }
                    value.push(character);
                    used += units;
                }
                value.push('Z');
                value
            }
        }
    }

    fn mutate_array_value(&mut self, spec: &V3Spec, current: &str) -> String {
        let byte_size = spec.byte_size.or(spec.size).unwrap_or(0) as usize;
        if byte_size == 0 {
            return current.to_string();
        }
        let mut bytes = decode_hex_bytes(current, byte_size);
        let Some(element) = spec.element.as_deref() else {
            let index = self.below(byte_size as u64) as usize;
            bytes[index] ^= 0xff;
            return encode_hex_bytes(&bytes);
        };
        let count = spec.count.unwrap_or(1).max(1) as usize;
        let element_size = element.byte_size.or(element.size).unwrap_or(1).max(1) as usize;
        if element_size > byte_size {
            let index = self.below(byte_size as u64) as usize;
            bytes[index] ^= 0xff;
            return encode_hex_bytes(&bytes);
        }
        let count = count.min(byte_size / element_size).max(1);
        let index = self.below(count as u64) as usize;
        let offset = index * element_size;
        let value = match element.kind.as_str() {
            "scalar" if element.name == "REAL" => {
                let choices = self.float_candidates();
                (choices[self.below(choices.len() as u64) as usize] as f32).to_le_bytes().to_vec()
            }
            "scalar" if element.name == "LREAL" => {
                let choices = self.float_candidates();
                choices[self.below(choices.len() as u64) as usize].to_le_bytes().to_vec()
            }
            "scalar" => {
                let (min, max) = integer_bounds(element).unwrap_or((0, 0));
                let choices = [min, max, 0, 1];
                let chosen = choices[self.below(choices.len() as u64) as usize].clamp(min, max);
                (chosen as u128).to_le_bytes().to_vec()
            }
            _ => vec![0xff; element_size],
        };
        let copy = element_size.min(value.len());
        bytes[offset..offset + copy].copy_from_slice(&value[..copy]);
        encode_hex_bytes(&bytes)
    }

    fn float_candidates(&self) -> Vec<f64> {
        let mut values = vec![-1.0, 0.0, 0.5, 1.0, 2.0];
        values.extend(self.literals.iter().map(|value| *value as f64));
        values.extend(self.floats.iter().copied().filter(|value| value.is_finite()));
        values.sort_by(f64::total_cmp);
        values.dedup();
        let midpoints = values.windows(2).map(|pair| (pair[0] + pair[1]) / 2.0).collect::<Vec<_>>();
        values.extend(midpoints);
        values
    }

    fn maybe_state_copy(&mut self, frames: &mut [Frame]) -> bool {
        // Generic input-to-state association: copy a concrete state value into
        // an integer input of compatible width.  R1 keeps this behind
        // --no-state-copy and count it separately.
        if frames.is_empty() || self.last_state.is_empty() {
            return false;
        }
        let mut candidates: Vec<(usize, i128)> = Vec::new();
        let fields = self.bundle.abi.action_fields();
        for (index, field) in fields.iter().enumerate() {
            if let Some((min, max)) = integer_bounds(&field.spec) {
                for value in self.last_state.values().copied() {
                    if value >= min && value <= max {
                        candidates.push((index, value));
                    }
                }
            }
        }
        if candidates.is_empty() {
            return false;
        }
        let (field_index, value) = candidates[self.below(candidates.len() as u64) as usize];
        let field = fields[field_index].clone();
        let frame_index = self.below(frames.len() as u64) as usize;
        frames[frame_index].values.insert(field.name, value.to_string());
        true
    }

    pub fn on_result(
        &mut self,
        exit_kind: &libafl::executors::ExitKind,
        semantic: &[u8],
        distance: f64,
        best_frame: Option<usize>,
        task: Option<&str>,
        state_values: BTreeMap<String, i128>,
        outcome: Option<&ExecOutcome>,
        frame_feedback: &[GoalFrameFeedback],
    ) -> Result<(), String> {
        self.stats.executions += 1;
        if let Some(task_id) = task {
            *self.task_attempts.entry(task_id.to_string()).or_insert(0) += 1;
        }
        self.stats.scans += outcome.map_or(0, |executed| executed.scans as u64);
        self.stats.suffix_scans += outcome.map_or(0, |executed| executed.suffix as u64);
        if matches!(exit_kind, libafl::executors::ExitKind::Crash | libafl::executors::ExitKind::Timeout) {
            if matches!(exit_kind, libafl::executors::ExitKind::Crash) && !self.current_path.is_empty() {
                write_candidate(&self.run_dir, &self.current_path, "crash", None, None)
                    .map_err(|error| error.to_string())?;
                self.stats.candidates += 1;
            }
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "target_exit", "task_id": task,
                "exit_kind": if matches!(exit_kind, libafl::executors::ExitKind::Crash) { "Crash" } else { "Timeout" },
                "depth": self.current.as_ref().map_or(0, |input| input.frames.len()),
            })).expect("failed to append target exit event");
            self.last_state.clear();
            self.current = None;
            self.current_path.clear();
            self.current_solver_seed = false;
            self.current_solver_seed_id = None;
            self.current_solver_task_id = None;
            self.current_solver_query_id = None;
            self.active_cache.clear();
            return Ok(());
        }
        self.last_state = state_values.clone();
        let Some(input) = self.current.clone() else { return Ok(()) };
        let solver_seed = self.current_solver_seed;
        let solver_seed_id = self.current_solver_seed_id.take();
        let solver_query_id = self.current_solver_query_id.take();
        self.current_solver_seed = false;
        self.current_solver_task_id = None;
        let mut hit_tasks = Vec::new();
        for task in &self.bundle.tasks.tasks {
            let Some(runtime_id) = task.goal.runtime_id else { continue };
            if semantic.get(runtime_id as usize).copied().unwrap_or(0) > 0 {
                let first_hit = self.stats.reached.insert(task.task_id.clone());
                if task.goal_class == "defect" && task.goal.modeling.as_deref() == Some("exact") && first_hit {
                    hit_tasks.push((task.task_id.clone(), runtime_id));
                }
            }
        }
        let new_reaches = self.bundle.tasks.tasks.iter()
            .filter(|candidate| candidate.goal_class == "defect"
                && !self.stats.reached.contains(&candidate.task_id))
            .flat_map(|candidate| candidate.reach_runtime_ids.iter()
                .filter(move |&&id| semantic.get(id as usize).copied().unwrap_or(0) > 0)
                .map(move |&id| (candidate.task_id.clone(), id)))
            .filter(|pair| !solver_seed && !self.seen_task_reaches.contains(pair))
            .collect::<Vec<_>>();
        for (task_id, reach_id) in new_reaches {
            self.seen_task_reaches.insert((task_id.clone(), reach_id));
            let mut prefix = input.clone();
            if let Some(frame) = frame_feedback.iter().find(|frame| frame.goal_hits.contains(&reach_id)) {
                prefix.frames.truncate(frame.frame_index.saturating_add(1));
            }
            let path = serialize(&self.bundle, &prefix.frames, &prefix.shared);
            let original_task = self.active_task_id.replace(task_id.clone());
            let retained = self.remember(prefix, path, f64::INFINITY, "relevant_reach");
            self.active_task_id = original_task;
            if retained {
                if let Some(saved) = self.prefixes.last() {
                    self.task_best_prefix.entry(task_id.clone()).or_insert_with(|| saved.prefix_id.clone());
                }
            }
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "task_reach", "task_id": task_id, "reach_runtime_id": reach_id,
                "native_execution": self.stats.executions, "prefix_retained": retained,
            })).expect("failed to append task reach event");
        }
        let mut improved = false;
        if let Some(task_id) = task {
            let previous = self.best_distance.get(task_id).copied().unwrap_or(f64::INFINITY);
            if distance < previous {
                improved = true;
                if previous.is_finite() {
                    self.task_distance_change.insert(task_id.to_string(), previous - distance);
                }
                self.best_distance.insert(task_id.to_string(), distance);
                let mut prefix = input.clone();
                if let Some(frame_index) = best_frame {
                    prefix.frames.truncate(frame_index.saturating_add(1));
                }
                if !solver_seed {
                    let path = serialize(&self.bundle, &prefix.frames, &prefix.shared);
                    if self.remember(prefix, path, distance, "fuzz") {
                        if let Some(saved) = self.prefixes.last() {
                            self.task_best_prefix.insert(task_id.to_string(), saved.prefix_id.clone());
                        }
                    }
                }
                if let Some(frame) = best_frame.and_then(|index| frame_feedback.iter().find(|frame| frame.frame_index == index)) {
                    append_event(&self.metrics_file, serde_json::json!({
                        "kind": "goal_progress", "task_id": task_id, "frame_index": frame.frame_index,
                        "logical_time_ns": frame.logical_time_ns, "distance": distance,
                        "abstraction": frame.abstractions.get(task_id), "exit_kind": frame.exit_kind,
                    })).expect("failed to append goal progress event");
                }
            }
        }
        let semantic_now: HashSet<u32> = semantic
            .iter()
            .enumerate()
            .filter(|(_, value)| **value > 0)
            .map(|(index, _)| index as u32)
            .collect();
        let new_to_fuzzer = semantic_now.difference(&self.seen_semantic_hits).next().is_some();
        self.seen_semantic_hits.extend(semantic_now.iter().copied());
        let new_state = !state_values.is_empty()
            && self.seen_states.insert(serde_json::to_string(&state_values).unwrap_or_default());
        if let Some(task_id) = task {
            let stagnant = self.task_stagnation.entry(task_id.to_string()).or_insert(0);
            *stagnant = if improved || new_to_fuzzer || new_state { 0 } else { stagnant.saturating_add(1) };
            if new_to_fuzzer {
                *self.task_novel_hits.entry(task_id.to_string()).or_insert(0) += 1;
            }
            if new_state {
                *self.task_novel_states.entry(task_id.to_string()).or_insert(0) += 1;
            }
        }
        if solver_seed {
            let retained = (new_to_fuzzer || new_state || improved)
                && self.remember(input.clone(), self.current_path.clone(), distance, "solver");
            let retained_prefix_id = retained.then(|| self.prefixes.last().map(|prefix| prefix.prefix_id.clone())).flatten();
            if let (Some(task_id), Some(prefix_id)) = (task, retained_prefix_id.as_ref()) {
                self.task_best_prefix.entry(task_id.to_string()).or_insert_with(|| prefix_id.clone());
            }
            self.stats.solver_seeds_retained += u64::from(retained);
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "solver_seed_executed", "task_id": task,
                "seed_id": solver_seed_id, "query_id": solver_query_id,
                "depth": input.frames.len(), "distance": distance,
                "new_runtime_hit": new_to_fuzzer, "new_state": new_state,
                "improved_distance": improved,
                "retained": retained, "retained_prefix_id": retained_prefix_id,
            })).expect("failed to append solver seed event");
        }
        let novel = semantic_now.difference(&self.last_semantic).next().is_some();
        if novel {
            self.last_semantic = semantic_now;
            if !solver_seed && self.prefixes.len() < self.retained_prefix_limit {
                self.remember(input.clone(), self.current_path.clone(), distance, "state_novelty");
            }
        } else if !solver_seed && self.prefixes.len() < self.retained_prefix_limit && self.stats.executions % 64 == 0 {
            self.remember(input.clone(), self.current_path.clone(), distance, "random");
        }
        for (task_id, runtime_id) in hit_tasks {
            let first_hit = frame_feedback.iter().find(|frame| frame.goal_hits.contains(&runtime_id));
            write_candidate(&self.run_dir, &self.current_path, "semantic_goal", Some(&task_id), Some(runtime_id))
                .map_err(|e| e.to_string())?;
            self.stats.candidates += 1;
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "goal_reached", "task_id": task_id, "runtime_id": runtime_id,
                "depth": first_hit.map_or(input.frames.len(), |frame| frame.frame_index + 1),
                "logical_time_ns": first_hit.map(|frame| frame.logical_time_ns),
                "exit_kind": first_hit.map_or_else(|| "Ok".to_string(), |frame| frame.exit_kind.clone()), "source": "fuzz",
            })).expect("failed to append goal reached event");
        }
        if self.started.elapsed().as_secs_f64() >= 10.0 && self.last_stats.elapsed().as_secs_f64() >= 10.0 {
            self.last_stats = Instant::now();
            append_event(&self.metrics_file, serde_json::json!({
                "kind": "stats", "executions": self.stats.executions, "scans": self.stats.scans,
                "suffix_scans": self.stats.suffix_scans, "cache_hits": self.cache_hits(),
                "saved_scans": self.cache_saved(), "candidates": self.stats.candidates,
                "active_tasks": self.bundle.tasks.tasks.len(), "prefixes": self.prefixes.len(),
            })).expect("failed to append engine stats event");
        }
        self.stats.action_counts.insert("prefix", self.prefixes.len() as u64);
        Ok(())
    }

    pub fn cache_hits(&self) -> u64 { self.stats.cache_hits }
    pub fn cache_saved(&self) -> u64 { self.stats.saved_scans }

    pub fn record_cache(&mut self, hits: u64, saved: u64, attempts: u64,
                        validations: u64, invalidations: u64) {
        self.stats.cache_hits = hits;
        self.stats.saved_scans = saved;
        self.stats.snapshot_attempts = attempts;
        self.stats.snapshot_validations = validations;
        self.stats.snapshot_invalidations = invalidations;
    }

    pub fn record_execution(&self, task_id: Option<&str>, outcome: Option<&ExecOutcome>) {
        let Some(outcome) = outcome else { return; };
        append_event(&self.metrics_file, serde_json::json!({
            "kind": "execution",
            "task_id": task_id,
            "frames": outcome.frames,
            "prefix_depth": outcome.prefix_depth,
            "suffix_frames": outcome.suffix,
            "scans": outcome.scans,
            "restored": outcome.restored,
            "provenance": outcome.provenance,
            "exit_kind": outcome.exit_kind,
        })).expect("failed to append execution event");
    }

    pub fn write_summary(&self, total: Duration, budget: Duration) -> Result<(), String> {
        let task_progress = self.bundle.tasks.tasks.iter().map(|task| {
            let used = self.task_attempts.get(&task.task_id).copied().unwrap_or(0);
            let limit = task.budget.as_ref().and_then(|value| value.get("attempts"))
                .and_then(serde_json::Value::as_u64);
            serde_json::json!({
                "task_id": task.task_id,
                "target_type": task.goal_class,
                "reached": self.stats.reached.contains(&task.task_id),
                "best_prefix_id": self.task_best_prefix.get(&task.task_id),
                "best_distance": self.best_distance.get(&task.task_id),
                "last_distance_improvement": self.task_distance_change.get(&task.task_id),
                "native_runtime_novelty": self.task_novel_hits.get(&task.task_id).copied().unwrap_or(0),
                "native_state_novelty": self.task_novel_states.get(&task.task_id).copied().unwrap_or(0),
                "consecutive_no_progress": self.task_stagnation.get(&task.task_id).copied().unwrap_or(0),
                "fuzz_attempts_used": used,
                "fuzz_attempts_remaining": limit.map(|max| max.saturating_sub(used)),
                "remaining_time_s": budget.saturating_sub(total).as_secs_f64(),
            })
        }).collect::<Vec<_>>();
        let summary = serde_json::json!({
            "executions": self.stats.executions,
            "scans": self.stats.scans,
            "suffix_scans": self.stats.suffix_scans,
            "cache_hits": self.stats.cache_hits,
            "saved_scans": self.stats.saved_scans,
            "snapshot_attempts": self.stats.snapshot_attempts,
            "snapshot_misses": self.stats.snapshot_attempts.saturating_sub(self.stats.cache_hits),
            "snapshot_validations": self.stats.snapshot_validations,
            "snapshot_invalidations": self.stats.snapshot_invalidations,
            "candidates": self.stats.candidates,
            "solver_seeds_loaded": self.stats.solver_seeds_loaded,
            "solver_seeds_executed": self.stats.solver_seeds_executed,
            "solver_seeds_retained": self.stats.solver_seeds_retained,
            "reached": self.stats.reached,
            "task_progress": task_progress,
            "ended": self.ended,
            "elapsed_s": total.as_secs_f64(),
            "budget_s": budget.as_secs_f64(),
            "action_counts": self.stats.action_counts,
            "hold_actions": self.stats.hold_actions,
            "wait_actions": self.stats.wait_actions,
            "state_copies": self.stats.state_copies,
        });
        fs::create_dir_all(self.run_dir.join("metrics")).ok();
        fs::write(self.run_dir.join("metrics").join("summary.json"), serde_json::to_vec_pretty(&summary).unwrap())
            .map_err(|e| e.to_string())
    }
}

fn logical_time_ns(bundle: &V3Bundle, frames: &[Frame]) -> u64 {
    frames.iter().fold(0u64, |total, frame| {
        total.saturating_add(frame.dt_ns.unwrap_or(bundle.scan_period_ns))
    })
}

fn decode_hex_bytes(value: &str, size: usize) -> Vec<u8> {
    let trimmed = value.trim();
    let text = trimmed.strip_prefix("0x").or_else(|| trimmed.strip_prefix("0X")).unwrap_or(trimmed);
    let mut bytes = vec![0; size];
    for (index, pair) in text.as_bytes().chunks(2).take(size).enumerate() {
        let high = pair.first().and_then(|byte| (*byte as char).to_digit(16)).unwrap_or(0);
        let low = pair.get(1).and_then(|byte| (*byte as char).to_digit(16)).unwrap_or(0);
        bytes[index] = ((high << 4) | low) as u8;
    }
    bytes
}

fn encode_hex_bytes(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(2 + bytes.len() * 2);
    text.push_str("0x");
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(text, "{byte:02x}");
    }
    text
}

pub fn load_seeds(dir: &Path, bundle: &V3Bundle) -> Vec<V3Input> {
    let mut result = Vec::new();
    let Ok(entries) = fs::read_dir(dir) else { return result };
    let mut paths = entries.flatten().map(|entry| entry.path()).filter(|path| path.is_file()).collect::<Vec<_>>();
    paths.sort();
    for path in paths {
        let Ok(bytes) = fs::read(&path) else { continue };
        let Ok(text) = std::str::from_utf8(&bytes) else { eprintln!("[v3] invalid UTF-8 seed: {}", path.display()); continue };
        let Ok(case) = parse_testcase(text, &bundle.schema()) else { eprintln!("[v3] invalid frame seed: {}", path.display()); continue };
        if case.frames.is_empty() {
            continue;
        }
        result.push(V3Input { frames: case.frames.clone(), shared: case.shared.clone() });
    }
    result
}

pub fn serialize(bundle: &V3Bundle, frames: &[Frame], shared: &BTreeMap<String, String>) -> Vec<u8> {
    let mut out = String::new();
    for (name, value) in shared {
        let token = bundle
            .abi
            .shared_fields
            .iter()
            .find(|field| field.name == *name)
            .map(|field| type_token(&field.spec))
            .unwrap_or_default();
        out.push_str(&format!("{},{},{}\n", name, token, value));
    }
    for (index, frame) in frames.iter().enumerate() {
        for field in bundle.abi.action_fields() {
            let value = frame
                .values
                .get(&field.name)
                .cloned()
                .unwrap_or_else(|| default_for(&field.spec));
            out.push_str(&format!("{}.{},{},{}\n", index, field.name, type_token(&field.spec), value));
        }
        if let Some(dt) = frame.dt_ns {
            out.push_str(&format!("{}.__DT,TIME,{}\n", index, dt));
        }
    }
    out.into_bytes()
}

pub fn append_event(path: &Path, value: serde_json::Value) -> Result<(), String> {
    use std::io::Write;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let mut file = fs::OpenOptions::new().create(true).append(true).open(path).map_err(|e| e.to_string())?;
    let line = format!("{value}\n");
    let written = file.write(line.as_bytes()).map_err(|e| e.to_string())?;
    if written != line.len() {
        return Err("short JSONL event write".into());
    }
    Ok(())
}

pub fn collect_literals(bundle: &V3Bundle) -> (Vec<i64>, Vec<f64>) {
    let mut ints = BTreeSet::new();
    let mut floats = BTreeSet::new();
    if let Ok(value) = serde_json::to_value(&bundle.tasks) {
        collect_numbers(&value, &mut ints, &mut floats);
    }
    (ints.into_iter().collect(), floats.into_iter().map(f64::from_bits).collect())
}

fn collect_numbers(value: &serde_json::Value, ints: &mut BTreeSet<i64>, floats: &mut BTreeSet<u64>) {
    match value {
        serde_json::Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                ints.insert(value);
            } else if let Some(value) = number.as_f64() {
                floats.insert(value.to_bits());
            }
        }
        serde_json::Value::Array(values) => values.iter().for_each(|value| collect_numbers(value, ints, floats)),
        serde_json::Value::Object(values) => values.values().for_each(|value| collect_numbers(value, ints, floats)),
        _ => {}
    }
}

/// The spec's name for the stage implemented above.
pub type GoalStage = V3GoalStage;

#[cfg(test)]
mod v3_stage_tests {
    use super::*;
    use std::path::PathBuf;

    fn bundle() -> V3Bundle {
        V3Bundle {
            dir: PathBuf::new(),
            abi: V3Abi {
                schema_version: "x".into(),
                target: V3Target { name: "F".into(), kind: "FUNCTION_BLOCK".into(), instance_size: 0 },
                frame: V3FrameLayout { size: 4, align: 2, fields: vec![] },
                frame_fields: vec![
                    V3Field { name: "A".into(), offset: Some(0), size: Some(1), c_name: "f0".into(), block_kind: "VAR_INPUT".into(), spec: V3Spec { kind: "scalar".into(), name: "BOOL".into(), c_type: Some("uint8_t".into()), size: Some(1), count: None, byte_size: Some(1), length: None, element: None } },
                    V3Field { name: "B".into(), offset: Some(2), size: Some(2), c_name: "f1".into(), block_kind: "VAR_INPUT".into(), spec: V3Spec { kind: "scalar".into(), name: "INT".into(), c_type: Some("int16_t".into()), size: Some(2), count: None, byte_size: Some(2), length: None, element: None } },
                ],
                shared_fields: vec![],
                state_fields: vec![],
            },
            layout: V3Layout { total_size: 0, snapshot_schema_version: 1, harness_region_index: None, regions: vec![] },
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            contract_id: "c".into(),
            scan_period_ns: 10,
        }
    }

    fn stage(no_time: bool, no_copy: bool) -> V3GoalStage {
        // Rust tests run in parallel; sharing a process-id-only directory lets
        // one test remove another test's metrics/prefix files.
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let dir = std::env::temp_dir().join(format!(
            "v3-stage-test-{}-{}-{nonce}",
            std::process::id(),
            no_time
        ));
        fs::create_dir_all(&dir).unwrap();
        V3GoalStage::new(bundle(), dir.clone(), &dir, vec![1, 2, 4, 8], vec![], 7, false, false, no_time, no_copy, false)
    }

    #[test]
    fn failed_execution_does_not_retain_unreachable_prefix_or_state() {
        for exit_kind in [libafl::executors::ExitKind::Crash, libafl::executors::ExitKind::Timeout] {
            let mut stage = stage(false, false);
            stage.next_input();
            stage.last_state.insert("B".into(), 7);
            stage.active_cache.push(1);
            let original_prefixes = stage.prefixes.len();
            stage.on_result(&exit_kind, &[], 0.0, None, None, BTreeMap::new(), None, &[]).unwrap();
            assert_eq!(stage.prefixes.len(), original_prefixes);
            assert!(stage.last_state.is_empty());
            assert!(stage.current.is_none());
            assert!(stage.current_path.is_empty());
            assert!(stage.active_cache.is_empty());
            assert_eq!(stage.stats.executions, 1);
            assert_eq!(stage.stats.candidates, if matches!(exit_kind, libafl::executors::ExitKind::Crash) { 1 } else { 0 });
        }
    }

    #[test]
    fn no_time_guidance_removes_hold_and_wait_actions() {
        let mut stage = stage(true, false);
        for _ in 0..2000 {
            stage.next_input();
        }
        assert_eq!(stage.stats.hold_actions, 0);
        assert_eq!(stage.stats.wait_actions, 0);
    }

    #[test]
    fn default_stage_exercises_hold_or_wait() {
        let mut stage = stage(false, false);
        for _ in 0..2000 {
            stage.next_input();
        }
        assert!(stage.stats.hold_actions + stage.stats.wait_actions > 0);
    }

    #[test]
    fn no_state_copy_keeps_the_counter_at_zero() {
        let mut stage = stage(false, true);
        stage.last_state.insert("A".into(), 1);
        for _ in 0..2000 {
            stage.next_input();
        }
        assert_eq!(stage.stats.state_copies, 0);
    }

    #[test]
    fn no_goal_prefix_and_no_distance_use_random_prefixes() {
        for (no_goal, no_distance) in [(true, false), (false, true)] {
            let mut stage = stage(false, false);
            stage.prefixes.push(V3Prefix {
                prefix_id: "p-test".into(),
                parent_id: Some("p-00000000".into()),
                task_id: None,
                input: V3Input {
                    frames: vec![Frame { index: 0, values: BTreeMap::new(), dt_ns: None }],
                    shared: BTreeMap::new(),
                },
                path: Vec::new(),
                distance: 1.0,
                source: "test",
                uses: 0,
            });
            stage.no_goal_prefix = no_goal;
            stage.no_distance = no_distance;
            for _ in 0..100 {
                stage.next_input();
            }
            assert!(stage.stats.random_prefix_picks > 0);
            assert_eq!(stage.stats.goal_prefix_picks, 0);
        }
    }

    #[test]
    fn no_snapshot_disables_the_cache() {
        let cache = PrefixCache::new(false);
        assert!(!cache.enabled);
        assert!(PrefixCache::new(true).enabled);
    }

    #[test]
    fn execution_event_records_actual_cache_provenance_and_counts() {
        let stage = stage(false, false);
        let outcome = ExecOutcome {
            frames: 12,
            suffix: 7,
            prefix_depth: 5,
            restored: true,
            scans: 7,
            provenance: "snapshot".into(),
            observations: Vec::new(),
            exit_kind: "Ok".into(),
        };
        stage.record_execution(Some("task-1"), Some(&outcome));
        let line = fs::read_to_string(&stage.metrics_file).unwrap();
        let event: serde_json::Value = serde_json::from_str(line.lines().last().unwrap()).unwrap();
        assert_eq!(event["kind"], "execution");
        assert_eq!(event["task_id"], "task-1");
        assert_eq!(event["prefix_depth"], 5);
        assert_eq!(event["suffix_frames"], 7);
        assert_eq!(event["scans"], 7);
        assert_eq!(event["provenance"], "snapshot");
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn prefix_logical_time_uses_each_frame_delta() {
        let bundle = bundle();
        let frames = vec![
            Frame { index: 0, values: BTreeMap::new(), dt_ns: Some(11) },
            Frame { index: 1, values: BTreeMap::new(), dt_ns: None },
            Frame { index: 2, values: BTreeMap::new(), dt_ns: Some(25) },
        ];
        assert_eq!(logical_time_ns(&bundle, &frames), 46);
    }

    #[test]
    fn reachable_prefix_is_published_to_klee_queue_with_task_context() {
        let mut stage = stage(false, false);
        stage.active_task_id = Some("task-array".into());
        stage.task_stagnation.insert("task-array".into(), 7);
        let input = V3Input {
            frames: vec![Frame { index: 0, values: BTreeMap::new(), dt_ns: Some(17) }],
            shared: BTreeMap::new(),
        };
        let path = serialize(&stage.bundle, &input.frames, &input.shared);
        stage.remember(input, path, 0.25, "fuzz");
        let line = fs::read_to_string(&stage.solver_queue_file).unwrap();
        let record: serde_json::Value = serde_json::from_str(line.lines().next().unwrap()).unwrap();
        assert_eq!(record["queue"], "klee_suffix");
        assert_eq!(record["task_id"], "task-array");
        assert_eq!(record["prefix_id"], "p-1");
        assert_eq!(record["depth"], 1);
        assert_eq!(record["task_unresolved"], true);
        assert_eq!(record["trigger"], "task_distance_improved");
        assert_eq!(record["consecutive_no_progress"], 7);
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn matching_native_hazard_reach_queues_only_its_unresolved_task() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks = vec![serde_json::from_value(serde_json::json!({
            "task_id": "task-reach", "goal_class": "defect",
            "goal": {"runtime_id": 5, "kind": "hazard_violation"},
            "reach_runtime_ids": [3],
        })).unwrap()];
        stage.next_input();
        stage.active_task_id = Some("task-reach".into());
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 0, 0, 1, 0, 0],
                        f64::INFINITY, None, Some("task-reach"), BTreeMap::new(), None, &[]).unwrap();
        let line = fs::read_to_string(&stage.solver_queue_file).unwrap();
        let record: serde_json::Value = serde_json::from_str(line.lines().next().unwrap()).unwrap();
        assert_eq!(record["task_id"], "task-reach");
        assert_eq!(record["trigger"], "native_hazard_reach");
        assert_eq!(record["native_execution"], 1);
        assert_eq!(stage.seen_task_reaches.len(), 1);
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn single_cycle_stage_never_grows_a_history_or_queues_suffixes() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.single_cycle = true;
        let input = V3Input {
            frames: vec![
                Frame { index: 0, values: BTreeMap::new(), dt_ns: Some(11) },
                Frame { index: 1, values: BTreeMap::new(), dt_ns: Some(23) },
            ],
            shared: BTreeMap::new(),
        };
        stage.pending_solver_seeds.push_back((1, input, None, None));
        let first = String::from_utf8(stage.next_input()).unwrap();
        assert!(first.contains("0.__DT,TIME,23"));
        assert!(!first.contains("1."));
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 0], 2.0, Some(0),
                        None, BTreeMap::new(), None, &[]).unwrap();
        for _ in 0..64 {
            let candidate = String::from_utf8(stage.next_input()).unwrap();
            assert!(!candidate.contains("1."));
        }
        assert!(!stage.solver_queue_file.exists());
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn unknown_effects_bound_multiframe_fallback() {
        let mut target = bundle();
        target.tasks.unknown_cycle_effects = true;
        let root = std::env::temp_dir().join(format!("v3-unknown-{}-{}",
            std::process::id(), current_nanos()));
        let stage = V3GoalStage::new(target, root.clone(), &root, vec![], vec![], 7,
            false, false, false, false, false);
        assert_eq!(stage.max_depth, 4);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn all_existing_seeds_enter_the_initial_execution_queue() {
        let root = std::env::temp_dir().join(format!("v3-seed-queue-{}-{}", std::process::id(), current_nanos()));
        let seeds = root.join("seeds");
        fs::create_dir_all(&seeds).unwrap();
        for index in 0..70 {
            fs::write(seeds.join(format!("{index:03}.seed")), format!("0.A,BOOL,1\n0.B,INT,{index}\n")).unwrap();
        }
        let mut stage = V3GoalStage::new(bundle(), root.clone(), &seeds, vec![], vec![], 7,
                                         false, false, false, false, false);
        assert_eq!(stage.seeds.len(), 70);
        for index in 0..70 {
            let input = String::from_utf8(stage.next_input()).unwrap();
            assert!(input.contains(&format!("0.B,INT,{index}\n")));
        }
        assert_eq!(stage.stats.action_counts["seed"], 70);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn solver_seeds_arrive_during_fuzzing_and_become_mutation_prefixes() {
        let mut stage = stage(false, false);
        let directory = stage.run_dir.join("solver").join("reinject");
        fs::create_dir_all(&directory).unwrap();
        let first = b"0.A,BOOL,1\n0.B,INT,7\n0.__DT,TIME,17\n";
        fs::write(directory.join("solver-seed-00000001.seed"), first).unwrap();
        fs::write(directory.join("solver-seed-00000002.seed"), b"invalid\n").unwrap();
        fs::write(directory.join("solver-seed-00000003.seed"), b"0.A,BOOL,0\n0.B,INT,9\n").unwrap();

        let executed = stage.next_input();
        assert_eq!(executed, first);
        assert_eq!(stage.stats.solver_seeds_loaded, 2);
        assert_eq!(stage.stats.solver_seeds_executed, 1);
        assert!(stage.current_solver_seed);
        stage.active_task_id = Some("task-other".into());
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 1], 0.0, Some(0),
                        Some("task-other"), BTreeMap::new(), None, &[]).unwrap();
        assert_eq!(stage.stats.solver_seeds_retained, 1);
        assert!(stage.prefixes.iter().any(|prefix|
            prefix.source == "solver" && prefix.input.frames[0].dt_ns == Some(17)));
        assert_eq!(stage.next_input(), b"0.A,BOOL,0\n0.B,INT,9\n".to_vec());
        assert_eq!(stage.stats.solver_seeds_executed, 2);

        stage.pending_solver_seeds.clear();
        let mut picked_solver = false;
        for _ in 0..25 {
            if stage.choose_prefix().source == "solver" {
                picked_solver = true;
                break;
            }
        }
        assert!(picked_solver);
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn solver_seed_metadata_keeps_the_query_task_for_distance_feedback() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks.push(serde_json::from_value(serde_json::json!({
            "task_id": "task-reach", "goal": {}
        })).unwrap());
        let directory = stage.run_dir.join("solver").join("reinject");
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("solver-seed-00000001.json"),
            r#"{"query_id":"live-prefix-p-7-9","task_id":"task-reach"}"#).unwrap();
        fs::write(directory.join("solver-seed-00000001.seed"),
            b"0.A,BOOL,1\n0.B,INT,7\n").unwrap();
        stage.next_input();
        assert_eq!(stage.current_solver_task_id(), Some("task-reach"));
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 1], 0.0, Some(0),
                        Some("task-reach"), BTreeMap::new(), None, &[]).unwrap();
        let events = fs::read_to_string(stage.run_dir.join("metrics").join("events.jsonl")).unwrap();
        assert!(events.contains("live-prefix-p-7-9"));
        assert!(events.contains("\"task_id\":\"task-reach\""));
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn arriving_solver_seed_preempts_unexecuted_initial_seed() {
        let mut stage = stage(false, false);
        stage.seeds.push(V3Input { frames: vec![Frame { index: 0,
            values: [("A".into(), "0".into()), ("B".into(), "3".into())].into(),
            dt_ns: None }], shared: BTreeMap::new() });
        let directory = stage.run_dir.join("solver").join("reinject");
        fs::create_dir_all(&directory).unwrap();
        let solver = b"0.A,BOOL,1\n0.B,INT,7\n";
        fs::write(directory.join("solver-seed-00000001.seed"), solver).unwrap();
        assert_eq!(stage.next_input(), solver);
        assert!(stage.current_solver_seed);
        assert_eq!(stage.next_input(), b"0.A,BOOL,0\n0.B,INT,3\n");
        assert!(!stage.current_solver_seed);
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn solver_seed_without_new_native_feedback_is_not_retained() {
        let mut stage = stage(false, false);
        stage.next_input();
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 1], f64::INFINITY, None,
                        None, BTreeMap::new(), None, &[]).unwrap();
        let directory = stage.run_dir.join("solver").join("reinject");
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("solver-seed-00000001.seed"), b"0.A,BOOL,1\n0.B,INT,7\n").unwrap();
        stage.next_input();
        stage.on_result(&libafl::executors::ExitKind::Ok, &[0, 1], f64::INFINITY, None,
                        None, BTreeMap::new(), None, &[]).unwrap();
        assert_eq!(stage.stats.solver_seeds_executed, 1);
        assert_eq!(stage.stats.solver_seeds_retained, 0);
        assert!(!stage.prefixes.iter().any(|prefix| prefix.source == "solver"));
        fs::remove_dir_all(stage.run_dir).unwrap();
    }

    #[test]
    fn planned_prefixes_rotate_within_task_and_leave_room_for_learned_prefixes() {
        let root = std::env::temp_dir().join(format!(
            "v3-planned-prefix-{}-{}", std::process::id(), current_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let mut target = bundle();
        target.tasks.tasks = vec![
            serde_json::from_value(serde_json::json!({
                "task_id": "task-a", "goal": {"runtime_id": 1},
                "seed_prefixes": (0..520).map(|_| vec![serde_json::json!({"A": "0", "B": "1"})])
                    .collect::<Vec<_>>()
            })).unwrap(),
            serde_json::from_value(serde_json::json!({
                "task_id": "task-b", "goal": {"runtime_id": 2},
                "seed_prefixes": [[{"A": "1", "B": "2"}]]
            })).unwrap(),
        ];
        let mut stage = V3GoalStage::new(target, root.clone(), &root, vec![], vec![], 7,
                                         false, false, false, false, false);
        assert_eq!(stage.prefixes.len(), 522);
        stage.active_task_id = Some("task-a".into());
        let mut visited = BTreeSet::new();
        for _ in 0..1000 {
            let prefix = stage.choose_prefix();
            if prefix.input.frames.is_empty() { continue; }
            assert_eq!(prefix.task_id.as_deref(), Some("task-a"));
            visited.insert(prefix.prefix_id);
        }
        assert_eq!(visited.len(), 520);
        assert!(stage.prefixes.iter().filter(|prefix| prefix.task_id.as_deref() == Some("task-a"))
            .all(|prefix| prefix.uses > 0));
        let input = V3Input {
            frames: vec![Frame { index: 0, values: BTreeMap::new(), dt_ns: None }],
            shared: BTreeMap::new(),
        };
        let path = serialize(&stage.bundle, &input.frames, &input.shared);
        stage.remember(input, path, 0.5, "fuzz");
        assert_eq!(stage.prefixes.len(), 523);
        assert_eq!(stage.prefixes.last().unwrap().task_id.as_deref(), Some("task-a"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn scheduler_visits_exploration_and_defect_tasks_with_attempt_limits() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks = ["exploration", "defect"].into_iter().enumerate().map(|(index, class)| {
            serde_json::from_value(serde_json::json!({
                "task_id": format!("task-{index}"), "goal_class": class,
                "goal": {"runtime_id": index + 1, "stable_id": null, "kind": null, "hazard": null},
                "budget": {"attempts": 1}, "controllable_fields": ["B"]
            })).unwrap()
        }).collect();
        assert_eq!(stage.select_task().as_deref(), Some("task-0"));
        stage.task_attempts.insert("task-0".into(), 1);
        assert_eq!(stage.select_task().as_deref(), Some("task-1"));
        stage.task_attempts.insert("task-1".into(), 1);
        assert!(stage.select_task().is_none());
    }

    #[test]
    fn all_goals_early_stop_ignores_non_observable_control_tasks() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks = vec![
            serde_json::from_value(serde_json::json!({
                "task_id": "explore", "goal_class": "exploration",
                "goal": {"runtime_id": null, "stable_id": null, "kind": null, "hazard": null}
            })).unwrap(),
            serde_json::from_value(serde_json::json!({
                "task_id": "hazard", "goal_class": "defect",
                "goal": {"runtime_id": 7, "stable_id": null, "kind": null, "hazard": "array_access"}
            })).unwrap(),
        ];
        assert!(!stage.all_goals_reached());
        stage.stats.reached.insert("hazard".into());
        assert!(stage.all_goals_reached());
    }

    #[test]
    fn repeated_semantic_goal_hit_writes_one_candidate() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks = vec![serde_json::from_value(serde_json::json!({
            "task_id": "hazard", "goal_class": "defect",
            "goal": {"runtime_id": 0, "stable_id": null, "kind": null,
                     "hazard": "division", "modeling": "exact"}
        })).unwrap()];
        for _ in 0..2 {
            stage.next_input();
            stage.on_result(&libafl::executors::ExitKind::Ok, &[1], 0.0, None,
                Some("hazard"), BTreeMap::new(), None, &[]).unwrap();
        }
        assert_eq!(stage.stats.candidates, 1);
        assert!(stage.stats.reached.contains("hazard"));
    }

    #[test]
    fn conservative_goal_reaches_without_claiming_a_candidate() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks = vec![serde_json::from_value(serde_json::json!({
            "task_id": "hazard", "goal_class": "defect",
            "goal": {"runtime_id": 0, "stable_id": null, "kind": null,
                     "hazard": "division", "modeling": "conservative"}
        })).unwrap()];
        stage.next_input();
        stage.on_result(&libafl::executors::ExitKind::Ok, &[1], 0.0, None,
            Some("hazard"), BTreeMap::new(), None, &[]).unwrap();
        assert!(stage.stats.reached.contains("hazard"));
        assert_eq!(stage.stats.candidates, 0);
    }

    #[test]
    fn empty_task_bundle_is_a_valid_native_only_schedule() {
        let mut stage = stage(false, false);
        stage.bundle.tasks.tasks.clear();
        assert!(stage.select_task().is_none());
        assert!(stage.active_task_id.is_none());
    }

    #[test]
    fn unsigned_mutation_and_state_copy_keep_the_full_domain() {
        let unsigned = V3Spec { kind: "scalar".into(), name: "ULINT".into(), c_type: Some("uint64_t".into()),
                                size: Some(8), count: None, byte_size: Some(8), length: None, element: None };
        assert_eq!(integer_bounds(&unsigned), Some((0, u64::MAX as i128)));
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields = vec![V3Field {
            name: "U".into(), offset: Some(0), size: Some(8), c_name: "f0".into(),
            block_kind: "VAR_INPUT".into(), spec: unsigned,
        }];
        let mut frame = stage.default_frame();
        for _ in 0..200 {
            stage.mutate_frame(&mut frame);
            frame.values["U"].parse::<u64>().expect("ULINT mutation must stay in range");
        }
        stage.last_state.insert("S".into(), u64::MAX as i128);
        let mut frames = vec![stage.default_frame()];
        assert!(stage.maybe_state_copy(&mut frames));
        assert_eq!(frames[0].values["U"], u64::MAX.to_string());
        stage.bundle.abi.frame_fields[0].spec.size = Some(2);
        stage.bundle.abi.frame_fields[0].spec.name = "UINT".into();
        assert!(!stage.maybe_state_copy(&mut frames));
    }

    #[test]
    fn array_mutation_preserves_abi_size_and_changes_element_bytes() {
        let element = V3Spec {
            kind: "scalar".into(),
            name: "LREAL".into(),
            c_type: Some("double".into()),
            size: Some(8),
            count: None,
            byte_size: Some(8),
            length: None,
            element: None,
        };
        let array = V3Spec {
            kind: "array".into(),
            name: "ARRAY OF LREAL".into(),
            c_type: None,
            size: None,
            count: Some(8),
            byte_size: Some(64),
            length: None,
            element: Some(Box::new(element)),
        };
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields = vec![V3Field {
            name: "InRecv".into(), offset: Some(0), size: Some(64), c_name: "f0".into(),
            block_kind: "VAR_IN_OUT".into(), spec: array,
        }];
        let mut frame = stage.default_frame();
        stage.mutate_frame(&mut frame);
        let value = frame.values.get("InRecv").unwrap();
        assert_eq!(value.len(), 130);
        assert!(value.starts_with("0x"));
        assert_ne!(value.as_str(), "0x".to_string() + &"00".repeat(64));

        stage.bundle.abi.frame_fields[0].spec.byte_size = Some(4);
        stage.bundle.abi.frame_fields[0].spec.size = Some(4);
        let mut malformed = stage.default_frame();
        stage.mutate_frame(&mut malformed);
        assert_eq!(malformed.values["InRecv"].len(), 10);
    }

    #[test]
    fn float_mutation_candidates_include_literal_interval_midpoints() {
        let mut stage = stage(false, false);
        stage.floats = vec![1.1, 1.5, 2.14, 5.0, 6.5];
        let candidates = stage.float_candidates();
        assert!(candidates.contains(&1.3));
        assert!(candidates.contains(&5.75));
        assert!(candidates.contains(&2.14));
    }

    #[test]
    fn pointer_and_string_mutations_stay_within_abi_domains() {
        let pointer = V3Spec {
            kind: "pointer".into(),
            name: "REF_TO BYTE".into(),
            c_type: None,
            size: Some(8),
            count: None,
            byte_size: Some(8),
            length: None,
            element: Some(Box::new(V3Spec {
                kind: "scalar".into(), name: "BYTE".into(), c_type: Some("uint8_t".into()),
                size: Some(1), count: None, byte_size: Some(1), length: None, element: None,
            })),
        };
        let string = V3Spec {
            kind: "string".into(), name: "WSTRING".into(), c_type: Some("uint16_t".into()),
            size: Some(2), count: None, byte_size: Some(10), length: Some(4), element: None,
        };
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields = vec![
            V3Field { name: "P".into(), offset: Some(0), size: Some(8), c_name: "f0".into(), block_kind: "VAR_INPUT".into(), spec: pointer },
            V3Field { name: "Text".into(), offset: Some(8), size: Some(10), c_name: "f1".into(), block_kind: "VAR_INPUT".into(), spec: string },
        ];
        let mut frame = stage.default_frame();
        let mut pointer_changed = false;
        let mut string_values = Vec::new();
        for _ in 0..64 {
            stage.mutate_frame(&mut frame);
            let pointer_value = frame.values["P"].clone();
            let string_value = frame.values["Text"].clone();
            assert!(pointer_value.starts_with("0x"));
            assert_eq!(pointer_value.len(), 4);
            assert!(string_value.chars().count() <= 4);
            pointer_changed |= pointer_value != "0x00";
            string_values.push(string_value);
        }
        assert!(pointer_changed);
        assert!(string_values.iter().any(|value| value.is_empty() || value == "ZZZZ"));
        let wstring_spec = stage.bundle.abi.frame_fields[1].spec.clone();
        let truncated = stage.mutate_string_value(&wstring_spec, "😀😀A");
        assert!(truncated.encode_utf16().count() <= 4);
    }

    #[test]
    fn concurrent_event_appends_keep_jsonl_records_intact() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let path = std::env::temp_dir().join(format!(
            "v3-events-{}-{nonce}.jsonl", std::process::id()
        ));
        let workers = (0..8).map(|worker| {
            let path = path.clone();
            std::thread::spawn(move || {
                for sequence in 0..100 {
                    append_event(&path, serde_json::json!({
                        "worker": worker, "sequence": sequence, "payload": "x".repeat(512),
                    })).unwrap();
                }
            })
        }).collect::<Vec<_>>();
        for worker in workers {
            worker.join().unwrap();
        }
        let lines = fs::read_to_string(&path).unwrap();
        assert_eq!(lines.lines().count(), 800);
        for line in lines.lines() {
            serde_json::from_str::<serde_json::Value>(line).unwrap();
        }
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn pulse_fields_follow_the_active_goal() {
        let mut stage = stage(false, false);
        let mut reset = stage.bundle.abi.frame_fields[0].clone();
        reset.name = "Reset".into();
        stage.bundle.abi.frame_fields.push(reset);
        stage.bundle.tasks.tasks.push(serde_json::from_value(serde_json::json!({
            "task_id": "reset-edge", "goal": {}, "edge_inputs": ["Reset"],
            "controllable_fields": ["A", "Reset"],
        })).unwrap());
        stage.active_task_id = Some("reset-edge".into());
        assert_eq!(stage.boolean_fields(), vec!["Reset"]);
    }

    #[test]
    fn pulse_train_can_cross_a_reset_and_hold_integer_guards() {
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields[0].name = "Sample".into();
        stage.bundle.abi.frame_fields[1].name = "Code".into();
        let mut reset = stage.bundle.abi.frame_fields[0].clone();
        reset.name = "Reset".into();
        stage.bundle.abi.frame_fields.push(reset);
        let mut frames = Vec::new();
        stage.append_pulse_train(&mut frames, "Sample", Some("Reset"), 8, true, None);
        assert_eq!(frames.len(), 18);
        assert_eq!(frames[0].values["Sample"], "1");
        assert_eq!(frames[1].values["Reset"], "1");
        assert!(frames.iter().all(|frame| frame.values["Code"] == "1"));
        assert!(frames.iter().skip(2).all(|frame| frame.values["Reset"] == "0"));
        assert_eq!(frames.iter().skip(2).filter(|frame| frame.values["Sample"] == "1").count(), 8);
    }

    #[test]
    fn timed_pulse_train_keeps_each_frame_delta() {
        let stage = stage(false, false);
        let mut frames = Vec::new();
        stage.append_pulse_train(&mut frames, "A", None, 8, false, Some(1_000_000_000));
        assert_eq!(frames.len(), 16);
        assert!(frames.iter().all(|frame| frame.dt_ns == Some(1_000_000_000)));
        assert_eq!(frames.iter().filter(|frame| frame.values["A"] == "1").count(), 8);
    }

    #[test]
    fn long_hold_uses_goal_guard_and_clears_reset() {
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields[0].name = "Run".into();
        stage.bundle.abi.frame_fields[1].name = "Speed".into();
        let mut reset = stage.bundle.abi.frame_fields[0].clone();
        reset.name = "ResetService".into();
        stage.bundle.abi.frame_fields.push(reset);
        stage.bundle.tasks.tasks.push(serde_json::from_value(serde_json::json!({
            "task_id": "counter", "goal": {},
            "controllable_fields": ["Run", "Speed", "ResetService"],
            "abstraction": [{"field": "Speed", "thresholds": [1, 1450, 1550]}],
        })).unwrap());
        stage.active_task_id = Some("counter".into());
        let mut frames = vec![stage.default_frame()];
        frames[0].values.insert("ResetService".into(), "1".into());
        stage.long_hold(&mut frames);
        assert!(matches!(frames.len(), 33 | 65 | 129));
        assert!(frames[1..].iter().all(|frame| frame.values["Run"] == "1"
            && frame.values["Speed"] == "1450"
            && frame.values["ResetService"] == "0"));
    }

    #[test]
    fn timer_train_advances_virtual_time_and_resets_between_cycles() {
        let mut stage = stage(false, false);
        stage.bundle.abi.frame_fields[1].name = "Pressure".into();
        stage.bundle.tasks.tasks.push(serde_json::from_value(serde_json::json!({
            "task_id": "timed", "goal": {}, "controllable_fields": ["Pressure"],
            "timers": [{"name": "T", "pt_const_ns": 500_000_000}],
            "abstraction": [{"field": "Pressure", "thresholds": [800]}],
        })).unwrap());
        stage.active_task_id = Some("timed".into());
        let mut frames = Vec::new();
        stage.timer_train(&mut frames);
        assert!(matches!(frames.len(), 3 | 6 | 12 | 15));
        for cycle in frames.chunks_exact(3) {
            assert!(matches!(cycle[0].values["Pressure"].as_str(), "800" | "801"));
            assert_eq!(cycle[0], cycle[1]);
            assert_eq!(cycle[0].dt_ns, Some(500_000_000));
            assert_eq!(cycle[2].values["Pressure"], "0");
            assert_eq!(cycle[2].dt_ns, Some(stage.bundle.scan_period_ns));
        }
    }
}
