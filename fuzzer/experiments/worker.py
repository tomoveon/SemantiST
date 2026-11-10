"""Run one isolated trial and emit the shared result contract."""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from fuzzer.experiments.artifacts import (_contract_id, _copy_template, build_path, plan_path,
                                          validate_build_inputs, write_json)
from fuzzer.experiments.matrix import CONFIGURATIONS, Job, ROOT, validate_job
from fuzzer.pipeline.campaign import Campaign


def result_path(root: Path, job: Job) -> Path:
    return root / "runs" / job.job_id / "result.json"


def _resource_snapshot() -> tuple[float, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime
    try:
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text(encoding="ascii").splitlines():
            key, value = line.split()
            if key == "usage_usec":
                cpu = int(value) / 1_000_000
                break
    except (OSError, ValueError):
        pass
    peak_file = Path("/sys/fs/cgroup/memory.peak")
    try:
        peak = int(peak_file.read_text(encoding="ascii"))
    except (OSError, ValueError):
        peak = max(usage.ru_maxrss, children.ru_maxrss) * 1024
    return cpu, peak


def _semantist_trial(root: Path, job: Job, run_dir: Path,
                     review_seconds: int,
                     mutation_mode: str | None = None,
                     semantic_ratio: float = 0.30) -> dict[str, Any]:
    build = build_path(root, job.target["id"])
    if not (build / "build.json").is_file():
        return {"status": "prerequisite_missing", "reason": f"missing build package: {build}"}
    validate_build_inputs(build, job.target)
    build_metadata = json.loads((build / "build.json").read_text(encoding="utf-8"))
    _copy_template(build, run_dir)
    flags = dict(CONFIGURATIONS[job.configuration])
    # An LLM ablation still uses the same STG tasks and native execution path.
    prepared = None
    if flags["llm"]:
        frozen_plan = plan_path(root, job.target["id"])
        plan_file = frozen_plan / "plan.json"
        if not plan_file.is_file():
            return {"status": "prerequisite_missing", "reason": f"missing frozen LLM plan: {plan_file}"}
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        if (plan.get("calls", 0) < 1 or
                (plan.get("summary") or {}).get("native_valid_plans", 0) < 1 or
                not plan.get("seed_files")):
            return {"status": "prerequisite_missing", "reason": "frozen LLM plan lacks native validation"}
        source_seeds = frozen_plan / "unified-corpus" / "seeds"
        destination = run_dir / "unified-corpus" / "seeds"
        for name in plan["seed_files"]:
            shutil.copy2(source_seeds / name, destination / name)
        prepared = {"plan_id": plan["plan_id"], "summary": plan["summary"],
                    "symbolic_suggestions": plan["symbolic_suggestions"],
                    "solver_hints": plan.get("solver_hints") or {}}
        flags["symbolic_start"] = "replay"
    config: dict[str, Any] = {
        "mode": "hybrid", "engine": "rust", "function": job.target["function"],
        "contract_id": _contract_id(run_dir), "ablations": flags,
        "prepared_llm": prepared,
        "online_budget_from_start": True,
        "full_hybrid_duration": True,
        "symbolic_startup_budget_s": min(30.0, float(job.online_seconds) * 0.1),
        "symbolic_startup_max_queries": 8,
        "online_symbolic_max_queries": 4,
        "solver_feedback_margin_s": min(15.0, max(1.0, float(job.online_seconds) * 0.05)),
        "review_budget_s": review_seconds,
        "seed": job.seed, "fuzz_timeout": job.online_seconds,
        "max_evaluations": None,
        "mutation_mode": mutation_mode or "combined",
        "semantic_ratio": semantic_ratio,
    }
    write_json(run_dir / "campaign.json", config)
    campaign = Campaign(run_dir, run_dir / "bundle", build / "build" / "fuzz_target",
                        function=job.target["function"],
                        st_file=build / "source" / "target.st",
                        config=config, time_budget_s=float(job.online_seconds),
                        jobs=2, seed=job.seed, max_evaluations=40000)
    previous_mutation_mode = os.environ.get("SEMANTIST_MUTATION_MODE")
    previous_semantic_ratio = os.environ.get("SEMANTIST_SEMANTIC_RATIO")
    os.environ["SEMANTIST_MUTATION_MODE"] = mutation_mode or "combined"
    os.environ["SEMANTIST_SEMANTIC_RATIO"] = str(semantic_ratio)
    try:
        campaign.run()
    finally:
        if previous_mutation_mode is None:
            os.environ.pop("SEMANTIST_MUTATION_MODE", None)
        else:
            os.environ["SEMANTIST_MUTATION_MODE"] = previous_mutation_mode
        if previous_semantic_ratio is None:
            os.environ.pop("SEMANTIST_SEMANTIC_RATIO", None)
        else:
            os.environ["SEMANTIST_SEMANTIC_RATIO"] = previous_semantic_ratio
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    engine = json.loads((run_dir / "metrics" / "engine-summary.json").read_text(encoding="utf-8"))
    solver_feedback = report.get("solver_feedback") or {}
    online_start = report.get("online_start_offset_s") or 0.0
    all_findings = report.get("findings", [])
    findings = [item for item in all_findings
                if isinstance(item.get("first_candidate_s"), (int, float))
                and 0 <= item["first_candidate_s"] - online_start <= job.online_seconds]
    preparation_costs = {
        "common_build_s": build_metadata.get("build_seconds"),
        "adapter_build_s": None,
        "llm_planning_s": plan.get("planning_seconds") if prepared else 0,
        "offline_klee_s": plan.get("offline_solver_seconds") if prepared else 0,
        "planning_review_s": plan.get("planning_review_seconds") if prepared else 0,
    }
    shared = preparation_costs["common_build_s"]
    own = [preparation_costs[key] for key in
           ("llm_planning_s", "offline_klee_s", "planning_review_s")]
    amortized = (shared / 15 + sum(own) / 5
                 if isinstance(shared, (int, float))
                 and all(isinstance(value, (int, float)) for value in own) else None)
    unamortized = (shared + sum(own) if amortized is not None else None)
    confirmed_findings = []
    for item in findings:
        candidate_online_s = max(0.0, round(item.get("first_candidate_s", 0) - online_start, 3))
        confirmed_findings.append({
            "oracle": item.get("oracle"), "source": item.get("goal", {}).get("source"),
            "first_candidate_online_s": candidate_online_s,
            "first_candidate_end_to_end_s": (round(amortized + candidate_online_s, 3)
                                                if amortized is not None else None),
            "first_candidate_unamortized_s": (round(unamortized + candidate_online_s, 3)
                                               if unamortized is not None else None),
            "repro_dir": f"findings/confirmed/{item.get('finding_id', '')}",
        })
    return {
        "status": "completed" if engine.get("executions", 0) else "failed",
        "reason": None if engine.get("executions", 0) else "native engine completed without executing an input",
        "supported": True,
        "symbolic_support": (report.get("execution_status") or {}).get("symbolic"),
        "confirmed": len(findings),
        "excluded_outside_online_findings": len(all_findings) - len(findings),
        "findings": confirmed_findings,
        "executions": engine.get("executions", 0), "scans": engine.get("scans", 0),
        "timeouts": engine.get("timeouts", 0),
        "run_failures": int(not engine.get("executions", 0)),
        "phase_times_s": campaign.phase_times_s,
        "preparation_costs_s": preparation_costs,
        "llm_plan_id": prepared["plan_id"] if prepared else None,
        "llm_planning_cost": (plan.get("planning_seconds") if prepared else None),
        "llm_planning_review_cost": (plan.get("planning_review_seconds") if prepared else None),
        "llm_tokens": (plan.get("tokens") if prepared else 0),
        "planning_stage_confirmed": len(plan.get("planning_confirmed", [])) if prepared else 0,
        "feedback": {
            "native_edges": engine.get("native_edge_novelty", 0),
            "state_progress": engine.get("state_transition_novelty", 0),
            "finding_candidates": engine.get("candidates", 0),
            "shared_corpus": True,
            "enqueued": solver_feedback.get("enqueued", 0),
            "executed": engine.get("solver_seeds_executed", 0),
            "retained": engine.get("solver_seeds_retained", 0),
            "events_file": "metrics/events.jsonl",
        },
        "mutation": {
            "mode": engine.get("mutation_mode", mutation_mode or "combined"),
            "semantic_ratio": engine.get("semantic_ratio", semantic_ratio),
            "semantic_mutations": engine.get("semantic_mutations", 0),
            "raw_mutations": engine.get("raw_mutations", 0),
            "raw_executions": engine.get("raw_executions", 0),
            "raw_retained": engine.get("raw_retained", 0),
            "parser_rejects": engine.get("parser_rejects", 0),
            "timeouts": engine.get("timeouts", 0),
        },
        "model_scope": report.get("model_scope", {}),
    }


def _external_trial(root: Path, job: Job, run_dir: Path,
                    adapter_config: dict[str, Any], review_seconds: int) -> dict[str, Any]:
    tool = adapter_config.get(job.tool) or {}
    command = tool.get("run_command")
    build = root / "builds" / job.target["id"] / job.tool
    if not command or not (build / "build-result.json").is_file():
        return {"status": "prerequisite_missing", "supported": False,
                "reason": f"{job.tool} adapter or build package is unavailable"}
    build_result = json.loads((build / "build-result.json").read_text(encoding="utf-8"))
    if (build_result.get("status") != "supported"
            or build_result.get("canonical_testcases") is not True
            or build_result.get("oracle_equivalent") is not True
            or not isinstance(build_result.get("version"), str)
            or not build_result["version"].strip()):
        return {"status": "unsupported", "supported": False,
                "reason": build_result.get("reason") or
                "baseline version, canonical input or oracle equivalence unavailable"}
    common_build = build_path(root, job.target["id"])
    if not (common_build / "build.json").is_file():
        return {"status": "prerequisite_missing", "supported": False,
                "reason": "common native replay build is missing"}
    validate_build_inputs(common_build, job.target)
    run_dir.mkdir(parents=True)
    variables = {
        "common_build": str(common_build),
        "source": str(build_path(root, job.target["id"]) / "source" / "target.st"),
        "function": job.target["function"], "seed": str(job.seed),
        "seconds": str(job.online_seconds), "output": str(run_dir),
        "build": str(build),
        "initial_corpus": str(build_path(root, job.target["id"]) / "template" / "unified-corpus" / "seeds"),
        "abi": str(build_path(root, job.target["id"]) / "template" / "bundle" / "abi.json"),
        "contract": str(build_path(root, job.target["id"]) / "template" / "bundle" / "execution-contract.json"),
        "replay_binary": str(common_build / "build" / "common" / "fuzz_target"),
    }
    argv = [str(token).format_map(variables) for token in command]
    started = time.monotonic()
    with (run_dir / "adapter.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=job.online_seconds + 60, check=False)
    adapter_elapsed = round(time.monotonic() - started, 3)
    if completed.returncode:
        return {"status": "failed", "supported": True,
                "reason": f"{job.tool} adapter exited {completed.returncode}"}
    output_file = run_dir / "adapter-result.json"
    if not output_file.is_file():
        return {"status": "failed", "supported": True,
                "reason": "adapter did not write adapter-result.json"}
    output = json.loads(output_file.read_text(encoding="utf-8"))
    if output.get("status") == "unsupported":
        return {"status": "unsupported", "supported": False,
                "reason": output.get("reason", "adapter cannot execute this target")}
    if output.get("status") != "completed":
        return {"status": "failed", "supported": False,
                "reason": output.get("reason", "adapter did not report completion")}
    testcase_format = output.get("testcase_format")
    if testcase_format != "common-raw" or output.get("cold_reset") is not True:
        return {"status": "unsupported", "supported": False,
                "reason": "adapter did not provide canonical testcase and cold-reset evidence"}
    online_elapsed = output.get("online_elapsed_s")
    if (not isinstance(online_elapsed, (int, float)) or online_elapsed < 0
            or online_elapsed > job.online_seconds + 1):
        return {"status": "failed", "supported": True,
                "reason": "adapter did not report a valid online search duration"}
    # Baseline candidates are fixed-size ABI bytes.  The adapter already
    # replayed each raw file with the native baseline binary; replaying via
    # SemantiST's semantic probe would change the input protocol and the
    # instrumentation being measured, so no semantic parser or runtime index
    # is consulted here.
    review_started = time.monotonic()
    findings = []
    for candidate in output.get("candidates", []):
        candidate_time = candidate.get("first_candidate_s")
        relative_file = Path(candidate.get("testcase_file", ""))
        replay = candidate.get("replay") or {}
        if (not isinstance(candidate_time, (int, float)) or candidate_time < 0
                or candidate_time > job.online_seconds or relative_file.is_absolute()
                or ".." in relative_file.parts or not isinstance(replay, dict)
                or replay.get("timed_out")):
            continue
        if not (run_dir / relative_file).is_file():
            continue
        returncode = replay.get("returncode")
        if not isinstance(returncode, int) or returncode == 0:
            continue
        sequence_file = candidate.get("sequence_file")
        if sequence_file is not None:
            if not isinstance(sequence_file, str):
                continue
            sequence_path = Path(sequence_file)
            if (sequence_path.is_absolute() or ".." in sequence_path.parts
                    or not (run_dir / sequence_path).is_file()):
                continue
        findings.append({
            "oracle": "native_crash",
            "source": None,
            "first_candidate_online_s": candidate_time,
            "testcase_file": sequence_file or str(relative_file),
            "raw_testcase_file": str(relative_file),
            "scan_cycles": candidate.get("scan_cycles"),
            "sequence_frames": candidate.get("sequence_frames"),
            "replay_returncode": returncode,
        })
    common_metadata = json.loads((common_build / "build.json").read_text(encoding="utf-8"))
    preparation_costs = {"common_build_s": common_metadata.get("build_seconds"),
                         "adapter_build_s": build_result.get("build_seconds"),
                         "llm_planning_s": 0, "offline_klee_s": 0,
                         "planning_review_s": 0}
    common_seconds = preparation_costs["common_build_s"]
    adapter_seconds = preparation_costs["adapter_build_s"]
    amortized = (common_seconds / 15 + adapter_seconds / 5
                 if isinstance(common_seconds, (int, float))
                 and isinstance(adapter_seconds, (int, float)) else None)
    unamortized = (common_seconds + adapter_seconds if amortized is not None else None)
    return {"status": "completed", "supported": True,
            "confirmed": len(findings),
            "native_crash_events": output.get("native_crash_events"),
            "native_sequence_saved": output.get("native_sequence_saved"),
            "findings": [{**finding,
                          "first_candidate_end_to_end_s": (
                              round(amortized + finding["first_candidate_online_s"], 3)
                              if amortized is not None else None),
                          "first_candidate_unamortized_s": (
                              round(unamortized + finding["first_candidate_online_s"], 3)
                              if unamortized is not None else None)}
                         for finding in findings],
            "preparation_costs_s": preparation_costs,
            "executions": output.get("executions"), "scans": output.get("scans"),
            "phase_times_s": {"online_search": round(online_elapsed, 3),
                              "adapter_postprocess": round(max(0, adapter_elapsed - online_elapsed), 3),
                              "result_review": round(time.monotonic() - review_started, 3)},
            "adapter_version": build_result.get("version")}
def run_job(root: Path, job: Job, *, adapter_config: dict[str, Any] | None = None,
            review_seconds: int = 90,
            mutation_mode: str | None = None,
            semantic_ratio: float = 0.30) -> dict[str, Any]:
    validate_job(job)
    output = result_path(root, job)
    if output.is_file():
        return json.loads(output.read_text(encoding="utf-8"))
    run_dir = output.parent
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"incomplete trial directory: {run_dir}")
    started = time.monotonic()
    before_cpu, before_peak = _resource_snapshot()
    try:
        if job.tool == "semantist":
            details = _semantist_trial(root, job, run_dir, review_seconds, mutation_mode,
                                       semantic_ratio)
        else:
            details = _external_trial(root, job, run_dir, adapter_config or {}, review_seconds)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        details = {"status": "failed", "supported": False, "reason": str(error)[:500]}
    after_cpu, after_peak = _resource_snapshot()
    result = {**job.as_dict(), **details,
              "mutation_mode": mutation_mode or "combined",
              "semantic_ratio": semantic_ratio,
              "wall_seconds": round(time.monotonic() - started, 3),
              "cpu_seconds": round(max(0.0, after_cpu - before_cpu), 3),
              "peak_memory_bytes": max(before_peak, after_peak)}
    if result.get("status") == "failed":
        result.setdefault("run_failures", 1)
    write_json(output, result)
    return result
