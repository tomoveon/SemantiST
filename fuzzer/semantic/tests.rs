#[cfg(test)]
mod tests {
    use super::*;

    fn task() -> SemanticTask {
        SemanticTask {
            task_id: "task".to_string(),
            task_kind: "violation".to_string(),
            pou: "P".to_string(),
            terminal_target_id: "violation".to_string(),
            candidate_paths: vec![
                CandidatePath {
                    path_id: "long".to_string(),
                    target_ids: vec![
                        "a".to_string(),
                        "b".to_string(),
                        "reach".to_string(),
                        "violation".to_string(),
                    ],
                    ..Default::default()
                },
                CandidatePath {
                    path_id: "short".to_string(),
                    target_ids: vec![
                        "c".to_string(),
                        "reach".to_string(),
                        "violation".to_string(),
                    ],
                    ..Default::default()
                },
            ],
            hazard_prerequisites: vec!["reach".to_string()],
            modeling_status: "exact".to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn guide_advances_and_prefers_less_remaining_work() {
        let task = task();
        let covered = BTreeSet::from(["c".to_string()]);
        let path = choose_active_path(&task, &covered);
        assert_eq!(path[0], "c");
        assert_eq!(first_uncovered(&path, &covered).as_deref(), Some("reach"));
    }

    #[test]
    fn hazard_reach_is_a_ready_internal_waypoint() {
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task()],
            ..Default::default()
        };
        let mut runtime =
            SemanticRuntimeMetadata::new(plan, &HashSet::from(["violation".to_string()]));
        let selection = runtime.select(&[SeedProfile {
            content_hash: "seed".to_string(),
            covered_target_ids: vec!["c".to_string()],
            ..Default::default()
        }]);
        let selection = selection.expect("violation task should guide through HazardReach");
        assert_eq!(selection.guide_target_id, "reach");
        assert_eq!(selection.target_kind.as_deref(), Some("hazard_reach"));
        assert_eq!(runtime.task_states["task"].status, TaskStatus::Active);
    }

    #[test]
    fn deferred_task_reactivates_after_new_coverage() {
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task()],
            ..Default::default()
        };
        let mapped = HashSet::from(["violation".to_string()]);
        let mut runtime = SemanticRuntimeMetadata::new(plan, &mapped);
        runtime.covered_target_ids.insert("reach".to_string());
        runtime.refresh_states();
        runtime.active_task_id = Some("task".to_string());
        runtime.guide_target_id = Some("violation".to_string());
        runtime.record_attempt(false, None);
        runtime.record_attempt(false, None);
        runtime.record_attempt(false, None);
        assert_eq!(runtime.task_states["task"].status, TaskStatus::Deferred);
        assert!(runtime
            .ranking_statistics
            .rerank_reasons
            .contains_key("task_deferred"));
        runtime.record_coverage(["other".to_string()]);
        assert_eq!(runtime.task_states["task"].status, TaskStatus::Ready);
        assert!(runtime
            .ranking_statistics
            .rerank_reasons
            .contains_key("task_reactivated"));
    }

    #[test]
    fn failure_decay_reorders_equal_tasks() {
        let mut second = task();
        second.task_id = "task2".to_string();
        second.terminal_target_id = "violation2".to_string();
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task(), second],
            ..Default::default()
        };
        let mapped = HashSet::from(["violation".to_string(), "violation2".to_string()]);
        let mut runtime = SemanticRuntimeMetadata::new(plan, &mapped);
        runtime.covered_target_ids.insert("reach".to_string());
        runtime.task_states.get_mut("task").unwrap().failed_attempts = 4;
        let selected = runtime.select(&[SeedProfile::default()]).unwrap();
        assert_eq!(selected.task_id, "task2");
    }

    #[test]
    fn runtime_only_advances_coverage_after_observed_targets() {
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task()],
            ..Default::default()
        };
        let mut runtime =
            SemanticRuntimeMetadata::new(plan, &HashSet::from(["violation".to_string()]));
        runtime.cache_seed_profile(SeedProfile {
            content_hash: "seed".to_string(),
            covered_target_ids: vec!["c".to_string()],
            ..Default::default()
        });
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "reach");
        runtime.record_coverage(["different-target".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("reach"));
        runtime.record_coverage(["reach".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("violation"));
    }

    #[test]
    fn cached_scheduler_reuses_selection_and_updates_top_k_incrementally() {
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task()],
            ..Default::default()
        };
        let mut runtime =
            SemanticRuntimeMetadata::new(plan, &HashSet::from(["violation".to_string()]));
        runtime.cache_seed_profile(SeedProfile {
            content_hash: "seed-b".to_string(),
            covered_target_ids: vec!["c".to_string()],
            ..Default::default()
        });
        let first = runtime.select_cached().unwrap();
        let ranking_count = runtime.ranking_statistics.ranking_count;
        let second = runtime.select_cached().unwrap();
        assert_eq!(first.task_id, second.task_id);
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count);
        assert_eq!(runtime.ranking_statistics.cache_hit_count, 1);

        runtime.cache_seed_profile(SeedProfile {
            content_hash: "afl-only".to_string(),
            ..Default::default()
        });
        assert!(!runtime.ranking_dirty);
        runtime.select_cached().unwrap();
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count);

        runtime.cache_seed_profile(SeedProfile {
            content_hash: "seed-a".to_string(),
            covered_target_ids: vec!["c".to_string(), "reach".to_string()],
            state_signatures: vec!["state:new".to_string()],
            ..Default::default()
        });
        assert!(runtime.ranking_dirty);
        let updated = runtime.select_cached().unwrap();
        assert_eq!(updated.best_seed_id.as_deref(), Some("seed-a"));
        assert_eq!(runtime.top_seeds_by_task["task"][0].content_hash, "seed-a");
    }

    #[test]
    fn cached_and_explicit_ranking_produce_the_same_semantic_selection() {
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks: vec![task()],
            ..Default::default()
        };
        let mapped = HashSet::from(["violation".to_string()]);
        let seeds = vec![
            SeedProfile {
                content_hash: "seed-b".to_string(),
                covered_target_ids: vec!["c".to_string()],
                ..Default::default()
            },
            SeedProfile {
                content_hash: "seed-a".to_string(),
                covered_target_ids: vec!["c".to_string(), "reach".to_string()],
                state_signatures: vec!["state:a".to_string()],
                ..Default::default()
            },
        ];
        let mut explicit = SemanticRuntimeMetadata::new(plan.clone(), &mapped);
        let expected = explicit.select(&seeds).unwrap();
        let mut cached = SemanticRuntimeMetadata::new(plan, &mapped);
        for seed in seeds {
            cached.cache_seed_profile(seed);
        }
        let actual = cached.select_cached().unwrap();
        assert_eq!(actual.task_id, expected.task_id);
        assert_eq!(actual.guide_target_id, expected.guide_target_id);
        assert_eq!(actual.best_seed_id, expected.best_seed_id);
        assert_eq!(actual.active_path, expected.active_path);
        assert_eq!(cached.ranking, explicit.ranking);
    }

    #[test]
    fn reranking_is_event_driven_and_not_task_seed_product_per_next() {
        let mut tasks = Vec::new();
        for index in 0..128 {
            let mut candidate = task();
            candidate.task_id = format!("task-{index:03}");
            candidate.terminal_target_id = format!("violation-{index:03}");
            tasks.push(candidate);
        }
        let mapped = tasks
            .iter()
            .map(|task| task.terminal_target_id.clone())
            .collect::<HashSet<_>>();
        let plan = SemanticTaskPlan {
            schema_version: PLAN_SCHEMA.to_string(),
            target_identity: "stg_stable_target_id".to_string(),
            tasks,
            ..Default::default()
        };
        let mut runtime = SemanticRuntimeMetadata::new(plan, &mapped);
        for index in 0..512 {
            runtime.cache_seed_profile(SeedProfile {
                content_hash: format!("seed-{index:04}"),
                covered_target_ids: if index == 0 {
                    vec!["c".to_string()]
                } else {
                    Vec::new()
                },
                ..Default::default()
            });
        }
        runtime.select_cached().unwrap();
        let ranking_count = runtime.ranking_statistics.ranking_count;
        for _ in 0..1_000 {
            runtime.select_cached().unwrap();
        }
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count);
        assert_eq!(runtime.ranking_statistics.cache_hit_count, 1_000);

        runtime.record_attempt(false, Some("seed-0000".to_string()));
        assert!(runtime.ranking_dirty);
        runtime.select_cached().unwrap();
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count + 1);
        assert!(runtime
            .ranking_statistics
            .rerank_reasons
            .contains_key("budget_failed"));
    }
}
