"""Small measurement contract shared by independent tool runs.

Native candidates and replay-confirmed findings are deliberately separate.
Counts are executor invocations (testcases), not PLC scan cycles. Wall time is
measured by the host from online launch, including native seed generation.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

CONTRACT = "semantist.independent-tool-metrics/1"
CRASH_ORACLES = {"asan", "ubsan", "target_signal"}
VALID_STATUSES = {"budget_completed", "early_completed_with_finding", "early_completed_no_finding"}


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def execution_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    parsed = number(value)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def reported_executions(path: Path) -> int | None:
    """Read integer native monitor counters, never derive them from a rate."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"\bexecutions[:= ]+(\d+)(?=[,\s]|$)", text, flags=re.IGNORECASE)
    return max(map(int, matches)) if matches else None


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # Writers may still be completing the last line.
        if isinstance(event, dict):
            result.append(event)
    return result


def native_inputs(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*")) if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
            and not p.name.endswith((".metadata", ".lock")) and not p.name.startswith("README")]


def metadata_for(seed: Path) -> dict[str, Any]:
    path = seed.with_name(f".{seed.name}.metadata")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


class FindingObserver:
    """Timestamp first visibility and retain later metadata without retiming.

    Append-only measurement evidence survives a later runner failure. It is
    written only on discovery/metadata changes, not on every polling tick.
    """
    def __init__(self, t0: int, journal: Path):
        self.t0, self.journal = t0, journal
        self.by_id: dict[str, dict[str, Any]] = {}

    def observe(self, candidates: list[dict[str, Any]], *, after_budget: bool = False) -> None:
        for candidate in candidates:
            key = str(candidate["candidate_id"])
            previous = self.by_id.get(key)
            if previous is None:
                current = dict(candidate)
                current["observed_monotonic_ns"] = time.monotonic_ns()
                current["observed_elapsed_seconds"] = (current["observed_monotonic_ns"] - self.t0) / 1e9
                current["time_source"] = "runner_first_observed"
                current["observed_after_budget"] = after_budget
            else:
                current = dict(previous)
                for field, value in candidate.items():
                    if field == "execution_count_source" and candidate.get("execution_ordinal") is None and previous.get("execution_ordinal") is not None:
                        continue
                    if value is not None and not field.startswith("observed_") and field != "time_source":
                        current[field] = value
            if current != previous:
                self.by_id[key] = current
                self.journal.parent.mkdir(parents=True, exist_ok=True)
                with self.journal.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(current, sort_keys=True) + "\n")

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self.by_id.values())


def paper_metrics(run: dict[str, Any]) -> dict[str, Any]:
    crashes = [e for e in run.get("candidate_findings", []) if e.get("oracle") in CRASH_ORACLES]
    budget = number(run.get("online_budget_seconds"))
    timely = [e for e in crashes if not e.get("observed_after_budget", False)
              and number(e.get("observed_elapsed_seconds")) is not None and budget is not None
              and float(e["observed_elapsed_seconds"]) <= budget]
    late = [e for e in crashes if e.get("observed_after_budget", False)
            or (number(e.get("observed_elapsed_seconds")) is not None and budget is not None
                and float(e["observed_elapsed_seconds"]) > budget)]
    # A shared native execution counter orders discoveries even when several
    # artifacts first become visible in the same observer poll.
    all_counts = bool(timely) and all(execution_count(e.get("execution_ordinal")) is not None for e in timely)
    def order(event: dict[str, Any]) -> int | float:
        value = event.get("execution_ordinal") if all_counts else event.get("observed_elapsed_seconds")
        if all_counts:
            return execution_count(value)
        return number(value) if number(value) is not None else float("inf")
    first = min(timely, key=order) if timely else None
    count = execution_count(first.get("execution_ordinal")) if first else None
    elapsed = number(run.get("online_elapsed_seconds"))
    total = execution_count(run.get("total_executions"))
    confirmed = [e for e in timely if (e.get("common_replay") or {}).get("confirmed")]
    unknown_replay = [e for e in timely if (e.get("common_replay") or {}).get("status")
                      not in {"confirmed", "not_reproduced"}]
    return {
        "contract": CONTRACT,
        "valid_run": run.get("run_status") in VALID_STATUSES,
        "execution_unit": "native_executor_testcase",
        "execution_model": run.get("execution_model", "unknown"),
        "initialization_in_online_budget": True,
        "crash_found": bool(timely),
        "crash_artifact_count": len(timely),
        "late_crash_artifact_count": len(late),
        "unmeasured_crash_artifact_count": len(crashes) - len(timely) - len(late),
        "first_crash_candidate_id": first.get("candidate_id") if first else None,
        "first_crash_time_seconds": number(first.get("observed_elapsed_seconds")) if first else None,
        "first_crash_time_source": "runner_first_observed" if first else None,
        "first_crash_executions": count,
        "first_crash_execution_source": first.get("execution_count_source", "unavailable") if first else None,
        "first_crash_order_source": "native_execution_counter" if all_counts else "runner_first_observed",
        "first_crash_count_status": "recorded" if count is not None else "unavailable" if first else "no_crash",
        "first_crash_input": (first.get("reproducer_artifact_path") or first.get("artifact_path")) if first else None,
        "replay_confirmed_crash_found": True if confirmed else None if unknown_replay else False,
        "unverified_crash_count": len(unknown_replay),
        "total_executions_reported": total,
        "total_executions_status": "last_native_report_not_exact_stop_count" if total is not None else "unavailable",
        "total_executions_window": "native_stop_snapshot_may_include_shutdown_tail",
        "execs_per_sec": total / elapsed if total is not None and elapsed else None,
        "throughput_basis": "last_native_report_divided_by_online_wall_time",
    }
