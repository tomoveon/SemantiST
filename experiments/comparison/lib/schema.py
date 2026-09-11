"""Run-result schema helpers."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RUN_SCHEMA_VERSION = "semantist.full-comparison-run/1.0.0"
RUN_STATUSES = {
    "budget_completed",
    "early_completed_with_finding",
    "early_completed_no_finding",
    "unsupported",
    "preprocessing_failed",
    "online_start_failed",
    "online_crashed",
    "watchdog_timeout",
    "runner_error",
    "missing",
}


def failure_reason(code: str, message: str, log_tail_path: str | None = None) -> dict[str, object]:
    return {"code": code, "message": message, "log_tail_path": log_tail_path}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run_result_template(
    *,
    experiment_id: str,
    run_key: str,
    tool: str,
    target: Any,
    trial_id: int,
    rng_seed_requested: int,
    rng_seed_applied: int | None,
    rng_seed_status: str,
    online_budget_seconds: int,
    per_execution_timeout_ms: int | None,
    execution_timeout_model: str,
    support_status: str,
    observer_poll_interval_ms: int = 10,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "run_key": run_key,
        "tool": tool,
        "configuration_id": "full",
        "target_id": target.target_id,
        "suite": target.suite,
        "kind": target.kind,
        "function": target.function,
        "trial_id": trial_id,
        "rng_seed_requested": rng_seed_requested,
        "rng_seed_applied": rng_seed_applied,
        "rng_seed_status": rng_seed_status,
        "support_status": support_status,
        "preprocessing_status": None,
        "preprocessing_start_monotonic_ns": None,
        "preprocessing_end_monotonic_ns": None,
        "preprocessing_time_seconds": None,
        "online_budget_seconds": online_budget_seconds,
        "t0_monotonic_ns": None,
        "observer_poll_interval_ms": observer_poll_interval_ms,
        "online_elapsed_seconds": None,
        "per_execution_timeout_ms": per_execution_timeout_ms,
        "execution_timeout_model": execution_timeout_model,
        "run_status": "missing",
        "finding_found": False,
        "common_oracle_finding_found": False,
        "semantic_objective_found": False,
        "first_common_finding": None,
        "first_semantic_objective": None,
        "candidate_findings": [],
        "unique_confirmed_fault_ids": [],
        "total_executions": None,
        "execs_per_sec": None,
        "corpus_count": 0,
        "crash_count_raw": 0,
        "timeout_count_raw": 0,
        "common_replay": {
            "status": "not-run",
            "confirmed": False,
            "oracle": None,
        },
        "common_coverage": {
            "status": "not-run",
            "covered_slots": None,
            "covered_slots_sha256": None,
            "conversion_failures": 0,
        },
        "tool_specific_metrics": {},
        "artifact_paths": {},
        "failure_reason": None,
    }


def validate_run_result(value: dict[str, Any]) -> None:
    schema = _run_schema()
    status = value.get("run_status")
    if status not in RUN_STATUSES:
        raise ValueError(f"invalid run_status: {status!r}")
    required = set(schema.get("required", []))
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"run result missing fields: {missing}")

    properties = schema.get("properties", {})
    for field, rules in properties.items():
        if field not in value:
            continue
        if "const" in rules and value[field] != rules["const"]:
            raise ValueError(f"{field} must be {rules['const']!r}")
        if "enum" in rules and value[field] not in rules["enum"]:
            raise ValueError(f"invalid {field}: {value[field]!r}")
        expected_type = rules.get("type")
        if expected_type and not _matches_json_type(value[field], expected_type):
            raise ValueError(f"{field} must be JSON type {expected_type}")

    poll_interval = value.get("observer_poll_interval_ms")
    if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or not 1 <= poll_interval <= 10:
        raise ValueError("observer_poll_interval_ms must be an integer in [1, 10]")
    candidates = value.get("candidate_findings")
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        raise ValueError("candidate_findings must be an array of objects")
    if value.get("finding_found") != bool(candidates):
        raise ValueError("finding_found must match candidate_findings")
    if value.get("common_oracle_finding_found") != (value.get("first_common_finding") is not None):
        raise ValueError("common_oracle_finding_found must match first_common_finding")
    if value.get("semantic_objective_found") != (value.get("first_semantic_objective") is not None):
        raise ValueError("semantic_objective_found must match first_semantic_objective")
    failure_statuses = {
        "unsupported",
        "preprocessing_failed",
        "online_start_failed",
        "online_crashed",
        "watchdog_timeout",
        "runner_error",
    }
    if status in failure_statuses and not isinstance(value.get("failure_reason"), dict):
        raise ValueError(f"{status} requires failure_reason")


@lru_cache(maxsize=1)
def _run_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "metrics_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _matches_json_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks.get(expected, lambda _item: True)(value)
