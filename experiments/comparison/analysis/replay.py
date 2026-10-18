#!/usr/bin/env python3
"""Best-effort common oracle replay for comparison findings."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.comparison.lib.process import container_run_command
from experiments.comparison.lib.schema import validate_run_result
from experiments.comparison.lib.metrics import CRASH_ORACLES, paper_metrics


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def replay_experiment(
    experiment_dir: Path,
    timeout_seconds: float,
    *,
    container_runtime: str | None = None,
    semantist_image: str = "semantist:0.1.0",
    container_platform: str | None = None,
) -> list[dict[str, Any]]:
    experiment_dir = experiment_dir.resolve()
    records = []
    for result_path in sorted((experiment_dir / "runs").rglob("run-result.json")):
        run = json.loads(result_path.read_text(encoding="utf-8"))
        run_dir = experiment_dir / "runs" / run["tool"] / run["suite"] / run["target_id"] / f"trial_{int(run['trial_id']):02d}"
        target_bin = run_dir / "tool-artifacts" / "fuzz_target"
        for event in run.get("candidate_findings", []):
            if event.get("oracle") not in CRASH_ORACLES:
                continue
            seed = resolve_event_seed(experiment_dir, event)
            unsupported_history = run.get("execution_model") == "scan_cycle"
            if unsupported_history or not target_bin.is_file() or not seed.is_file():
                status = "conversion_unsupported"
                confirmed = False
                stderr = ""
                returncode = None
                reason = "state_history_unavailable" if unsupported_history else "missing_target_or_seed"
            else:
                outcome = replay_seed(
                    experiment_dir,
                    target_bin,
                    seed,
                    timeout_seconds,
                    container_runtime=container_runtime,
                    semantist_image=str(run.get("replay_image") or semantist_image),
                    container_platform=container_platform,
                )
                status = outcome["status"]
                stderr = str(outcome.get("stderr") or "")
                returncode = outcome.get("returncode")
                confirmed = bool(outcome.get("confirmed"))
                reason = outcome.get("reason")
            fault_class = None if unsupported_history or not target_bin.is_file() or not seed.is_file() else outcome.get("fault_class")
            records.append(
                {
                    "run_key": run["run_key"],
                    "candidate_id": event.get("candidate_id"),
                    "tool": run["tool"],
                    "target_id": run["target_id"],
                    "trial_id": run["trial_id"],
                    "status": status,
                    "confirmed": confirmed,
                    "oracle": event.get("oracle"),
                    "fault_class": fault_class,
                    "returncode": returncode,
                    "reason": reason,
                    "input_mode": "stdin",
                    "stderr_preview": stderr[:2000],
                }
            )
    write_json(experiment_dir / "summaries" / "replay_common.json", records)
    return records


def replay_seed(
    experiment_dir: Path,
    target_bin: Path,
    seed: Path,
    timeout_seconds: float,
    *,
    container_runtime: str | None = None,
    semantist_image: str = "semantist:0.1.0",
    container_platform: str | None = None,
) -> dict[str, Any]:
    data = seed.read_bytes()
    if container_runtime:
        try:
            relative_target = target_bin.resolve().relative_to(experiment_dir.resolve())
            seed.resolve().relative_to(experiment_dir.resolve())
        except ValueError:
            return {
                "status": "replay_error",
                "confirmed": False,
                "returncode": None,
                "stderr": "target and seed must be inside experiment_dir for container replay",
                "reason": "path_outside_experiment",
            }
        container_target = "/replay/" + relative_target.as_posix()
        inner_command = [container_target]
        command = container_run_command(
            container_runtime,
            semantist_image,
            mounts=[(experiment_dir, "/replay", True)],
            env=REPLAY_SANITIZER_ENV,
            platform=container_platform,
            interactive_stdin=True,
            command=inner_command,
        )
    else:
        command = [str(target_bin)]
        if target_bin.suffix.lower() == ".py":
            command.insert(0, sys.executable)
    try:
        completed = subprocess.run(
            command,
            env=None if container_runtime else {**os.environ, **REPLAY_SANITIZER_ENV},
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except OSError as error:
        return {"status": "replay_error", "confirmed": False, "returncode": None,
                "stderr": str(error), "reason": "exec_failed"}
    except subprocess.TimeoutExpired as error:
        stderr = (error.stderr or b"").decode("utf-8", errors="replace")
        return {
            "status": "unstable",
            "confirmed": False,
            "returncode": None,
            "stderr": stderr,
            "reason": "timeout",
        }
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    combined = f"{stdout}\n{stderr}"
    startup_reason = _startup_failure_reason(combined)
    if startup_reason:
        return {
            "status": "replay_error",
            "confirmed": False,
            "returncode": completed.returncode,
            "stderr": stderr,
            "reason": startup_reason,
        }
    sanitizer = bool(re.search(r"(?:ERROR|SUMMARY): (?:Address|UndefinedBehavior)Sanitizer:|runtime error:", combined))
    crash_signals = {4, 5, 6, 7, 8, 11, 31}  # ILL, TRAP, ABRT, BUS, FPE, SEGV, SYS on Linux.
    signal_exit = completed.returncode is not None and (
        -completed.returncode in crash_signals
        or bool(container_runtime) and completed.returncode - 128 in crash_signals
    )
    confirmed = sanitizer or signal_exit
    return {
        "status": "confirmed" if confirmed else "not_reproduced" if completed.returncode == 0 else "replay_error",
        "confirmed": confirmed,
        "returncode": completed.returncode,
        "stderr": stderr,
        "fault_class": classify_fault(combined) if confirmed else None,
        "reason": (
            "sanitizer_or_signal"
            if sanitizer or signal_exit
            else "clean_or_unclassified_nonzero"
        ),
    }


# Symbolized stacks let one rule classify every tool's replay the same way.
REPLAY_SANITIZER_ENV = {
    "ASAN_OPTIONS": "detect_leaks=0:symbolize=1",
    "UBSAN_OPTIONS": "print_stacktrace=1:symbolize=1",
}
_RUST_PANIC = re.compile(r"thread '[^']*' panicked at |panicked at [^\n]*\.rs:\d+")
_FRAME = re.compile(r"^\s*#\d+ 0x[0-9a-f]+ in (?P<function>\S+)(?: (?P<location>\S+))?", re.M)
_RUNTIME_FRAME = re.compile(r"^(?:__asan|__interceptor|___interceptor|__sanitizer|__ubsan|__lsan|__libc_|abort$|raise$)")


def classify_fault(output: str) -> str:
    """Attribute a confirmed crash to the toolchain, the harness, or the target.

    ``toolchain_panic`` is a Rust panic in the RuSTy standard library (e.g. its
    UTF-8 string assumptions), which a CODESYS runtime does not share.
    ``harness_fault`` faults first in harness code. Only ``target_fault``
    counts as a target defect.
    """
    if _RUST_PANIC.search(output):
        return "toolchain_panic"
    for frame in _FRAME.finditer(output):
        if _RUNTIME_FRAME.match(frame.group("function")):
            continue
        location = frame.group("location") or ""
        if re.search(r"(?:^|/)(?:semantist_)?harness\.c:", location):
            return "harness_fault"
        break
    return "target_fault"


def _startup_failure_reason(output: str) -> str | None:
    lowered = output.lower()
    patterns = (
        ("asan runtime does not come first", "sanitizer_initialization_failed"),
        ("shadow memory range interleaves", "sanitizer_initialization_failed"),
        ("sanitizer check failed", "sanitizer_initialization_failed"),
        ("malformed line detected", "invalid_input"),
        ("unknown type", "invalid_input"),
        ("invalid bool value", "invalid_input"),
        ("invalid sint value", "invalid_input"),
        ("invalid int value", "invalid_input"),
        ("invalid real value", "invalid_input"),
        ("usage:", "invalid_command"),
        ("error while loading shared libraries", "missing_runtime_or_input"),
        ("failed to load initial corpus", "initial_corpus_load_failed"),
        ("unable to request new process from fork server", "forkserver_start_failed"),
        ("wrong forkserver message", "forkserver_protocol_mismatch"),
        ("failed to start a forkserver", "forkserver_start_failed"),
        ("failed to open", "input_or_file_open_failed"),
        ("no such file or directory", "missing_runtime_or_input"),
    )
    for needle, reason in patterns:
        if needle in lowered:
            return reason
    return None


def resolve_event_seed(experiment_dir: Path, event: dict[str, Any]) -> Path:
    for key in ("reproducer_artifact_path", "artifact_path"):
        raw = event.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = experiment_dir / path
        if path.is_file():
            return path
    return experiment_dir / "__missing_candidate_seed__"


def merge_replay_results(experiment_dir: Path, records: list[dict[str, Any]]) -> None:
    by_run: dict[str, list[dict[str, Any]]] = {}
    by_candidate = {}
    for record in records:
        by_run.setdefault(str(record["run_key"]), []).append(record)
        by_candidate[(str(record["run_key"]), str(record.get("candidate_id")))] = record
    for result_path in sorted((experiment_dir / "runs").rglob("run-result.json")):
        run = json.loads(result_path.read_text(encoding="utf-8"))
        run_records = by_run.get(str(run.get("run_key")), [])
        if not run_records:
            validate_run_result(run)
            continue
        confirmed_records = [record for record in run_records if record.get("confirmed")]
        for event in run.get("candidate_findings", []):
            record = by_candidate.get((str(run["run_key"]), str(event.get("candidate_id"))))
            if record is not None:
                event["common_replay"] = {
                    "status": record["status"],
                    "confirmed": record["confirmed"],
                    "fault_class": record.get("fault_class"),
                    "returncode": record["returncode"],
                    "reason": record.get("reason"),
                    "input_mode": record.get("input_mode"),
                }
        if confirmed_records:
            confirmed_ids = {str(record.get("candidate_id")) for record in confirmed_records}
            confirmed_events = [
                event
                for event in run.get("candidate_findings", [])
                if str(event.get("candidate_id")) in confirmed_ids
            ]
            run["common_replay"] = {
                "status": "confirmed",
                "confirmed": True,
                "oracle": confirmed_records[0].get("oracle"),
                "reason": confirmed_records[0].get("reason"),
            }
            run["confirmed_candidate_ids"] = sorted(confirmed_ids)
        else:
            run["confirmed_candidate_ids"] = []
            statuses = {str(record.get("status")) for record in run_records}
            status = "conversion_unsupported" if statuses == {"conversion_unsupported"} else "not_reproduced"
            if "replay_error" in statuses:
                status = "replay_error"
            if "unstable" in statuses:
                status = "unstable"
            run["common_replay"] = {
                "status": status,
                "confirmed": False,
                "oracle": None,
                "reason": next(
                    (record.get("reason") for record in run_records if record.get("reason")),
                    None,
                ),
            }
        run["paper_metrics"] = paper_metrics(run)
        validate_run_result(run)
        write_json(result_path, run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--container-runtime", choices=("docker", "podman"))
    parser.add_argument("--semantist-image", default="semantist:0.1.0")
    parser.add_argument("--container-platform")
    args = parser.parse_args()
    records = replay_experiment(
        args.experiment_dir,
        args.timeout,
        container_runtime=args.container_runtime,
        semantist_image=args.semantist_image,
        container_platform=args.container_platform,
    )
    merge_replay_results(args.experiment_dir.resolve(), records)
    print(f"replay records: {len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
