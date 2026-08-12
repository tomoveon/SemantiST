#[derive(Debug)]
struct SemanticTaskBudgetStage {
    last_guide_target_id: Option<String>,
    last_accounted_executions: u64,
    last_state_signature: Option<String>,
    executions_by_guide: HashMap<String, u64>,
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
        "{}:{}:{:?}:{:?}:{}:{}:{}",
        runtime.coverage_epoch,
        runtime.corpus_epoch,
        runtime.active_task_id,
        runtime.guide_target_id,
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
        if let Some(runtime) = state.metadata_map().get::<SemanticRuntimeMetadata>() {
            let signature = semantic_state_signature(runtime);
            if self.last_state_signature.as_ref() != Some(&signature) {
                persist_semantic_task_state(&self.state_file, runtime)?;
                self.last_state_signature = Some(signature);
            }
        }
        let executions = *state.executions();
        let guide = state
            .metadata_map()
            .get::<SemanticRuntimeMetadata>()
            .and_then(|runtime| runtime.guide_target_id.clone());
        let guide_executions = self.account_execution_progress(executions, guide.clone());
        let Some(guide_target_id) = guide else {
            return Ok(());
        };
        if guide_executions < self.budget {
            return Ok(());
        }
        if let Some(runtime) = state
            .metadata_map_mut()
            .get_mut::<SemanticRuntimeMetadata>()
        {
            runtime.record_attempt(false, None);
            persist_semantic_task_state(&self.state_file, runtime)?;
            self.last_state_signature = Some(semantic_state_signature(runtime));
        }
        self.executions_by_guide.insert(guide_target_id, 0);
        self.last_accounted_executions = *state.executions();
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
