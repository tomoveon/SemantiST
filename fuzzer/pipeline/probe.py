"""Native probe: execute one canonical testcase and decode its observations."""

from __future__ import annotations

import json
import re
import os
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SANITIZER_ENV = {
    "ASAN_OPTIONS": "abort_on_error=1:symbolize=0:detect_leaks=0:allocator_may_return_null=1",
    "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
    "LSAN_OPTIONS": "detect_leaks=0",
}


@dataclass
class Observation:
    returncode: int | None
    timed_out: bool
    stderr: str
    hits: dict[int, list[int]] = field(default_factory=dict)
    states: list[dict[str, Any]] = field(default_factory=list)
    wall_s: float = 0.0
    crashed: bool = False
    scan_budget: bool = False
    signal_name: str | None = None

    def target_hits(self, kind: str, runtime_index: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            runtime_index[runtime_id]
            for runtime_id in self.hits
            if runtime_id in runtime_index and runtime_index[runtime_id].get("kind") == kind
        ]


def load_runtime_index(path: Path) -> dict[int, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    index = {int(entry["runtime_id"]): entry for entry in data.get("entries", [])}
    if not any((entry.get("violation_predicate") or {}).get("kind") == "narrowing"
               and entry.get("modeling") == "conservative" for entry in index.values()):
        return index
    model_path = Path(path).with_name("stg-model.json")
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return index
    from fuzzer.pipeline.probe_audit import _platform_assumptions

    assumptions = set()
    for pou in model.get("pous", []):
        pou_name = (pou.get("pou") or {}).get("qualified_name")
        environment = pou.get("environment") or {}
        types = {str(item.get("name") or "").upper(): item
                 for item in environment.get("types", [])}
        for assumption in _platform_assumptions(pou, types):
            source = assumption.get("source") or {}
            assumptions.add((pou_name, str(source.get("file")), source.get("start_line"),
                             assumption.get("pointer_bits"), assumption.get("integer_bits")))
    for entry in index.values():
        predicate = entry.get("violation_predicate") or {}
        source = entry.get("source") or {}
        key = (entry.get("pou"), str(source.get("file")), source.get("start_line"),
               predicate.get("source_bit_width"), predicate.get("target_bit_width"))
        if predicate.get("kind") == "narrowing" and key in assumptions:
            entry["platform_pointer_narrowing"] = True
    return index


def _decode_states(path: Path) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    if not path.exists():
        return states
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        fields = record.get("fields")
        if isinstance(fields, dict):
            states.append(fields)
    return states


def probe(
    target_binary: Path,
    testcase: str | bytes,
    *,
    work_dir: Path,
    timeout_s: float = 3.0,
    runtime_ids: Path | None = None,
    extra_env: dict[str, str] | None = None,
    trace: bool = True,
) -> Observation:
    work_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    testcase_path = work_dir / f"{token}.txt"
    hits_path = work_dir / f"{token}.hits.jsonl"
    states_path = work_dir / f"{token}.state.jsonl"
    if isinstance(testcase, str):
        testcase = testcase.encode("utf-8")
    testcase_path.write_bytes(testcase)
    env = os.environ.copy()
    # Keep user supplied execution settings (for example FB cycle limits),
    # but replace per-probe output paths below so parallel probes do not share
    # stale telemetry files.
    for key in (
        "SEMANTIST_SEMANTIC_TRACE_RUNTIME",
        "SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE",
        "SEMANTIST_STATE_DUMP_FILE",
    ):
        env.pop(key, None)
    env.update(SANITIZER_ENV)
    if trace:
        env.update(
            {
                "SEMANTIST_SEMANTIC_TRACE_RUNTIME": "1",
                "SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE": str(hits_path),
                "SEMANTIST_STATE_DUMP_FILE": str(states_path),
            }
        )
    if extra_env:
        env.update(extra_env)
    started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.Popen(
            [str(target_binary), str(testcase_path)],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        # Keep launch failures in the same observation vocabulary as a
        # non-zero target exit.  Callers can then record FAILED/replay
        # evidence without losing the campaign to an uncaught traceback.
        wall = time.monotonic() - started
        for path in (testcase_path, hits_path, states_path):
            try:
                path.unlink()
            except OSError:
                pass
        return Observation(
            returncode=127,
            timed_out=False,
            stderr=f"native probe could not start: {error}",
            wall_s=wall,
        )
    try:
        _stdout, raw_stderr = process.communicate(timeout=timeout_s)
        returncode = process.returncode
        stderr = raw_stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = None
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        raw = error.stderr or b""
        tail = process.communicate()[1]
        stderr = (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw))
        if tail:
            stderr += "\n" + tail.decode("utf-8", errors="replace")
    wall = time.monotonic() - started
    hits: dict[int, list[int]] = {}
    if hits_path.exists():
        for line in hits_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            runtime_id = int(record.get("runtime_id", -1))
            if runtime_id >= 0:
                hits.setdefault(runtime_id, []).append(int(record.get("cycle", 0)))
    states = _decode_states(states_path)
    for path in (testcase_path, hits_path, states_path):
        try:
            path.unlink()
        except OSError:
            pass
    scan_budget = "semantist-scan-budget-exceeded" in stderr
    crashed = returncode is not None and (returncode < 0 or returncode >= 128)
    signal_name = None
    if returncode is not None and returncode < 0:
        try:
            signal_name = signal.Signals(-returncode).name
        except ValueError:
            signal_name = str(-returncode)
    return Observation(
        returncode=returncode,
        timed_out=timed_out,
        stderr=stderr,
        hits=hits,
        states=states,
        wall_s=wall,
        crashed=crashed,
        scan_budget=scan_budget,
        signal_name=signal_name,
    )


def classify(observation: Observation, runtime_index: dict[int, dict[str, Any]], *,
             expected_runtime_id: int | None = None) -> dict[str, Any] | None:
    """Return a justified candidate, optionally for one saved runtime goal."""
    if observation.timed_out:
        return None
    marker = re.search(r"(?m)^SEMANTIST_PROPERTY_VIOLATION:([A-Za-z][A-Za-z0-9_-]{0,63}):(\d+)\s*$",
                       observation.stderr)
    if marker and observation.crashed and expected_runtime_id is None:
        return {
            "oracle": "generated_property", "hazard": "functional_output",
            "runtime_id": None, "cycle": int(marker.group(2)),
            "source": None, "kind": "property_violation",
            "property_id": marker.group(1), "modeling": "generated_checker",
            "diagnostic": marker.group(0).strip(),
        }
    hits = ([(expected_runtime_id, observation.hits[expected_runtime_id])]
            if expected_runtime_id is not None and expected_runtime_id in observation.hits
            else [] if expected_runtime_id is not None else sorted(observation.hits.items()))
    for runtime_id, cycles in hits:
        entry = runtime_index.get(runtime_id)
        if not entry or entry.get("kind") != "hazard_violation":
            continue
        # Numeric boundary probes guide search, but the execution contract
        # treats integer wrap as exploration and declares no nonfinite oracle.
        if entry.get("hazard") == "arithmetic_boundary":
            continue
        predicate = entry.get("violation_predicate") or {}
        observed_conversion = (
            entry.get("hazard") == "dangerous_conversion"
            and predicate.get("kind") == "float_to_integer"
            and predicate.get("target_bit_width") in (8, 16, 32, 64)
            and predicate.get("signed") is not None
        ) or (
            entry.get("hazard") == "dangerous_conversion"
            and predicate.get("kind") == "narrowing"
            and entry.get("platform_pointer_narrowing") is True
        )
        if entry.get("modeling") == "exact" or observed_conversion:
            hazard = entry.get("hazard") or "semantic_violation"
            return {
                "oracle": oracle_for_hazard(hazard),
                "hazard": hazard,
                "runtime_id": runtime_id,
                "cycle": cycles[-1] if cycles else None,
                "source": entry.get("source"),
                "kind": "hazard_violation",
                "diagnostic": None,
                "modeling": entry.get("modeling"),
                "observed_predicate": predicate.get("kind") if observed_conversion else None,
            }
    if observation.scan_budget:
        return {
            "oracle": "scan_budget",
            "hazard": "scan_budget",
            "runtime_id": None,
            "cycle": None,
            "source": None,
            "kind": "scan_budget",
            "diagnostic": "semantist-scan-budget-exceeded",
        }
    if expected_runtime_id is not None:
        return None
    if observation.crashed:
        stderr = observation.stderr
        oracle = "crash"
        if "division by zero" in stderr or "FPE" in stderr or observation.signal_name == "SIGFPE":
            oracle = "division_by_zero"
        elif "out of bounds" in stderr or "buffer-overflow" in stderr or "bounds" in stderr:
            oracle = "array_access"
        elif "null" in stderr and "pointer" in stderr:
            oracle = "null_dereference"
        return {
            "oracle": oracle,
            "hazard": oracle,
            "runtime_id": None,
            "cycle": observation.states[-1].get("__cycle") if observation.states else None,
            "source": None,
            "kind": "crash",
            "diagnostic": stderr.splitlines()[0] if stderr else observation.signal_name,
        }
    return None


def is_finding_candidate(info: dict[str, Any] | None) -> bool:
    """Return whether classified probe data represents a real finding candidate."""
    return info is not None and info.get("kind") != "scan_budget"


def oracle_for_hazard(hazard: str) -> str:
    return {
        "array_access": "array_access",
        "pointer_access": "null_dereference",
        "division": "division_by_zero",
        "modulo": "division_by_zero",
        "dangerous_conversion": "conversion_out_of_range",
        "arithmetic_boundary": "crash",
        "user_property": "crash",
    }.get(hazard, "crash")
