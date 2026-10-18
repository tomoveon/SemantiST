#[derive(Debug)]
struct SemanticTaskBudgetStage {
    last_guide_target_id: Option<String>,
    last_accounted_executions: u64,
    last_state_signature: Option<String>,
    executions_by_guide: HashMap<String, u64>,
    successes_by_guide: HashMap<String, u64>,
    budget: u64,
    state_file: PathBuf,
}

impl SemanticTaskBudgetStage {
    fn new(budget: u64, state_file: PathBuf) -> Self {
        Self {
            last_guide_target_id: None,
            last_accounted_executions: 0,
            last_state_signature: None,
            executions_by_guide: HashMap::new(),
            successes_by_guide: HashMap::new(),
            budget: budget.max(1),
            state_file,
        }
    }

    fn account_execution_progress(
        &mut self,
        executions: u64,
        current_guide: Option<String>,
    ) -> u64 {
        let execution_delta = executions.saturating_sub(self.last_accounted_executions);
        if let Some(previous_guide) = self.last_guide_target_id.as_ref() {
            let accumulated = self
                .executions_by_guide
                .entry(previous_guide.clone())
                .or_default();
            *accumulated = accumulated.saturating_add(execution_delta);
        }
        self.last_accounted_executions = executions;
        self.last_guide_target_id = current_guide.clone();
        current_guide
            .and_then(|guide| self.executions_by_guide.get(&guide).copied())
            .unwrap_or(0)
    }

    fn settle_execution_window(
        &mut self,
        executions: u64,
        current_guide: Option<String>,
        previous_successes: u64,
    ) -> Option<String> {
        let previous = self.last_guide_target_id.clone();
        self.account_execution_progress(executions, current_guide);
        let previous = previous?;
        let recorded = self.successes_by_guide.entry(previous.clone()).or_default();
        if previous_successes > *recorded {
            *recorded = previous_successes;
            self.executions_by_guide.insert(previous, 0);
            return None;
        }
        if self.executions_by_guide.get(&previous).copied().unwrap_or(0) >= self.budget {
            self.executions_by_guide.insert(previous.clone(), 0);
            return Some(previous);
        }
        None
    }
}

fn semantic_state_detail_enabled() -> bool {
    matches!(
        env::var("SEMANTIST_SEMANTIC_STATE_DETAIL")
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "full" | "debug"
    )
}

fn semantic_state_signature(runtime: &SemanticRuntimeMetadata) -> String {
    format!(
        "{}:{}:{:?}:{:?}:{:?}:{:?}:{}:{}:{}",
        runtime.coverage_epoch,
        runtime.corpus_epoch,
        runtime.active_task_id,
        runtime.guide_target_id,
        runtime.active_path_key,
        runtime.active_guide_index,
        runtime.covered_target_ids.len(),
        runtime.ranking.len(),
        runtime.attempt_history.len()
    )
}

fn persist_semantic_task_state(
    path: &Path,
    runtime: &SemanticRuntimeMetadata,
) -> Result<(), Error> {
    const COMPACT_RANKING_LIMIT: usize = 32;
    let _timer = PhaseTimer::start(Phase::IoTaskState);
    let detailed = semantic_state_detail_enabled();
    let active_task_state = runtime
        .active_task_id
        .as_ref()
        .and_then(|task_id| runtime.task_states.get(task_id));
    let ranking_limit = if detailed {
        runtime.ranking.len()
    } else {
        COMPACT_RANKING_LIMIT
    };
    let ranking: Vec<&String> = runtime.ranking.iter().take(ranking_limit).collect();
    let mut value = serde_json::json!({
        "schema_version": "semantist.semantic-task-state/1.0.0",
        "coverage_epoch": runtime.coverage_epoch,
        "corpus_epoch": runtime.corpus_epoch,
        "active_task_id": runtime.active_task_id,
        "guide_target_id": runtime.guide_target_id,
        "active_path": runtime.active_path,
        "active_path_key": runtime.active_path_key,
        "active_guide_index": runtime.active_guide_index,
        "selection_epoch": runtime.selection_epoch,
        "best_seed_id": runtime.best_seed_id,
        "ranking": ranking,
        "ranking_total": runtime.ranking.len(),
        "ranking_truncated": !detailed && runtime.ranking.len() > ranking_limit,
        "ranking_dirty": runtime.ranking_dirty,
        "ranking_statistics": runtime.ranking_statistics,
        "last_rerank_reason": runtime.last_rerank_reason,
        "covered_target_count": runtime.covered_target_ids.len(),
        "active_task_state": active_task_state,
        "attempt_history_len": runtime.attempt_history.len(),
    });
    if detailed {
        value["covered_target_ids"] = serde_json::json!(runtime.covered_target_ids);
        value["task_states"] = serde_json::json!(runtime.task_states);
        value["attempt_history"] = serde_json::json!(runtime.attempt_history);
    }
    let serialized = serde_json::to_string(&value).map_err(|error| {
        Error::serialize(format!("unable to serialize semantic task state: {error}"))
    })?;
    let temporary = path.with_extension("json.tmp");
    fs::write(&temporary, format!("{serialized}\n"))?;
    fs::rename(temporary, path)?;
    Ok(())
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for SemanticTaskBudgetStage
where
    S: HasCorpus<BytesInput> + HasExecutions + HasMetadata,
{
    fn perform(
        &mut self,
        _fuzzer: &mut Z,
        _executor: &mut E,
        state: &mut S,
        _manager: &mut EM,
    ) -> Result<(), Error> {
        let executions = *state.executions();
        let guide = state
            .metadata_map()
            .get::<TargetContext>()
            .and_then(|context| state.metadata_map().get::<SemanticRuntimeMetadata>()
                .filter(|runtime| runtime.active_task_id == context.semantic_task_id
                    && runtime.guide_target_id == context.target_id)
                .and_then(SemanticRuntimeMetadata::active_guide_budget_key));
        let previous_successes = self.last_guide_target_id.as_ref()
            .and_then(|key| key.split_once('\0'))
            .and_then(|(task, guide)| state.metadata_map().get::<SemanticRuntimeMetadata>()
                .map(|runtime| runtime.guide_success_count(task, guide))).unwrap_or(0);
        if let Some(expired) = self.settle_execution_window(executions, guide, previous_successes) {
            if let (Some((task, guide)), Some(runtime)) = (expired.split_once('\0'),
                state.metadata_map_mut().get_mut::<SemanticRuntimeMetadata>()) {
                // Charge the selection that actually ran, even if the
                // scheduler has already selected another task for this stage.
                runtime.record_budget_failure(task, guide);
            }
        }
        if state.metadata_map().get::<SemanticRuntimeMetadata>()
            .is_some_and(|runtime| runtime.active_task_id.is_none()) {
            // Deferral can happen after the scheduler selected this seed.
            // Keep the following mutations ordinary queue work in that case.
            state.add_metadata(TargetContext::default());
            self.last_guide_target_id = None;
        }
        if let Some(runtime) = state.metadata_map().get::<SemanticRuntimeMetadata>() {
            let signature = semantic_state_signature(runtime);
            if self.last_state_signature.as_ref() != Some(&signature) {
                persist_semantic_task_state(&self.state_file, runtime)?;
                self.last_state_signature = Some(signature);
            }
        }
        Ok(())
    }
}

impl<S> Restartable<S> for SemanticTaskBudgetStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, Error> {
        Ok(true)
    }

    fn clear_progress(&mut self, _state: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

#[cfg(test)]
mod semantic_budget_window_tests {
    use super::*;

    #[test]
    fn successful_window_resets_budget_before_failure_accounting() {
        let mut stage = SemanticTaskBudgetStage::new(3, PathBuf::from("unused.json"));
        let key = "task\0guide".to_string();
        assert!(stage.settle_execution_window(0, Some(key.clone()), 0).is_none());
        assert!(stage.settle_execution_window(5, Some(key.clone()), 1).is_none());
        assert_eq!(stage.executions_by_guide[&key], 0);
        assert!(stage.settle_execution_window(7, Some(key.clone()), 1).is_none());
        assert_eq!(stage.settle_execution_window(8, None, 1), Some(key));
    }

    #[test]
    fn shared_guide_budgets_belong_to_the_task_that_executed() {
        let mut stage = SemanticTaskBudgetStage::new(3, PathBuf::from("unused.json"));
        let first = "first\0shared".to_string();
        let second = "second\0shared".to_string();
        stage.settle_execution_window(0, Some(first.clone()), 0);
        assert!(stage.settle_execution_window(2, Some(second.clone()), 0).is_none());
        assert!(stage.settle_execution_window(4, Some(first.clone()), 0).is_none());
        assert_eq!(stage.settle_execution_window(5, Some(second.clone()), 0), Some(first));
        assert_eq!(stage.executions_by_guide[&second], 2);
    }

    #[test]
    fn ordinary_queue_executions_do_not_consume_the_previous_guide_budget() {
        let mut stage = SemanticTaskBudgetStage::new(3, PathBuf::from("unused.json"));
        let key = "task\0guide".to_string();
        stage.settle_execution_window(0, Some(key.clone()), 0);
        assert!(stage.settle_execution_window(1, None, 0).is_none());
        assert!(stage.settle_execution_window(101, Some(key.clone()), 0).is_none());
        assert_eq!(stage.executions_by_guide[&key], 1);
    }

    #[test]
    fn first_occurrence_success_does_not_clear_later_occurrence_budget() {
        let mut stage = SemanticTaskBudgetStage::new(3, PathBuf::from("unused.json"));
        let first = format!("task\0ordered_waypoints\0{}\0a", 0);
        let later = format!("task\0ordered_waypoints\0{}\0a", 2);
        stage.settle_execution_window(0, Some(later.clone()), 0);
        assert!(stage.settle_execution_window(2, Some(first.clone()), 0).is_none());
        assert!(stage.settle_execution_window(3, Some(later.clone()), 1).is_none());
        assert_eq!(stage.executions_by_guide[&first], 0);
        assert_eq!(stage.executions_by_guide[&later], 2);
        assert_eq!(stage.settle_execution_window(4, None, 0), Some(later));
    }
}
