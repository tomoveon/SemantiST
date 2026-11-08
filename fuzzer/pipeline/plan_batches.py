"""Canonical pre-search LLM prompt, request, and native-feedback loop."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fuzzer.corpus.frames import canonicalize_raw, default_for_spec, parse_testcase
from compiler.symbolic.driver import symbolic_scope
from fuzzer.llm.client import LlmClient, LlmError
from fuzzer.llm.config import load_config
from fuzzer.llm.plan import PlanRejected, _field_map, expand_plan, validate_plan, validate_plan_batch
from fuzzer.pipeline.probe import classify, is_finding_candidate
from fuzzer.pipeline.probe_audit import audit_probe_completeness


PLANNER_SYSTEM_MESSAGE = """You plan native Structured Text tests before hybrid search.
Return only a PlanBatch JSON object with exactly contract_id and plans. Each plan
must target a supplied task and cite supplied evidence references. Use the ABI,
cycle effects, source excerpt, STG goal, probe status and valid example to choose
set, pulse, hold, wait or repeat actions. Prefer one frame when it can test the
condition; use multiple frames only when the supplied effects or temporal goal
justify them. Never invent fields, probes, prefix IDs, model support or findings.
Use action_forms for exact JSON shapes: pulse is BOOL only, frames and times are
positive integers, and every input value must fit its ABI type.
Produce distinct plans whose native execution could reach the target or improve
coverage, valid state or target distance. Symbolic field suggestions must name
controllable frame ABI fields; they do not establish a solver result. Optional
solver_triggers may name task_distance_improved, native_hazard_reach,
state_frontier or task_stagnation when the supplied task supports them.
Read previous_feedback before revising: correct rejected values and actions,
preserve the original plan's task and evidence, and do not repeat rejected or
non-novel actions. The native executor, KLEE and finding verifier decide outcomes.
If probe_status is incomplete, do not claim that executing the plan verifies the
source risk. Return only JSON, without Markdown or explanations outside the JSON."""


def _example(abi: dict[str, Any], contract_id: str | None,
             task_id: str, evidence_refs: list[str]) -> dict[str, Any]:
    fields = [*abi.get("frame_fields", []),
              *(entry for entry in abi.get("shared_fields", [])
                if entry.get("block_kind") == "VAR_IN_OUT")]
    inputs: dict[str, Any] = {}
    if fields:
        entry = fields[0]
        spec = entry["spec"]
        value: Any = default_for_spec(spec)
        if spec.get("kind") == "scalar":
            value = float(value) if spec.get("name") in {"REAL", "LREAL"} else int(value)
        inputs[entry["name"]] = value
    plan = {"plan_id": "valid-example", "task_id": task_id,
            "contract_id": contract_id, "evidence_refs": evidence_refs[:1],
            "priority": 1,
            "suggested_symbolic_fields": [],
            "actions": [{"op": "set", "inputs": inputs}]}
    return validate_plan(plan, abi=abi, contract_id=contract_id, task_id=task_id,
                         allowed_evidence_refs=set(evidence_refs), max_frames=1)


def _fallback_coverage_seeds(campaign: Any, abi: dict[str, Any], effect: dict[str, Any]) -> int:
    """Seed ordinary coverage search when no defect task is available."""
    base = campaign._default_action_frame(abi)
    variants = [dict(base)]
    for entry in [*abi.get("frame_fields", []),
                  *(item for item in abi.get("shared_fields", [])
                    if item.get("block_kind") == "VAR_IN_OUT")]:
        name = entry.get("name")
        spec = entry.get("spec") or {}
        if not name:
            continue
        candidate = dict(base)
        if spec.get("kind") == "string":
            candidate[name] = "0"
        elif spec.get("name") in {"REAL", "LREAL"}:
            candidate[name] = "1.0"
        elif spec.get("name") == "BOOL":
            candidate[name] = "1"
        else:
            candidate[name] = "1"
        if candidate != base:
            variants.append(candidate)
        if len(variants) >= 4:
            break

    max_frames = 1 if effect.get("mode") == "single_cycle" else 2
    seed_dir = campaign.run_dir / "unified-corpus" / "seeds"
    seed_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, frame in enumerate(variants, start=1):
        frames = [dict(base), dict(frame)] if max_frames > 1 else [dict(frame)]
        try:
            testcase = campaign.model.build(frames)
        except (OSError, TypeError, ValueError):
            continue
        (seed_dir / f"llm-coverage-{index:04}.seed").write_bytes(
            canonicalize_raw(testcase, campaign._abi_like()))
        written += 1
    return written


def run_plan_batches(campaign: Any) -> None:
    campaign.llm_symbolic_suggestions = {}
    campaign.llm_solver_hints = {}
    ablations = campaign.config.get("ablations", {})
    if not (ablations.get("llm") or ablations.get("llm_initial_only")):
        return
    offline = bool(campaign.config.get("llm_offline"))
    max_calls = max(0, int(campaign.config.get("llm_max_calls", 40)))
    rounds = min(5, max(3, int(campaign.config.get("llm_plan_rounds", 3))))
    batch_size = max(1, int(campaign.config.get("llm_plan_batch_size", 3)))
    token_budget = max(0, int(campaign.config.get("llm_plan_token_budget", 20000)))
    cache_dirs = campaign.config.get("llm_cache_dirs")
    config = load_config()
    client = LlmClient(config, cache_dirs=([Path(p) for p in cache_dirs] if cache_dirs is not None
                                         else [campaign.run_dir / "llm" / "cache", Path("artifacts/llm-cache")]),
                       offline=offline, max_calls=max_calls)
    abi = json.loads((campaign.bundle / "abi.json").read_text(encoding="utf-8"))
    goals = json.loads((campaign.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
    probe_audit = audit_probe_completeness(campaign.run_dir)
    probe_status_by_runtime: dict[int, set[str]] = {}
    for risk in probe_audit.get("risks", []):
        for runtime_id in risk.get("runtime_ids", []):
            if runtime_id is not None:
                probe_status_by_runtime.setdefault(int(runtime_id), set()).add(risk["status"])
    effect = goals.get("cycle_effects") or {}
    contract_id = campaign.config.get("contract_id")
    task_context: dict[str, dict[str, Any]] = {}
    task_limit = max(3, batch_size * 2)
    tasks = goals.get("tasks", [])
    planning_tasks = [task for task in tasks if task.get("goal_class") == "defect"]
    if not planning_tasks:
        planning_tasks = tasks[:task_limit] or [{"task_id": "coverage:abi", "goal": {},
                                                 "goal_class": "coverage"}]
    for task in planning_tasks:
        if len(task_context) >= task_limit:
            break
        goal = task.get("goal") or {}
        source = goal.get("source") or {}
        refs = []
        if isinstance(source, dict) and source.get("file"):
            refs.append(f"source:{source['file']}:{source.get('line', 0)}")
        if goal.get("stable_id"):
            refs.append(f"stg:{goal['stable_id']}")
        if not refs:
            refs.append(f"abi:{abi.get('target', {}).get('name', 'target')}")
        task_id = str(task["task_id"])
        task_context[task_id] = {
            "task_id": task_id, "goal": goal, "evidence_refs": refs,
            "probe_status": sorted(probe_status_by_runtime.get(goal.get("runtime_id"), []))
            or [probe_audit["status"]],
            "source_excerpt": campaign._source_excerpt(task),
            "candidate_paths": (task.get("candidate_paths") or [])[:8],
            "state_slice": task.get("state_slice") or [],
            "abstraction": task.get("abstraction") or [],
            "abstraction_fields": [item.get("field") for item in task.get("abstraction") or []],
            "controllable_fields": campaign._llm_controllable_fields(
                abi, set(task.get("controllable_fields") or []) or None),
            "reachable_prefix_ids": [], "symbolic_suitable": task.get("symbolic_suitable"),
            "modeling_status": task.get("plan_modeling_status"),
        }
    if not config.available and not offline:
        campaign.llm_planning_summary = {"status": "failed", "reason": "not_configured",
                                         "valid_plans": 0, "useful_plans": 0, "rounds": 0,
                                         "tokens": 0}
        campaign.metrics.event("llm_planning_failed", reason="not_configured")
        if campaign.config.get("llm_strict_planning"):
            raise RuntimeError("LLM planning required but not configured")
        return
    if max_calls == 0:
        campaign.llm_planning_summary = {"status": "failed", "reason": "no_call_budget",
                                         "valid_plans": 0, "useful_plans": 0, "rounds": 0,
                                         "tokens": 0}
        if campaign.config.get("llm_strict_planning"):
            raise RuntimeError("LLM planning required but no call budget")
        return
    examples = [_example(abi, contract_id, task_id, task["evidence_refs"])
                for task_id, task in list(task_context.items())[:1]]
    seen_actions: set[str] = set()
    native_hits: set[int] = set()
    native_states: set[str] = set()
    best_distance: dict[str, float] = {}
    feedback: list[dict[str, Any]] = []
    valid_count = 0
    native_valid_count = 0
    useful_count = 0
    seed_count = 0
    used_tokens = 0
    planning_deadline = min(campaign.deadline, getattr(campaign, "startup_deadline", campaign.deadline))
    coverage_only = not any(task.get("goal_class") == "defect" for task in tasks)
    max_frames = 1 if effect.get("mode") == "single_cycle" else 4 if effect.get("mode") == "unknown" else 128
    baseline_summary: dict[str, Any] = {"status": "not_observed"}
    try:
        baseline = campaign.model.build([campaign._default_action_frame(abi)])
        baseline_observation = campaign._probe_with_deadline(
            baseline, timeout_s=5.0, deadline=planning_deadline, trace=False)
        if baseline_observation.returncode == 0 and not baseline_observation.timed_out:
            native_hits.update(baseline_observation.hits)
            baseline_case = parse_testcase(baseline, campaign._abi_like())
            for task_id in task_context:
                state = campaign._solver_state_key(task_id, baseline_observation)
                if state is not None:
                    native_states.add(state)
                distance = campaign._solver_task_distance(
                    task_id, baseline_case, baseline_observation)
                if distance is not None:
                    best_distance[task_id] = distance
            baseline_summary = {"status": "cold_ok", "runtime_hits": sorted(native_hits),
                                "task_distances": dict(best_distance)}
        else:
            baseline_summary = {"status": "failed_or_timed_out",
                                "returncode": baseline_observation.returncode,
                                "timed_out": baseline_observation.timed_out}
    except (OSError, ValueError, TypeError) as error:
        campaign.metrics.event("llm_plan_baseline", status="FAILED", reason=str(error)[:300])
    solver_distances = getattr(campaign, "_solver_seed_best_distance", None)
    if solver_distances is None:
        solver_distances = campaign._solver_seed_best_distance = {}
    for task_id, distance in best_distance.items():
        solver_distances[task_id] = min(distance, solver_distances.get(task_id, float("inf")))
    completed_rounds = 0
    for round_index in range(1, rounds + 1):
        if (time.monotonic() >= planning_deadline or round_index > max_calls
                or used_tokens >= token_budget or useful_count >= batch_size
                or (coverage_only and native_valid_count > 0)):
            break
        completed_rounds += 1
        request = {
            "schema": {"contract_id": "same contract_id", "plans": [
                {"plan_id": "unique string", "task_id": "listed task_id",
                 "contract_id": "same contract_id", "priority": 1,
                 "evidence_refs": ["supplied reference"],
                 "suggested_symbolic_fields": ["controllable field"],
                 "solver_triggers": ["task_distance_improved"],
                 "actions": [{"op": "set|pulse|hold|wait|repeat"}]}]},
            "action_forms": [
                {"op": "set", "inputs": {"ABI_FIELD": "typed value"}},
                {"op": "pulse", "input": "BOOL_ABI_FIELD"},
                {"op": "hold", "inputs": {"ABI_FIELD": "typed value"}, "frames": 2},
                {"op": "wait", "frames": 2},
                {"op": "repeat", "times": 2,
                 "actions": [{"op": "set", "inputs": {"ABI_FIELD": "typed value"}}]},
            ],
            "contract_id": contract_id, "batch_size": batch_size,
            "round": round_index, "remaining_calls": max_calls - round_index + 1,
            "remaining_tokens": token_budget - used_tokens,
            "max_frames_per_plan": max_frames,
            "abi": {"target": abi.get("target"),
                    "frame_fields": abi.get("frame_fields"),
                    "shared_fields": [field for field in abi.get("shared_fields", [])
                                      if field.get("block_kind") == "VAR_IN_OUT"]},
            "cycle_effects": effect, "tasks": list(task_context.values()),
            "probe_audit": {"status": probe_audit["status"],
                            "risk_count": probe_audit.get("risk_count"),
                            "incomplete_count": probe_audit.get("incomplete_count"),
                            "platform_assumptions": probe_audit.get("platform_assumptions", [])},
            "valid_example": {"contract_id": contract_id, "plans": examples},
            "native_baseline": baseline_summary,
            "previous_feedback": feedback[-batch_size * 2:],
        }
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_MESSAGE},
            {"role": "user", "content": json.dumps(request, sort_keys=True)},
        ]
        try:
            remaining = planning_deadline - time.monotonic()
            call_deadline = (None if campaign.config.get("llm_preparation_unbounded") else
                             min(planning_deadline, time.monotonic() +
                                 min(30.0, remaining / (rounds - round_index + 1))))
            response = client.complete(messages, deadline=call_deadline)
            campaign.metrics.add("llm_calls", 1)
            tokens = int(response.usage.get("total_tokens", 0) or 0)
            campaign.metrics.add("llm_tokens", tokens)
            used_tokens += tokens
            raw_batch = json.loads(response.content.strip())
            accepted, rejected = validate_plan_batch(
                raw_batch, abi=abi, contract_id=contract_id, tasks=task_context,
                seen_actions=seen_actions, max_plans=batch_size, max_frames=max_frames)
        except (LlmError, PlanRejected, ValueError, TypeError, OSError) as error:
            rejected = [{"index": None, "plan": None, "reason": str(error)[:400]}]
            accepted = []
        round_feedback = list(rejected)
        for plan in sorted(accepted, key=lambda item: item.get("priority", 50)):
            valid_count += 1
            task_id = plan["task_id"]
            native_summary: dict[str, Any] = {}
            suggested = plan.get("suggested_symbolic_fields") or []
            member_parents = {name: entry.get("_parent")
                              for name, entry in _field_map(abi).items()
                              if entry.get("_parent")}
            symbolic_names = [member_parents.get(name, name) for name in suggested]
            supported, concretized = symbolic_scope(abi, symbolic_names)
            if concretized:
                campaign.metrics.event("llm_symbolic_suggestion", task_id=task_id,
                                       plan_id=plan["plan_id"], status="UNSUPPORTED",
                                       fields=concretized)
            try:
                frames = expand_plan(plan, campaign._default_action_frame(abi), abi,
                                     max_frames=max_frames)
                testcase = campaign.model.build(frames)
                observation = campaign._probe_with_deadline(
                    testcase, timeout_s=5.0, deadline=planning_deadline, trace=False)
                native_summary = {"returncode": observation.returncode,
                                  "timed_out": observation.timed_out,
                                  "scan_budget": observation.scan_budget,
                                  "runtime_hits": sorted(observation.hits),
                                  "frames": len(frames)}
                candidate_info = classify(observation, campaign.model.runtime_index)
                native_summary["goal_hit"] = is_finding_candidate(candidate_info)
                if is_finding_candidate(candidate_info):
                    # A native replay that already reaches a finding is a
                    # valid plan outcome.  Requiring a separate non-finding
                    # state made targets with shallow native findings fail
                    # planning before the engine could retain or confirm the
                    # candidate.
                    native_valid_count += 1
                    seed_dir = campaign.run_dir / "unified-corpus" / "seeds"
                    seed_dir.mkdir(parents=True, exist_ok=True)
                    seed_count += 1
                    (seed_dir / f"llm-plan-{seed_count:04}.seed").write_bytes(
                        canonicalize_raw(testcase, campaign._abi_like()))
                    campaign._register_candidate(testcase, observation, candidate_info,
                                                 provenance_source="llm", depth=len(frames))
                    useful_count += 1
                    reason = "planning_finding_candidate"
                elif observation.scan_budget:
                    reason = "instrumentation_abort_scan_budget"
                elif observation.timed_out or observation.returncode != 0:
                    reason = "native_replay_failed_or_timed_out"
                else:
                    native_valid_count += 1
                    state = campaign._solver_state_key(task_id, observation)
                    distance = campaign._solver_task_distance(
                        task_id, parse_testcase(testcase, campaign._abi_like()), observation)
                    novel_hits = set(observation.hits) - native_hits
                    novel_state = state is not None and state not in native_states
                    improved = (distance is not None and task_id in best_distance
                                and distance < best_distance[task_id])
                    goal_hit = False
                    native_summary.update({"new_runtime_hits": sorted(novel_hits),
                                           "new_valid_state": novel_state,
                                           "distance": distance,
                                           "best_distance_before": best_distance.get(task_id),
                                           "improved_distance": improved,
                                           "goal_hit": goal_hit})
                    gained = bool(novel_hits or novel_state or improved or goal_hit)
                    if gained or seed_count == 0:
                        seed_dir = campaign.run_dir / "unified-corpus" / "seeds"
                        seed_dir.mkdir(parents=True, exist_ok=True)
                        seed_id = seed_count + 1
                        (seed_dir / f"llm-plan-{seed_id:04}.seed").write_bytes(
                            canonicalize_raw(testcase, campaign._abi_like()))
                        seed_count += 1
                        if gained:
                            useful_count += 1
                        if supported:
                            existing = campaign.llm_symbolic_suggestions.setdefault(task_id, [])
                            existing.extend(name for name in supported if name not in existing)
                        campaign.llm_solver_hints[task_id] = {
                            "priority": plan.get("priority", 50),
                            "triggers": plan.get("solver_triggers") or [],
                        }
                        native_hits.update(observation.hits)
                        if state is not None:
                            native_states.add(state)
                        if distance is not None:
                            best_distance[task_id] = min(distance, best_distance.get(task_id, distance))
                            solver_distances[task_id] = min(distance, solver_distances.get(task_id, distance))
                        reason = "native_gain" if gained else "native_valid_seed"
                        campaign.metrics.event("llm_plan_seed", task_id=task_id,
                                               plan_id=plan["plan_id"], seed_id=seed_id,
                                               new_hits=sorted(novel_hits), new_state=novel_state,
                                               improved_distance=improved, goal_hit=goal_hit)
                    else:
                        reason = "no_new_probe_state_or_distance"
            except Exception as error:  # noqa: BLE001 - baseline search remains available
                reason = f"native_plan_error: {str(error)[:300]}"
            round_feedback.append({"plan": plan, "task_id": task_id,
                                   "status": ("useful" if reason == "native_gain" else
                                              "valid" if reason == "native_valid_seed" else "rejected"),
                                   "reason": reason, "native_replay": native_summary})
        feedback = round_feedback
        campaign.metrics.event("llm_plan_round", round=round_index,
                               accepted=len(accepted), rejected=len(rejected),
                               useful=sum(item.get("status") == "useful" for item in feedback),
                               feedback=feedback)
    campaign.llm_planning_summary = {
        "status": "useful" if useful_count else "native_valid" if native_valid_count else "failed",
        "valid_plans": valid_count, "native_valid_plans": native_valid_count,
        "useful_plans": useful_count, "seed_files": seed_count,
        "rounds": completed_rounds, "tokens": used_tokens,
    }
    campaign.metrics.event("llm_planning_finished" if native_valid_count else "llm_planning_failed",
                           **campaign.llm_planning_summary)
    if not native_valid_count and campaign.config.get("llm_strict_planning"):
        raise RuntimeError("LLM planning required but produced no native-valid plan")
