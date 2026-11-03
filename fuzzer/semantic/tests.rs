#[cfg(test)]
mod tests {
    use super::*;

    // An order-preserving CorpusId for a short ASCII seed name (seven bits per
    // byte, nine bytes), so equal-effort ties still break by name.
    fn seed_id(name: &str) -> libafl::corpus::CorpusId {
        assert!(name.len() <= 9 && name.is_ascii(), "{name}");
        libafl::corpus::CorpusId(name.bytes().chain(std::iter::repeat(0)).take(9)
            .fold(0usize, |id, byte| id << 7 | usize::from(byte)))
    }

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
            seed_id: seed_id("seed"),
            covered_target_ids: vec!["c".to_string()],
            ordered_target_ids: vec!["c".to_string()],
            ordered_trace_complete: true,
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
            seed_id: seed_id("seed"),
            covered_target_ids: vec!["c".to_string()],
            ordered_target_ids: vec!["c".to_string()],
            ordered_trace_complete: true,
            ..Default::default()
        });
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "reach");
        runtime.record_coverage(["different-target".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("reach"));
        runtime.record_coverage(["c".to_string(), "reach".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("reach"));
        runtime.cache_seed_profile(observed_seed("child", &["c", "reach"]));
        runtime.select_cached().unwrap();
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
            seed_id: seed_id("seed-b"),
            covered_target_ids: vec!["c".to_string()],
            ordered_target_ids: vec!["c".to_string()],
            ordered_trace_complete: true,
            ..Default::default()
        });
        let first = runtime.select_cached().unwrap();
        let ranking_count = runtime.ranking_statistics.ranking_count;
        let second = runtime.select_cached().unwrap();
        assert_eq!(first.task_id, second.task_id);
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count);
        assert_eq!(runtime.ranking_statistics.cache_hit_count, 1);

        runtime.cache_seed_profile(SeedProfile {
            seed_id: seed_id("afl-only"),
            ..Default::default()
        });
        assert!(!runtime.ranking_dirty);
        runtime.select_cached().unwrap();
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count);

        runtime.cache_seed_profile(SeedProfile {
            seed_id: seed_id("seed-a"),
            covered_target_ids: vec!["c".to_string(), "reach".to_string()],
            ordered_target_ids: vec!["c".to_string(), "reach".to_string()],
            ordered_trace_complete: true,
            state_signatures: vec!["state:new".to_string()],
            ..Default::default()
        });
        assert!(runtime.ranking_dirty);
        let updated = runtime.select_cached().unwrap();
        assert_eq!(updated.best_seed_id, Some(seed_id("seed-a")));
        assert_eq!(runtime.top_seeds_by_task["task"][0].seed_id, seed_id("seed-a"));
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
                seed_id: seed_id("seed-b"),
                covered_target_ids: vec!["c".to_string()],
                ordered_target_ids: vec!["c".to_string()],
                ordered_trace_complete: true,
                ..Default::default()
            },
            SeedProfile {
                seed_id: seed_id("seed-a"),
                covered_target_ids: vec!["c".to_string(), "reach".to_string()],
                ordered_target_ids: vec!["c".to_string(), "reach".to_string()],
                ordered_trace_complete: true,
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
    fn replacing_seed_evidence_updates_both_old_and_new_task_affinities() {
        let tasks = [("task-a", "a", "terminal-a"), ("task-b", "b", "terminal-b")]
            .into_iter()
            .map(|(task_id, guide, terminal)| SemanticTask {
                task_id: task_id.to_string(),
                terminal_target_id: terminal.to_string(),
                ordered_waypoints: vec![guide.to_string(), terminal.to_string()],
                control_obligations: vec![ControlObligation {
                    target_id: guide.to_string(),
                    ..Default::default()
                }],
                ..Default::default()
            })
            .collect::<Vec<_>>();
        let mapped = tasks.iter().map(|task| task.terminal_target_id.clone()).collect();
        let mut runtime = SemanticRuntimeMetadata::new(
            SemanticTaskPlan { tasks, ..Default::default() }, &mapped,
        );
        runtime.cache_seed_profile(observed_seed("bootstrap", &[]));
        runtime.cache_seed_profile(observed_seed("moving", &["a"]));
        assert_eq!(runtime.top_seeds_by_task["task-a"][0].effort, 1.0);
        assert!(!runtime.top_seeds_by_task["task-b"].iter()
            .any(|seed| seed.seed_id == seed_id("moving")));

        let replacement = observed_seed("moving", &["b"]);
        runtime.cache_seed_profile(replacement.clone());
        let old_task_seed = runtime.top_seeds_by_task["task-a"].iter()
            .find(|seed| seed.seed_id == seed_id("moving")).unwrap();
        assert_eq!(old_task_seed.effort, 2.0);
        assert_eq!(runtime.top_seeds_by_task["task-b"][0].seed_id, seed_id("moving"));
        assert_eq!(runtime.top_seeds_by_task["task-b"][0].effort, 1.0);
        assert_eq!(runtime.corpus_epoch, 2);

        let updates = runtime.ranking_statistics.incremental_seed_updates;
        assert!(!runtime.cache_seed_profile(replacement));
        assert_eq!(runtime.ranking_statistics.incremental_seed_updates, updates);
        assert_eq!(runtime.corpus_epoch, 2);
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
                seed_id: seed_id(&format!("seed-{index:04}")),
                ordered_target_ids: if index == 0 { vec!["c".to_string()] } else { Vec::new() },
                ordered_trace_complete: true,
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

        runtime.record_attempt(false, Some(seed_id("seed-0000")));
        assert!(runtime.ranking_dirty);
        runtime.select_cached().unwrap();
        assert_eq!(runtime.ranking_statistics.ranking_count, ranking_count + 1);
        assert!(runtime
            .ranking_statistics
            .rerank_reasons
            .contains_key("budget_failed"));
    }

    fn linear_runtime() -> SemanticRuntimeMetadata {
        let task = SemanticTask {
            task_id: "linear-task".to_string(),
            task_kind: "violation".to_string(),
            pou: "P".to_string(),
            terminal_target_id: "violation".to_string(),
            ordered_waypoints: vec!["a".to_string(), "b".to_string(), "violation".to_string()],
            modeling_status: "exact".to_string(),
            ..Default::default()
        };
        SemanticRuntimeMetadata::new(
            SemanticTaskPlan { tasks: vec![task], ..Default::default() },
            &HashSet::from(["violation".to_string()]),
        )
    }

    fn observed_seed(id: &str, observed: &[&str]) -> SeedProfile {
        let ordered_target_ids = observed.iter().map(|id| (*id).to_string()).collect::<Vec<_>>();
        SeedProfile {
            seed_id: seed_id(id),
            covered_target_ids: ordered_target_ids.clone(),
            ordered_target_ids,
            ordered_trace_complete: true,
            ..Default::default()
        }
    }

    #[test]
    fn exploration_recomputes_guide_from_the_seed_actually_selected() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("best", &["a", "b"]));
        runtime.cache_seed_profile(observed_seed("explore", &[]));
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "violation");
        let selected = runtime.activate_task_with_seed_mode("linear-task", 1, true).unwrap();
        assert_eq!(selected.best_seed_id, Some(seed_id("explore")));
        assert_eq!(selected.guide_target_id, "a");
        assert_eq!(selected.remaining_obligations, vec!["a", "b", "violation"]);
    }

    #[test]
    fn global_coverage_neither_skips_seed_guides_nor_earns_execution_credit() {
        let mut runtime = linear_runtime();
        runtime.covered_target_ids.extend(["a".to_string(), "b".to_string()]);
        runtime.cache_seed_profile(observed_seed("empty", &[]));
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "a");
        runtime.record_coverage(["unrelated".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("a"));
        assert_eq!(runtime.task_states["linear-task"].attempts, 0);
        assert!(runtime.attempt_history.is_empty());
        assert!(runtime.covered_target_ids.contains("unrelated"));
    }

    #[test]
    fn reversed_execution_events_do_not_satisfy_an_ordered_guide_prefix() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("prefix", &["a"]));
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "b");
        runtime.record_execution(
            ["a".to_string(), "b".to_string()],
            vec!["b".to_string(), "a".to_string()],
            true,
        );
        assert_eq!(runtime.guide_target_id.as_deref(), Some("b"));
        assert_eq!(runtime.task_states["linear-task"].attempts, 0);
        assert!(runtime.attempt_history.is_empty());
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        assert_eq!(runtime.task_states["linear-task"].attempts, 1);
        assert!(runtime.attempt_history.last().unwrap().covered);
    }

    #[test]
    fn truncated_tail_preserves_success_proven_by_the_observed_prefix() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("prefix", &["a"]));
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "b");
        runtime.record_execution(
            ["a".to_string(), "b".to_string()],
            vec!["a".to_string(), "b".to_string()],
            false,
        );
        assert_eq!(runtime.guide_target_id.as_deref(), Some("b"));
        assert_eq!(runtime.task_states["linear-task"].attempts, 1);
        assert!(runtime.attempt_history.last().unwrap().covered);
        assert!(runtime.covered_target_ids.contains("b"));
    }

    #[test]
    fn queue_suspension_preserves_global_discoveries_without_task_credit() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("prefix", &["a"]));
        runtime.select_cached().unwrap();
        runtime.suspend_selection();
        assert!(runtime.active_task_id.is_none());
        assert!(runtime.guide_target_id.is_none());
        assert!(runtime.best_seed_id.is_none());
        assert!(runtime.active_path.is_empty());
        assert!(runtime.active_selection.is_none());
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        runtime.record_attempt(false, None);
        assert_eq!(runtime.task_states["linear-task"].attempts, 0);
        assert_eq!(runtime.task_states["linear-task"].failed_attempts, 0);
        assert_eq!(runtime.task_states["linear-task"].status, TaskStatus::Ready);
        assert!(runtime.attempt_history.is_empty());
        assert!(runtime.covered_target_ids.contains("b"));
    }

    #[test]
    fn deferred_task_retries_after_cooldown_without_new_edges_or_seeds() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("empty", &[]));
        runtime.select_cached().unwrap();
        for _ in 0..3 { runtime.record_attempt(false, None); }
        assert_eq!(runtime.task_states["linear-task"].status, TaskStatus::Deferred);
        let epochs = (runtime.coverage_epoch, runtime.corpus_epoch);
        for _ in 0..127 { runtime.tick_selection(); }
        assert_eq!(runtime.task_states["linear-task"].status, TaskStatus::Deferred);
        runtime.tick_selection();
        assert_eq!(runtime.task_states["linear-task"].status, TaskStatus::Ready);
        assert_eq!((runtime.coverage_epoch, runtime.corpus_epoch), epochs);
        assert!(runtime.task_states["linear-task"].failed_attempts < 3);
        assert!(runtime.activate_task("linear-task", 0).is_some());
        runtime.record_attempt(false, None);
        assert_eq!(runtime.task_states["linear-task"].status, TaskStatus::Deferred);
        assert_eq!(runtime.task_states["linear-task"].retry_after_selection, 256);
    }

    #[test]
    fn assigned_guide_stays_fixed_and_success_is_once_per_mutation_stage() {
        let mut runtime = linear_runtime();
        runtime.cache_seed_profile(observed_seed("prefix", &["a"]));
        runtime.select_cached().unwrap();
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        assert_eq!(runtime.guide_target_id.as_deref(), Some("b"));
        assert_eq!(runtime.active_selection.as_ref().unwrap().guide_target_id, "b");
        assert_eq!(runtime.guide_success_count("linear-task", &guide_identity("ordered_waypoints", 1, "b")), 1);
        assert_eq!(runtime.guide_success_count("linear-task", &guide_identity("ordered_waypoints", 2, "violation")), 0);
        assert_eq!(runtime.task_states["linear-task"].attempts, 1);
        runtime.cache_seed_profile(observed_seed("child", &["a", "b"]));
        runtime.select_cached().unwrap();
        assert_eq!(runtime.guide_target_id.as_deref(), Some("violation"));
        assert!(!runtime.active_selection_succeeded);
    }

    #[test]
    fn prefix_novelty_is_single_execution_monotone_and_bounded() {
        let mut runtime = linear_runtime();
        runtime.covered_target_ids.extend(["a".to_string(), "b".to_string()]);
        runtime.cache_seed_profile(observed_seed("empty", &[]));
        runtime.select_cached().unwrap();
        let targets = vec!["a".to_string(), "b".to_string()];
        // Incomplete traces prove only their observed positive prefix; separate
        // executions cannot be concatenated to extend it.
        assert!(runtime.record_execution(targets.clone(), vec!["a".to_string()], false));
        assert!(!runtime.record_execution(targets.clone(), vec!["b".to_string()], false));
        assert!(!runtime.record_execution(targets.clone(), vec!["b".to_string(), "a".to_string()], false));
        assert!(runtime.record_execution(targets.clone(), targets.clone(), false));
        for _ in 0..100 {
            assert!(!runtime.record_execution(targets.clone(), targets.clone(), false));
        }
        let progress = &runtime.task_states["linear-task"].best_prefix_by_path;
        assert_eq!(progress.len(), 1);
        assert_eq!(progress["ordered_waypoints"], 2);
        assert_eq!(runtime.attempt_history.len(), 1);
        assert!(runtime.attempt_history[0].covered);
    }

    #[test]
    fn repeated_waypoint_occurrences_require_repeated_ordered_evidence() {
        let mut runtime = linear_runtime();
        runtime.plan.tasks[0].ordered_waypoints = ["a", "b", "a", "violation"]
            .into_iter().map(str::to_string).collect();
        runtime.cache_seed_profile(observed_seed("prefix", &["a", "b"]));
        let selected = runtime.select_cached().unwrap();
        assert_eq!(selected.guide_target_id, "a");
        assert_eq!(selected.guide_index, 2);
        assert_eq!(selected.remaining_obligations, ["a", "violation"]);
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        assert!(runtime.attempt_history.is_empty());
        // Seeing the second a in this same execution satisfies occurrence 2.
        runtime.record_execution(vec!["a".to_string(), "b".to_string()],
            vec!["a".to_string(), "b".to_string(), "a".to_string()], false);
        assert_eq!(runtime.attempt_history.len(), 1);
        assert!(runtime.attempt_history[0].covered);
        assert_eq!(runtime.active_guide_index, Some(2));
        runtime.cache_seed_profile(observed_seed("child", &["a", "b", "a"]));
        let child = runtime.activate_task_with_seed_mode("linear-task", 0, true).unwrap();
        assert_eq!(child.best_seed_id, Some(seed_id("child")));
        assert_eq!(child.guide_index, 3);
        assert_eq!(child.guide_target_id, "violation");
    }

    #[test]
    fn legacy_flat_sets_cannot_skip_guides_and_partial_order_only_proves_its_prefix() {
        let mut runtime = linear_runtime();
        let mut seed = SeedProfile {
            seed_id: seed_id("legacy"),
            covered_target_ids: vec!["a".to_string(), "b".to_string()],
            ..Default::default()
        };
        runtime.cache_seed_profile(seed.clone());
        assert_eq!(runtime.select_cached().unwrap().guide_target_id, "a");
        seed.ordered_target_ids = vec!["b".to_string(), "a".to_string()];
        runtime.cache_seed_profile(seed);
        assert_eq!(runtime.activate_task("linear-task", 0).unwrap().guide_target_id, "b");
    }

    #[test]
    fn expired_budget_charges_its_original_task_after_scheduler_switches() {
        let mut runtime = linear_runtime();
        let mut second = runtime.plan.tasks[0].clone();
        second.task_id = "second-task".to_string();
        second.terminal_target_id = "second-terminal".to_string();
        runtime.plan.tasks.push(second);
        runtime.task_states.insert("second-task".to_string(), TaskRuntimeState::default());
        runtime.cache_seed_profile(observed_seed("empty", &[]));
        runtime.selectable_task_ids();
        runtime.activate_task("second-task", 0).unwrap();
        runtime.record_budget_failure("linear-task", &guide_identity("ordered_waypoints", 0, "a"));
        assert_eq!(runtime.active_task_id.as_deref(), Some("second-task"));
        assert_eq!(runtime.task_states["linear-task"].failed_attempts, 1);
        assert_eq!(runtime.task_states["second-task"].failed_attempts, 0);
        let attempt = runtime.attempt_history.last().unwrap();
        assert_eq!(attempt.task_id, "linear-task");
        assert_eq!(attempt.guide_target_id.as_deref(), Some("a"));
    }

    #[test]
    fn repeated_target_occurrences_have_distinct_success_and_budget_identities() {
        let mut runtime = linear_runtime();
        runtime.plan.tasks[0].ordered_waypoints = ["a", "b", "a", "violation"]
            .into_iter().map(str::to_string).collect();
        runtime.cache_seed_profile(observed_seed("empty", &[]));
        runtime.cache_seed_profile(observed_seed("middle", &["a", "b"]));
        runtime.selectable_task_ids();
        runtime.activate_task_with_seed_mode("linear-task", 1, true).unwrap();
        let second_key = runtime.active_guide_budget_key().unwrap();
        assert_eq!(runtime.active_guide_index, Some(2));
        runtime.record_coverage(["a".to_string(), "b".to_string()]);
        assert!(!runtime.active_selection_succeeded);
        runtime.activate_task_with_seed_mode("linear-task", 0, true).unwrap();
        let first_key = runtime.active_guide_budget_key().unwrap();
        assert_ne!(first_key, second_key);
        assert_eq!(runtime.active_guide_index, Some(0));
        runtime.record_coverage(["a".to_string()]);
        let (_, first_identity) = first_key.split_once('\0').unwrap();
        let (_, second_identity) = second_key.split_once('\0').unwrap();
        assert_eq!(runtime.guide_success_count("linear-task", first_identity), 1);
        assert_eq!(runtime.guide_success_count("linear-task", second_identity), 0);
        runtime.record_budget_failure("linear-task", second_identity);
        let attempt = runtime.attempt_history.last().unwrap();
        assert!(!attempt.covered);
        assert_eq!(attempt.path_key.as_deref(), Some("ordered_waypoints"));
        assert_eq!(attempt.guide_index, Some(2));
    }
}
