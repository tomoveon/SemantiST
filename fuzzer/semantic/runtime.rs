use libafl::SerdeAny;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashSet};

pub const PLAN_SCHEMA: &str = "semantist.semantic-task-plan/1.0.0";
const FAILURE_DECAY_RHO: f64 = 0.25;
const DEFER_AFTER_FAILURES: u64 = 3;
const TOP_SEEDS_PER_TASK: usize = 4;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticTaskPlan {
    pub schema_version: String,
    #[serde(default)]
    pub stg_schema_version: String,
    #[serde(default)]
    pub runtime_id_schema_version: String,
    #[serde(default)]
    pub model_sha256: String,
    #[serde(default)]
    pub target_identity: String,
    #[serde(default)]
    pub tasks: Vec<SemanticTask>,
    #[serde(default)]
    pub normalization_by_pou: BTreeMap<String, EffortNormalization>,
    #[serde(default)]
    pub sccs: BTreeMap<String, Vec<Vec<String>>>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticTask {
    pub task_id: String,
    pub task_kind: String,
    pub pou: String,
    pub terminal_target_id: String,
    #[serde(default)]
    pub candidate_paths: Vec<CandidatePath>,
    #[serde(default)]
    pub ordered_waypoints: Vec<String>,
    #[serde(default)]
    pub control_obligations: Vec<ControlObligation>,
    #[serde(default)]
    pub data_dependencies: Vec<DataDependency>,
    #[serde(default)]
    pub state_transfer_chains: Vec<StateTransfer>,
    #[serde(default)]
    pub temporal_requirements: Vec<TemporalRequirement>,
    #[serde(default)]
    pub hazard_prerequisites: Vec<String>,
    #[serde(default)]
    pub controllable_seed_fields: Vec<ControllableSeedField>,
    #[serde(default)]
    pub modeling_status: String,
    #[serde(default)]
    pub initial_state: String,
    #[serde(default)]
    pub unlocked_targets: Vec<String>,
    #[serde(default)]
    pub terminal_runtime_id: Option<u32>,
    #[serde(default)]
    pub terminal_kind: Option<String>,
    #[serde(default)]
    pub terminal_hazard: Option<String>,
    #[serde(default)]
    pub terminal_goal_expression_id: Option<String>,
    #[serde(default)]
    pub terminal_goal_expression: Option<serde_json::Value>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CandidatePath {
    pub path_id: String,
    #[serde(default)]
    pub target_ids: Vec<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub node_ids: Vec<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub edge_ids: Vec<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub scc_ids: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ControlObligation {
    pub target_id: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub evaluation_edge_id: String,
    #[serde(default)]
    pub goal_expression_id: Option<String>,
    #[serde(default)]
    pub goal_expression: Option<serde_json::Value>,
    #[serde(default)]
    pub modeling_status: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct DataDependency {
    pub symbol: String,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub type_name: Option<String>,
    #[serde(default)]
    pub configuration_source: Option<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub def_use: Vec<serde_json::Value>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct StateTransfer {
    pub edge_id: String,
    #[serde(default)]
    pub reads: Vec<String>,
    #[serde(default)]
    pub writes: Vec<String>,
    #[serde(default)]
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub versions_in: BTreeMap<String, u64>,
    #[serde(default)]
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub versions_out: BTreeMap<String, u64>,
    #[serde(default)]
    pub operations: Vec<serde_json::Value>,
    #[serde(default)]
    pub modeling_status: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct TemporalRequirement {
    pub edge_id: String,
    #[serde(default)]
    pub persistent_symbols: Vec<String>,
    #[serde(default)]
    pub minimum_cycles: usize,
    #[serde(default)]
    pub modeling_status: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ControllableSeedField {
    pub symbol: String,
    #[serde(default)]
    pub seed_field: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub type_name: Option<String>,
    #[serde(default)]
    pub configuration_source: Option<String>,
    #[serde(default, rename = "type")]
    pub type_fact: Option<serde_json::Value>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct EffortNormalization {
    #[serde(default = "one")]
    pub ec: f64,
    #[serde(default = "one")]
    pub ed: f64,
    #[serde(default = "one")]
    pub es: f64,
    #[serde(default = "one")]
    pub et: f64,
    #[serde(default = "one")]
    pub eh: f64,
}

fn one() -> f64 {
    1.0
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SeedProfile {
    pub content_hash: String,
    #[serde(default)]
    pub covered_target_ids: Vec<String>,
    #[serde(default)]
    pub covered_targets_by_cycle: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub cycle_ids: Vec<usize>,
    #[serde(default)]
    pub state_signatures: Vec<String>,
    #[serde(default)]
    pub target_affinity: BTreeMap<String, f64>,
    #[serde(default)]
    pub parent_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Blocked,
    Ready,
    Active,
    Covered,
    Deferred,
    Unmapped,
}

impl Default for TaskStatus {
    fn default() -> Self {
        Self::Ready
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct TaskRuntimeState {
    pub status: TaskStatus,
    #[serde(default)]
    pub failed_attempts: u64,
    #[serde(default)]
    pub attempts: u64,
    #[serde(default)]
    pub deferred_at_coverage_epoch: Option<u64>,
    #[serde(default)]
    pub last_guide_target_id: Option<String>,
    #[serde(default)]
    pub priority: f64,
    #[serde(default)]
    pub base_priority: f64,
    #[serde(default)]
    pub gain: f64,
    #[serde(default)]
    pub minimum_effort: f64,
    #[serde(default)]
    pub best_seed_id: Option<String>,
    #[serde(default)]
    pub active_path: Vec<String>,
    #[serde(default)]
    pub guide_target_id: Option<String>,
    #[serde(default)]
    pub remaining_obligations: Vec<String>,
    #[serde(default)]
    pub prerequisites: Vec<String>,
    #[serde(default)]
    pub unlocked_targets: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct AttemptHistory {
    pub task_id: String,
    pub guide_target_id: Option<String>,
    pub seed_id: Option<String>,
    pub covered: bool,
    pub coverage_epoch: u64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct RankedSeed {
    pub content_hash: String,
    pub effort: f64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct RankingStatistics {
    pub ranking_count: u64,
    pub cache_hit_count: u64,
    pub full_rebuild_count: u64,
    pub incremental_seed_updates: u64,
    pub seed_profile_build_count: u64,
    pub corpus_scan_count: u64,
    pub rerank_reasons: BTreeMap<String, u64>,
}

#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
pub struct SemanticRuntimeMetadata {
    pub plan: SemanticTaskPlan,
    pub task_states: BTreeMap<String, TaskRuntimeState>,
    pub active_task_id: Option<String>,
    pub guide_target_id: Option<String>,
    pub active_path: Vec<String>,
    pub best_seed_id: Option<String>,
    pub covered_target_ids: BTreeSet<String>,
    pub coverage_epoch: u64,
    pub attempt_history: Vec<AttemptHistory>,
    pub ranking: Vec<String>,
    #[serde(default)]
    pub seed_profiles: BTreeMap<String, SeedProfile>,
    #[serde(default)]
    pub top_seeds_by_task: BTreeMap<String, Vec<RankedSeed>>,
    #[serde(default)]
    pub active_selection: Option<ActiveSelection>,
    #[serde(default = "default_true")]
    pub ranking_dirty: bool,
    #[serde(default)]
    pub corpus_epoch: u64,
    #[serde(default)]
    pub ranking_statistics: RankingStatistics,
    #[serde(default)]
    pub last_rerank_reason: Option<String>,
}

fn default_true() -> bool {
    true
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ActiveSelection {
    pub task_id: String,
    pub terminal_target_id: String,
    pub guide_target_id: String,
    pub active_path: Vec<String>,
    pub remaining_obligations: Vec<String>,
    pub best_seed_id: Option<String>,
    pub priority: f64,
    pub control_obligation: Option<ControlObligation>,
    pub controllable_seed_fields: Vec<ControllableSeedField>,
    pub backward_dependencies: Vec<String>,
    pub hazard_prerequisites: Vec<String>,
    pub modeling_status: String,
    pub task_kind: String,
    pub target_kind: Option<String>,
    pub hazard_kind: Option<String>,
    pub goal_expression_id: Option<String>,
    pub goal_expression: Option<serde_json::Value>,
    pub cycle_hint: Option<u64>,
    pub state_transfer_chains: Vec<StateTransfer>,
    pub temporal_requirements: Vec<TemporalRequirement>,
}

impl SemanticRuntimeMetadata {
    pub fn new(plan: SemanticTaskPlan, mapped_targets: &HashSet<String>) -> Self {
        let task_states = plan
            .tasks
            .iter()
            .map(|task| {
                let status = if !mapped_targets.contains(&task.terminal_target_id) {
                    TaskStatus::Unmapped
                } else if !task_has_executable_path(task) {
                    TaskStatus::Blocked
                } else {
                    TaskStatus::Ready
                };
                (
                    task.task_id.clone(),
                    TaskRuntimeState {
                        status,
                        ..Default::default()
                    },
                )
            })
            .collect();
        Self {
            plan,
            task_states,
            ranking_dirty: true,
            last_rerank_reason: Some("initialization".to_string()),
            ..Default::default()
        }
    }

    pub fn mark_ranking_dirty(&mut self, reason: impl Into<String>) {
        let reason = reason.into();
        self.ranking_dirty = true;
        self.last_rerank_reason = Some(reason.clone());
        *self
            .ranking_statistics
            .rerank_reasons
            .entry(reason)
            .or_default() += 1;
    }

    pub fn cache_seed_profile(&mut self, profile: SeedProfile) -> bool {
        let hash = profile.content_hash.clone();
        let previous = self.seed_profiles.get(&hash).cloned();
        if previous.as_ref().is_some_and(|stored| {
            stored.covered_target_ids == profile.covered_target_ids
                && stored.covered_targets_by_cycle == profile.covered_targets_by_cycle
                && stored.cycle_ids == profile.cycle_ids
                && stored.state_signatures == profile.state_signatures
                && stored.target_affinity == profile.target_affinity
                && stored.parent_id == profile.parent_id
        }) {
            return false;
        }

        let first_seed = self.seed_profiles.is_empty();
        if previous.is_none() {
            self.corpus_epoch = self.corpus_epoch.saturating_add(1);
        }
        self.ranking_statistics.seed_profile_build_count = self
            .ranking_statistics
            .seed_profile_build_count
            .saturating_add(1);
        self.seed_profiles.insert(hash.clone(), profile.clone());

        let mut top_changed = false;
        let tasks = self.plan.tasks.clone();
        for task in &tasks {
            let affected = first_seed
                || task_affected_by_seed(task, &profile)
                || previous
                    .as_ref()
                    .is_some_and(|old| task_affected_by_seed(task, old));
            if !affected {
                continue;
            }
            let normalization = self
                .plan
                .normalization_by_pou
                .get(&task.pou)
                .cloned()
                .unwrap_or_default();
            let score = effort(&profile, task, &normalization);
            let top = self
                .top_seeds_by_task
                .entry(task.task_id.clone())
                .or_default();
            let before = top
                .iter()
                .map(|entry| (entry.content_hash.clone(), entry.effort))
                .collect::<Vec<_>>();
            top.retain(|entry| entry.content_hash != hash);
            top.push(RankedSeed {
                content_hash: hash.clone(),
                effort: score,
            });
            top.sort_by(|left, right| {
                left.effort
                    .total_cmp(&right.effort)
                    .then_with(|| left.content_hash.cmp(&right.content_hash))
            });
            top.truncate(TOP_SEEDS_PER_TASK);
            top_changed |= before
                != top
                    .iter()
                    .map(|entry| (entry.content_hash.clone(), entry.effort))
                    .collect::<Vec<_>>();
        }
        self.ranking_statistics.incremental_seed_updates = self
            .ranking_statistics
            .incremental_seed_updates
            .saturating_add(1);
        if first_seed {
            self.mark_ranking_dirty("initial_seed_profiles");
        } else if top_changed && seed_has_semantic_information(&profile) {
            self.mark_ranking_dirty("new_semantic_seed");
        }
        top_changed
    }

    pub fn invalidate_seed(&mut self, content_hash: &str) {
        if self.seed_profiles.remove(content_hash).is_none() {
            return;
        }
        for top in self.top_seeds_by_task.values_mut() {
            top.retain(|entry| entry.content_hash != content_hash);
        }
        if self.best_seed_id.as_deref() == Some(content_hash) {
            self.active_selection = None;
            self.mark_ranking_dirty("best_seed_invalidated");
        }
    }

    pub fn record_coverage(&mut self, target_ids: impl IntoIterator<Item = String>) -> bool {
        let active_task_id = self.active_task_id.clone();
        let active_guide = self.guide_target_id.clone();
        let mut advanced = false;
        for target_id in target_ids {
            advanced |= self.covered_target_ids.insert(target_id);
        }
        if advanced {
            self.coverage_epoch = self.coverage_epoch.saturating_add(1);
        }
        self.refresh_states();
        let mut guide_covered = false;
        if let Some(active_task_id) = active_task_id {
            guide_covered = active_guide
                .as_ref()
                .is_some_and(|guide| self.covered_target_ids.contains(guide));
            if guide_covered {
                self.attempt_history.push(AttemptHistory {
                    task_id: active_task_id.clone(),
                    guide_target_id: active_guide,
                    seed_id: None,
                    covered: true,
                    coverage_epoch: self.coverage_epoch,
                });
                if let Some(runtime) = self.task_states.get_mut(&active_task_id) {
                    runtime.attempts = runtime.attempts.saturating_add(1);
                    runtime.failed_attempts = 0;
                }
            }
            self.advance_active_guide(&active_task_id);
        }
        if advanced {
            self.mark_ranking_dirty("semantic_coverage");
        } else if guide_covered {
            self.mark_ranking_dirty("guide_target_covered");
        }
        advanced
    }

    pub fn refresh_states(&mut self) {
        let mut reactivated = false;
        for task in &self.plan.tasks {
            let Some(runtime) = self.task_states.get_mut(&task.task_id) else {
                continue;
            };
            if runtime.status == TaskStatus::Unmapped {
                continue;
            }
            if !task_has_executable_path(task) {
                runtime.status = TaskStatus::Blocked;
                continue;
            }
            if self.covered_target_ids.contains(&task.terminal_target_id) {
                runtime.status = TaskStatus::Covered;
                continue;
            }
            if runtime.status == TaskStatus::Deferred {
                if self.coverage_epoch
                    > runtime
                        .deferred_at_coverage_epoch
                        .unwrap_or(self.coverage_epoch)
                {
                    runtime.status = TaskStatus::Ready;
                    runtime.deferred_at_coverage_epoch = None;
                    reactivated = true;
                }
                continue;
            }
            if runtime.status != TaskStatus::Active {
                runtime.status = TaskStatus::Ready;
            }
        }
        if reactivated {
            self.mark_ranking_dirty("task_reactivated");
        }
    }

    #[cfg(test)]
    pub fn select(&mut self, seeds: &[SeedProfile]) -> Option<ActiveSelection> {
        self.seed_profiles.clear();
        self.top_seeds_by_task.clear();
        self.active_selection = None;
        for seed in seeds {
            self.cache_seed_profile(seed.clone());
        }
        self.mark_ranking_dirty("explicit_select");
        self.select_cached()
    }

    #[cfg(test)]
    pub fn select_cached(&mut self) -> Option<ActiveSelection> {
        if !self.ranking_dirty {
            if let Some(selection) = self.active_selection.clone() {
                self.ranking_statistics.cache_hit_count =
                    self.ranking_statistics.cache_hit_count.saturating_add(1);
                return Some(selection);
            }
        }
        self.rebuild_selection()
    }

    fn rebuild_selection(&mut self) -> Option<ActiveSelection> {
        self.ranking_statistics.ranking_count =
            self.ranking_statistics.ranking_count.saturating_add(1);
        self.ranking_statistics.full_rebuild_count =
            self.ranking_statistics.full_rebuild_count.saturating_add(1);
        self.refresh_states();
        let mut ranked = Vec::new();
        for task in &self.plan.tasks {
            let (status, failed_attempts) = self
                .task_states
                .get(&task.task_id)
                .map(|runtime| (runtime.status.clone(), runtime.failed_attempts))?;
            if status != TaskStatus::Ready && status != TaskStatus::Active {
                continue;
            }
            let normalization = self
                .plan
                .normalization_by_pou
                .get(&task.pou)
                .cloned()
                .unwrap_or_default();
            let seed_scores = self
                .top_seeds_by_task
                .get(&task.task_id)
                .into_iter()
                .flatten()
                .filter_map(|ranked| {
                    self.seed_profiles
                        .get(&ranked.content_hash)
                        .map(|seed| (ranked.effort, seed))
                })
                .collect::<Vec<_>>();
            let (minimum_effort, seed) = seed_scores
                .first()
                .map(|(score, seed)| (*score, Some(*seed)))
                .unwrap_or((default_effort(task, &normalization), None));
            let gain = task_gain(task, &self.covered_target_ids);
            let base_priority = gain / (1.0 + minimum_effort);
            let priority = base_priority * (-FAILURE_DECAY_RHO * failed_attempts as f64).exp();
            let covered = seed
                .map(|seed| seed.covered_target_ids.iter().cloned().collect())
                .unwrap_or_default();
            let active_path = choose_active_path(task, &covered);
            let Some(guide_target_id) = first_uncovered(&active_path, &covered) else {
                continue;
            };
            if let Some(runtime) = self.task_states.get_mut(&task.task_id) {
                runtime.priority = priority;
                runtime.base_priority = base_priority;
                runtime.gain = gain;
                runtime.minimum_effort = minimum_effort;
                runtime.best_seed_id = seed.map(|seed| seed.content_hash.clone());
                runtime.active_path = active_path.clone();
                runtime.guide_target_id = Some(guide_target_id.clone());
                runtime.remaining_obligations = remaining_path(&active_path, &covered);
                runtime.prerequisites = task.hazard_prerequisites.clone();
                runtime.unlocked_targets = task.unlocked_targets.clone();
            }
            ranked.push((
                priority,
                task.task_id.clone(),
                seed.map(|seed| seed.content_hash.clone()),
                active_path,
                guide_target_id,
            ));
        }
        ranked.sort_by(|left, right| {
            right
                .0
                .total_cmp(&left.0)
                .then_with(|| left.1.cmp(&right.1))
        });
        self.ranking = ranked
            .iter()
            .map(|(_, task_id, _, _, _)| task_id.clone())
            .collect();
        let ranked_ids = self.ranking.iter().cloned().collect::<HashSet<_>>();
        let mut inactive = self
            .plan
            .tasks
            .iter()
            .filter(|task| !ranked_ids.contains(&task.task_id))
            .map(|task| task.task_id.clone())
            .collect::<Vec<_>>();
        inactive.sort_by(|left, right| {
            task_status_rank(
                &self
                    .task_states
                    .get(left)
                    .map(|state| &state.status)
                    .unwrap_or(&TaskStatus::Unmapped),
            )
            .cmp(&task_status_rank(
                &self
                    .task_states
                    .get(right)
                    .map(|state| &state.status)
                    .unwrap_or(&TaskStatus::Unmapped),
            ))
            .then_with(|| left.cmp(right))
        });
        self.ranking.extend(inactive);
        let (_, task_id, _, _, _) = ranked.into_iter().next()?;
        self.ranking_dirty = false;
        self.activate_task(&task_id, 0)
    }

    pub fn selectable_task_ids(&mut self) -> Vec<String> {
        if self.ranking_dirty {
            let _ = self.rebuild_selection();
        }
        self.ranking
            .iter()
            .filter(|task_id| {
                self.task_states.get(*task_id).is_some_and(|runtime| {
                    matches!(runtime.status, TaskStatus::Ready | TaskStatus::Active)
                })
            })
            .cloned()
            .collect()
    }

    pub fn ranked_seed_count(&self, task_id: &str) -> usize {
        self.top_seeds_by_task
            .get(task_id)
            .map(Vec::len)
            .unwrap_or(0)
    }

    pub fn seed_candidate_count(&self, task_id: &str, broaden: bool) -> usize {
        if broaden && self.plan.tasks.iter().any(|task| task.task_id == task_id) {
            self.seed_profiles.len().max(self.ranked_seed_count(task_id))
        } else {
            self.ranked_seed_count(task_id)
        }
    }

    pub fn activate_task(&mut self, task_id: &str, seed_rank: usize) -> Option<ActiveSelection> {
        self.activate_task_with_seed_mode(task_id, seed_rank, false)
    }

    pub fn activate_task_with_seed_mode(
        &mut self,
        task_id: &str,
        seed_rank: usize,
        broaden_seed: bool,
    ) -> Option<ActiveSelection> {
        let runtime = self.task_states.get(task_id)?;
        if !matches!(runtime.status, TaskStatus::Ready | TaskStatus::Active) {
            return None;
        }
        let priority = runtime.priority;
        let active_path = runtime.active_path.clone();
        let guide_target_id = runtime.guide_target_id.clone()?;
        let task = self
            .plan
            .tasks
            .iter()
            .find(|task| task.task_id == task_id)?
            .clone();
        let best_seed_id = self
            .select_seed_for_task(&task, seed_rank, broaden_seed)
            .or_else(|| runtime.best_seed_id.clone());
        if let Some(previous) = self.active_task_id.take() {
            if previous != task_id {
                if let Some(runtime) = self.task_states.get_mut(&previous) {
                    if runtime.status == TaskStatus::Active {
                        runtime.status = TaskStatus::Ready;
                    }
                }
            }
        }
        if let Some(runtime) = self.task_states.get_mut(task_id) {
            runtime.status = TaskStatus::Active;
            runtime.last_guide_target_id = Some(guide_target_id.clone());
            runtime.best_seed_id = best_seed_id.clone();
        }
        self.active_task_id = Some(task_id.to_string());
        self.guide_target_id = Some(guide_target_id.clone());
        self.active_path = active_path.clone();
        self.best_seed_id = best_seed_id.clone();
        let remaining_obligations = remaining_path(&active_path, &self.active_covered_targets());
        let control_obligation = task
            .control_obligations
            .iter()
            .find(|obligation| obligation.target_id == guide_target_id)
            .cloned();
        let goal_expression_id = control_obligation
            .as_ref()
            .and_then(|obligation| obligation.goal_expression_id.clone())
            .or_else(|| task.terminal_goal_expression_id.clone());
        let goal_expression = control_obligation
            .as_ref()
            .and_then(|obligation| obligation.goal_expression.clone())
            .or_else(|| task.terminal_goal_expression.clone());
        let target_kind = control_obligation
            .as_ref()
            .map(|obligation| obligation.kind.clone())
            .or_else(|| {
                task.hazard_prerequisites
                    .contains(&guide_target_id)
                    .then(|| "hazard_reach".to_string())
            })
            .or_else(|| {
                (guide_target_id == task.terminal_target_id)
                    .then(|| task.terminal_kind.clone())
                    .flatten()
            });
        let selection = ActiveSelection {
            task_id: task_id.to_string(),
            terminal_target_id: task.terminal_target_id,
            guide_target_id,
            active_path,
            remaining_obligations,
            best_seed_id,
            priority,
            control_obligation,
            controllable_seed_fields: task.controllable_seed_fields,
            backward_dependencies: task
                .data_dependencies
                .iter()
                .map(|dependency| dependency.symbol.clone())
                .collect(),
            hazard_prerequisites: task.hazard_prerequisites,
            modeling_status: task.modeling_status,
            task_kind: task.task_kind,
            target_kind,
            hazard_kind: task.terminal_hazard,
            goal_expression_id,
            goal_expression,
            cycle_hint: task
                .temporal_requirements
                .iter()
                .map(|requirement| requirement.minimum_cycles as u64)
                .max(),
            state_transfer_chains: task.state_transfer_chains,
            temporal_requirements: task.temporal_requirements,
        };
        self.active_selection = Some(selection.clone());
        Some(selection)
    }

    fn select_seed_for_task(
        &self,
        task: &SemanticTask,
        seed_rank: usize,
        broaden_seed: bool,
    ) -> Option<String> {
        if broaden_seed {
            let normalization = self
                .plan
                .normalization_by_pou
                .get(&task.pou)
                .cloned()
                .unwrap_or_default();
            let mut candidates = self
                .seed_profiles
                .values()
                .map(|seed| RankedSeed {
                    content_hash: seed.content_hash.clone(),
                    effort: effort(seed, task, &normalization),
                })
                .collect::<Vec<_>>();
            candidates.sort_by(|left, right| {
                left.effort
                    .total_cmp(&right.effort)
                    .then_with(|| left.content_hash.cmp(&right.content_hash))
            });
            if let Some(seed) = candidates.get(seed_rank.min(candidates.len().saturating_sub(1))) {
                return Some(seed.content_hash.clone());
            }
        }
        self.top_seeds_by_task
            .get(&task.task_id)
            .and_then(|seeds| seeds.get(seed_rank.min(seeds.len().saturating_sub(1))))
            .map(|seed| seed.content_hash.clone())
    }

    pub fn record_attempt(&mut self, covered: bool, seed_id: Option<String>) {
        let Some(task_id) = self.active_task_id.clone() else {
            return;
        };
        let guide_target_id = self.guide_target_id.clone();
        self.attempt_history.push(AttemptHistory {
            task_id: task_id.clone(),
            guide_target_id,
            seed_id,
            covered,
            coverage_epoch: self.coverage_epoch,
        });
        let Some(runtime) = self.task_states.get_mut(&task_id) else {
            return;
        };
        let mut deferred = false;
        runtime.attempts = runtime.attempts.saturating_add(1);
        if covered {
            runtime.failed_attempts = 0;
            self.advance_active_guide(&task_id);
        } else {
            runtime.failed_attempts = runtime.failed_attempts.saturating_add(1);
            if runtime.failed_attempts >= DEFER_AFTER_FAILURES {
                runtime.status = TaskStatus::Deferred;
                runtime.deferred_at_coverage_epoch = Some(self.coverage_epoch);
                self.active_task_id = None;
                self.guide_target_id = None;
                self.active_path.clear();
                self.active_selection = None;
                deferred = true;
            }
            self.mark_ranking_dirty("budget_failed");
            if deferred {
                self.mark_ranking_dirty("task_deferred");
            }
        }
    }

    fn advance_active_guide(&mut self, task_id: &str) {
        let Some(task) = self.plan.tasks.iter().find(|task| task.task_id == task_id) else {
            return;
        };
        if self.covered_target_ids.contains(&task.terminal_target_id) {
            if let Some(runtime) = self.task_states.get_mut(task_id) {
                runtime.status = TaskStatus::Covered;
            }
            self.active_task_id = None;
            self.guide_target_id = None;
            self.active_path.clear();
            self.active_selection = None;
            return;
        }
        let path = if self.active_path.is_empty() {
            choose_active_path(task, &self.active_covered_targets())
        } else {
            self.active_path.clone()
        };
        let covered = self.active_covered_targets();
        self.guide_target_id = first_uncovered(&path, &covered);
        if let Some(runtime) = self.task_states.get_mut(task_id) {
            runtime.last_guide_target_id = self.guide_target_id.clone();
        }
    }

    fn active_covered_targets(&self) -> BTreeSet<String> {
        let mut covered = self.covered_target_ids.clone();
        if let Some(seed) = self
            .best_seed_id
            .as_ref()
            .and_then(|seed_id| self.seed_profiles.get(seed_id))
        {
            covered.extend(seed.covered_target_ids.iter().cloned());
        }
        covered
    }
}

pub fn load_plan(path: &std::path::Path) -> Result<SemanticTaskPlan, String> {
    let contents = std::fs::read_to_string(path)
        .map_err(|error| format!("unable to read {}: {error}", path.display()))?;
    let plan: SemanticTaskPlan = serde_json::from_str(&contents)
        .map_err(|error| format!("unable to parse {}: {error}", path.display()))?;
    if plan.schema_version != PLAN_SCHEMA {
        return Err(format!(
            "unsupported semantic task plan schema {}",
            plan.schema_version
        ));
    }
    if plan.target_identity != "stg_stable_target_id" {
        return Err("semantic task plan does not use STG Stable Target ID".to_string());
    }
    Ok(plan)
}

fn first_uncovered(path: &[String], covered: &BTreeSet<String>) -> Option<String> {
    path.iter()
        .find(|target_id| !covered.contains(*target_id))
        .cloned()
}

fn task_has_executable_path(task: &SemanticTask) -> bool {
    !task.ordered_waypoints.is_empty()
        || task
            .candidate_paths
            .iter()
            .any(|path| !path.target_ids.is_empty())
}

fn task_status_rank(status: &TaskStatus) -> u8 {
    match status {
        TaskStatus::Ready | TaskStatus::Active => 0,
        TaskStatus::Deferred => 1,
        TaskStatus::Blocked => 2,
        TaskStatus::Unmapped => 3,
        TaskStatus::Covered => 4,
    }
}

fn remaining_path(path: &[String], covered: &BTreeSet<String>) -> Vec<String> {
    let Some(index) = path
        .iter()
        .position(|target_id| !covered.contains(target_id))
    else {
        return Vec::new();
    };
    path[index..].to_vec()
}

fn choose_active_path(task: &SemanticTask, covered: &BTreeSet<String>) -> Vec<String> {
    task.candidate_paths
        .iter()
        .map(|path| path.target_ids.clone())
        .filter(|path| !path.is_empty())
        .min_by(|left, right| {
            remaining_path(left, covered)
                .len()
                .cmp(&remaining_path(right, covered).len())
                .then_with(|| left.len().cmp(&right.len()))
                .then_with(|| left.cmp(right))
        })
        .unwrap_or_else(|| task.ordered_waypoints.clone())
}

fn effort(seed: &SeedProfile, task: &SemanticTask, norm: &EffortNormalization) -> f64 {
    let covered = seed
        .covered_target_ids
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>();
    let active_path = choose_active_path(task, &covered);
    let remaining = remaining_path(&active_path, &covered)
        .into_iter()
        .collect::<HashSet<_>>();
    let ec = task
        .control_obligations
        .iter()
        .filter(|obligation| remaining.contains(&obligation.target_id))
        .count() as f64
        / norm.ec.max(1.0);
    let affinity = seed
        .target_affinity
        .get(&task.task_id)
        .copied()
        .unwrap_or(0.0)
        .clamp(0.0, 1.0);
    let ed = task.data_dependencies.len() as f64 * (1.0 - affinity) / norm.ed.max(1.0);
    let state_progress = if seed.state_signatures.is_empty() {
        0.0
    } else {
        affinity
    };
    let es = task.state_transfer_chains.len() as f64 * (1.0 - state_progress) / norm.es.max(1.0);
    let completed_temporal = seed.cycle_ids.len().saturating_sub(1);
    let required_temporal = task
        .temporal_requirements
        .iter()
        .map(|requirement| requirement.minimum_cycles.saturating_sub(1))
        .sum::<usize>();
    let et = required_temporal.saturating_sub(completed_temporal) as f64 / norm.et.max(1.0);
    let eh = (task
        .hazard_prerequisites
        .iter()
        .filter(|target| !covered.contains(*target))
        .count()
        + usize::from(!covered.contains(&task.terminal_target_id))) as f64
        / norm.eh.max(1.0);
    ec + ed + es + et + eh
}

fn default_effort(task: &SemanticTask, norm: &EffortNormalization) -> f64 {
    let empty = SeedProfile::default();
    effort(&empty, task, norm)
}

fn task_gain(task: &SemanticTask, covered: &BTreeSet<String>) -> f64 {
    let exact_violation = usize::from(
        task.task_kind == "violation"
            && task.modeling_status == "exact"
            && !covered.contains(&task.terminal_target_id),
    );
    let unlocked_hazards = task
        .unlocked_targets
        .iter()
        .filter(|target| !covered.contains(*target))
        .count();
    let semantic_kinds = task
        .control_obligations
        .iter()
        .map(|obligation| obligation.kind.as_str())
        .collect::<HashSet<_>>();
    let novelty = (semantic_kinds.len() as f64 / 7.0).min(1.0);
    (exact_violation + unlocked_hazards) as f64 + novelty
}

fn seed_has_semantic_information(seed: &SeedProfile) -> bool {
    !seed.covered_target_ids.is_empty()
        || !seed.covered_targets_by_cycle.is_empty()
        || !seed.state_signatures.is_empty()
        || !seed.target_affinity.is_empty()
        || seed.cycle_ids.len() > 1
}

fn task_affected_by_seed(task: &SemanticTask, seed: &SeedProfile) -> bool {
    let covered = seed
        .covered_target_ids
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    task.ordered_waypoints
        .iter()
        .chain(
            task.candidate_paths
                .iter()
                .flat_map(|path| path.target_ids.iter()),
        )
        .chain(task.hazard_prerequisites.iter())
        .chain(task.unlocked_targets.iter())
        .any(|target| covered.contains(target.as_str()))
        || seed
            .target_affinity
            .get(&task.task_id)
            .is_some_and(|affinity| *affinity > 0.0)
        || (!seed.state_signatures.is_empty() && !task.state_transfer_chains.is_empty())
        || (seed.cycle_ids.len() > 1 && !task.temporal_requirements.is_empty())
}
