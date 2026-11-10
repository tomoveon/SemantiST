"""Aggregate the frozen matrix without hiding unsupported targets."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from fuzzer.experiments.artifacts import build_path, plan_path, write_json
from fuzzer.experiments.matrix import CONFIGURATIONS, Job
from fuzzer.experiments.worker import result_path


def _location_key(target_id: str, finding: dict) -> str:
    source = finding.get("source") or {}
    return "|".join((target_id, str(finding.get("oracle") or ""),
                     str(source.get("file") or ""),
                     str(source.get("start_line") or source.get("line") or "")))


def _median(values: list[float | int]) -> float | None:
    return statistics.median(values) if values else None


def _first_time(result: dict, key: str) -> float | None:
    values = [finding.get(key) for finding in result.get("findings", [])]
    numeric = [value for value in values if isinstance(value, (int, float))]
    return min(numeric) if numeric else None


def _preparation_costs(root: Path, entries: list[tuple[Job, dict]],
                       tool: str, configuration: str) -> dict:
    targets = {job.target["id"] for job, _ in entries}
    common = []
    external = []
    planning = []
    offline_klee = []
    planning_review = []
    tokens = []
    for target_id in targets:
        common_file = build_path(root, target_id) / "build.json"
        if common_file.is_file():
            common.append(json.loads(common_file.read_text(encoding="utf-8")).get("build_seconds"))
        if tool != "semantist":
            adapter_file = root / "builds" / target_id / tool / "build-result.json"
            if adapter_file.is_file():
                external.append(json.loads(adapter_file.read_text(encoding="utf-8")).get("build_seconds"))
        elif CONFIGURATIONS[configuration]["llm"]:
            plan_file = plan_path(root, target_id) / "plan.json"
            if plan_file.is_file():
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
                planning.append(plan.get("planning_seconds"))
                offline_klee.append(plan.get("offline_solver_seconds"))
                planning_review.append(plan.get("planning_review_seconds"))
                tokens.append(plan.get("tokens"))

    def total(values: list, count: int) -> float | None:
        if len(values) != count or not all(isinstance(value, (int, float)) for value in values):
            return None
        return round(sum(values), 3)

    llm = tool == "semantist" and CONFIGURATIONS[configuration]["llm"]
    return {
        "unique_common_build_seconds": total(common, len(targets)),
        "unique_adapter_build_seconds": total(external, len(targets)) if tool != "semantist" else None,
        "unique_llm_planning_seconds": total(planning, len(targets)) if llm else 0,
        "unique_offline_klee_seconds": total(offline_klee, len(targets)) if llm else 0,
        "unique_llm_planning_review_seconds": total(planning_review, len(targets)) if llm else 0,
        "unique_llm_tokens": total(tokens, len(targets)) if llm else 0,
    }


def summarize(root: Path, jobs: list[Job]) -> dict:
    adjudication_file = root / "root-causes.json"
    adjudication = (json.loads(adjudication_file.read_text(encoding="utf-8"))
                    if adjudication_file.is_file() else {})
    rows = defaultdict(list)
    cases = {}
    pending_locations = set()
    planning_by_target = {}
    for job in jobs:
        target_id = job.target["id"]
        if (job.tool != "semantist" or not CONFIGURATIONS[job.configuration]["llm"]
                or target_id in planning_by_target):
            continue
        plan_file = plan_path(root, target_id) / "plan.json"
        plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.is_file() else {}
        planning_by_target[target_id] = plan.get("planning_confirmed", [])
        for finding in planning_by_target[target_id]:
            normalized = {"oracle": finding.get("oracle"),
                          "source": (finding.get("goal") or {}).get("source")}
            location = _location_key(target_id, normalized)
            case = cases.setdefault(location, {
                "location_key": location, "target_id": target_id,
                "oracle": normalized["oracle"], "source": normalized["source"],
                "root_cause_id": adjudication.get(location), "occurrences": [],
            })
            case["occurrences"].append({
                "phase": "planning", "plan_id": plan.get("plan_id"),
                "first_candidate_planning_s": finding.get("first_candidate_s"),
                "repro_dir": str(plan_path(root, target_id).relative_to(root)
                                 / "findings" / "confirmed" / str(finding.get("finding_id", ""))),
            })
    for job in jobs:
        path = result_path(root, job)
        result = (json.loads(path.read_text(encoding="utf-8")) if path.is_file()
                  else {"status": "not_run", "supported": False, "findings": []})
        if result.get("status") == "completed" and result.get("supported"):
            for finding in result.get("findings", []):
                location = _location_key(job.target["id"], finding)
                case = cases.setdefault(location, {
                    "location_key": location, "target_id": job.target["id"],
                    "oracle": finding.get("oracle"), "source": finding.get("source"),
                    "root_cause_id": adjudication.get(location), "occurrences": [],
                })
                case["occurrences"].append({
                    "phase": "online_search", "job_id": job.job_id, "tool": job.tool,
                    "first_candidate_online_s": finding.get("first_candidate_online_s"),
                    "first_candidate_end_to_end_s": finding.get("first_candidate_end_to_end_s"),
                    "first_candidate_unamortized_s": finding.get("first_candidate_unamortized_s"),
                    "repro_dir": finding.get("repro_dir"),
                    "testcase_file": finding.get("testcase_file"),
                })
        for experiment in job.experiments:
            rows[(experiment, job.tool, job.configuration, job.online_seconds)].append((job, result))
    groups = []
    per_target = []
    for (experiment, tool, configuration, seconds), entries in sorted(rows.items()):
        supported = [(job, result) for job, result in entries
                     if result.get("status") == "completed" and result.get("supported")]
        found = [(job, result) for job, result in supported if result.get("confirmed", 0) > 0]
        times = [_first_time(result, "first_candidate_online_s") for _, result in supported]
        end_to_end_times = [_first_time(result, "first_candidate_end_to_end_s")
                            for _, result in supported]
        unamortized_times = [_first_time(result, "first_candidate_unamortized_s")
                             for _, result in supported]
        root_ids = set()
        planning_locations = set()
        if tool == "semantist" and CONFIGURATIONS[configuration]["llm"]:
            for target_id in {job.target["id"] for job, _ in entries}:
                for finding in planning_by_target.get(target_id, []):
                    planning_locations.add(_location_key(target_id, {
                        "oracle": finding.get("oracle"),
                        "source": (finding.get("goal") or {}).get("source")}))
        planning_root_ids = {adjudication[location] for location in planning_locations
                             if location in adjudication}
        pending_locations.update(location for location in planning_locations
                                 if location not in adjudication)
        for job, result in supported:
            for finding in result.get("findings", []):
                location = _location_key(job.target["id"], finding)
                if location in adjudication:
                    root_ids.add(adjudication[location])
                else:
                    pending_locations.add(location)
        cpu = [result["cpu_seconds"] for _, result in supported
               if isinstance(result.get("cpu_seconds"), (float, int))]
        memory = [result["peak_memory_bytes"] for _, result in supported
                  if isinstance(result.get("peak_memory_bytes"), int)]
        throughput = [result["scans"] / result["phase_times_s"]["online_search"]
                      for _, result in supported
                      if isinstance(result.get("scans"), (float, int))
                      and result.get("phase_times_s", {}).get("online_search", 0) > 0]
        execution_rates = [result["executions"] / result["phase_times_s"]["online_search"]
                           for _, result in supported
                           if isinstance(result.get("executions"), (float, int))
                           and result.get("phase_times_s", {}).get("online_search", 0) > 0]
        online_times = [result["phase_times_s"]["online_search"] for _, result in supported
                        if isinstance(result.get("phase_times_s", {}).get("online_search"), (int, float))]
        review_times = [result["phase_times_s"]["result_review"] for _, result in supported
                        if isinstance(result.get("phase_times_s", {}).get("result_review"), (int, float))]
        reasons = defaultdict(int)
        for _, result in entries:
            if result.get("status") != "completed" and result.get("reason"):
                reasons[str(result["reason"])] += 1
        status_counts = {status: sum(result.get("status") == status for _, result in entries)
                         for status in sorted({result.get("status", "not_run") for _, result in entries})}
        target_ids = {job.target["id"] for job, _ in entries}
        supported_target_ids = {job.target["id"] for job, _ in supported}
        groups.append({
            "experiment": experiment, "tool": tool, "configuration": configuration,
            "online_seconds": seconds, "planned_runs": len(entries),
            "status_counts": status_counts,
            "incomplete_reasons": dict(sorted(reasons.items())),
            "supported_runs": len(supported), "support_rate": len(supported) / len(entries),
            "planned_targets": len(target_ids), "supported_targets": len(supported_target_ids),
            "target_support_rate": len(supported_target_ids) / len(target_ids),
            "runs_with_confirmed": len(found),
            "native_crash_events": sum(result.get("native_crash_events") or 0 for _, result in supported),
            "native_sequence_saved": sum(result.get("native_sequence_saved") or 0 for _, result in supported),
            "discovery_rate_all": (len(found) / len(entries)
                                   if len(supported) == len(entries) else None),
            "discovery_rate_supported": len(found) / len(supported) if supported else None,
            "median_first_candidate_online_s": _median([value for value in times if value is not None]),
            "median_first_candidate_end_to_end_s": _median([value for value in end_to_end_times if value is not None]),
            "median_first_candidate_unamortized_s": _median([value for value in unamortized_times if value is not None]),
            "independent_defects_adjudicated": len(root_ids),
            "planning_stage_confirmed_locations": len(planning_locations),
            "all_pipeline_independent_defects_adjudicated": len(root_ids | planning_root_ids),
            "median_cpu_seconds": _median(cpu),
            "median_peak_memory_bytes": _median(memory),
            "median_online_seconds": _median(online_times),
            "median_review_seconds": _median(review_times),
            "median_scans_per_second": _median(throughput),
            "median_executions_per_second": _median(execution_rates),
            "feedback_enqueued": sum(result.get("feedback", {}).get("enqueued", 0) for _, result in supported),
            "feedback_executed": sum(result.get("feedback", {}).get("executed", 0) for _, result in supported),
            "feedback_retained": sum(result.get("feedback", {}).get("retained", 0) for _, result in supported),
            "native_edge_novelty": sum(result.get("feedback", {}).get("native_edges", 0)
                                        for _, result in supported),
            "state_progress": sum(result.get("feedback", {}).get("state_progress", 0)
                                   for _, result in supported),
            "finding_candidates": sum(result.get("feedback", {}).get("finding_candidates", 0)
                                       for _, result in supported),
            "shared_corpus_runs": sum(bool(result.get("feedback", {}).get("shared_corpus"))
                                      for _, result in supported),
            "timeouts": sum(result.get("timeouts", 0) for _, result in supported),
            "run_failures": sum(result.get("run_failures", 0) for _, result in entries),
            "semantic_mutations": sum(result.get("mutation", {}).get("semantic_mutations", 0)
                                       for _, result in supported),
            "raw_mutations": sum(result.get("mutation", {}).get("raw_mutations", 0)
                                  for _, result in supported),
            "raw_executions": sum(result.get("mutation", {}).get("raw_executions", 0)
                                   for _, result in supported),
            "raw_retained": sum(result.get("mutation", {}).get("raw_retained", 0)
                                 for _, result in supported),
            "parser_rejects": sum(result.get("mutation", {}).get("parser_rejects", 0)
                                   for _, result in supported),
            **_preparation_costs(root, entries, tool, configuration),
        })
        by_target = defaultdict(list)
        for job, result in entries:
            by_target[job.target["id"]].append(result)
        for target_id, trials in sorted(by_target.items()):
            target_supported = [item for item in trials
                                if item.get("status") == "completed" and item.get("supported")]
            target_found = sum(item.get("confirmed", 0) > 0 for item in target_supported)
            per_target.append({
                "experiment": experiment, "tool": tool,
                "configuration": configuration, "online_seconds": seconds,
                "target_id": target_id, "planned_runs": len(trials),
                "status_counts": {status: sum(item.get("status") == status for item in trials)
                                  for status in sorted({item.get("status", "not_run")
                                                        for item in trials})},
                "supported_runs": len(target_supported),
                "runs_with_confirmed": target_found,
                "native_crash_events": sum(item.get("native_crash_events") or 0
                                           for item in target_supported),
                "native_sequence_saved": sum(item.get("native_sequence_saved") or 0
                                               for item in target_supported),
                "discovery_rate_all": (target_found / len(trials)
                                       if len(target_supported) == len(trials) else None),
                "discovery_rate_supported": target_found / len(target_supported)
                if target_supported else None,
                "median_first_candidate_online_s": _median([
                    value for item in target_supported
                    if (value := _first_time(item, "first_candidate_online_s")) is not None]),
                "planning_stage_confirmed_locations": len({
                    _location_key(target_id, {"oracle": finding.get("oracle"),
                                              "source": (finding.get("goal") or {}).get("source")})
                    for finding in planning_by_target.get(target_id, [])})
                if tool == "semantist" and CONFIGURATIONS[configuration]["llm"] else 0,
            })
    summary = {
        "schema_version": "semantist.experiment-summary/1",
        "planned_unique_runs": len(jobs),
        "groups": groups,
        "unadjudicated_locations": sorted(pending_locations),
        "independent_defects_complete": not pending_locations,
    }
    write_json(root / "summary.json", summary)
    write_json(root / "per-target.json", per_target)
    write_json(root / "cases.json", [cases[key] for key in sorted(cases)])
    return summary
