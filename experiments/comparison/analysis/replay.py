#!/usr/bin/env python3
"""Best-effort common oracle replay for comparison findings."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from experiments.comparison.lib.process import container_run_command
from experiments.comparison.lib.schema import validate_run_result


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
    for run in read_jsonl(experiment_dir / "raw-runs.jsonl"):
        run_dir = experiment_dir / "runs" / run["tool"] / run["suite"] / run["target_id"] / f"trial_{int(run['trial_id']):02d}"
        target_bin = run_dir / "tool-artifacts" / "fuzz_target"
        for event in run.get("candidate_findings", []):
            seed = resolve_event_seed(experiment_dir, event)
            if not target_bin.is_file() or not seed.is_file():
                status = "conversion_unsupported"
                confirmed = False
                stderr = ""
                returncode = None
            else:
                outcome = replay_seed(
                    experiment_dir,
                    target_bin,
                    seed,
                    timeout_seconds,
                    container_runtime=container_runtime,
                    semantist_image=semantist_image,
                    container_platform=container_platform,
                )
                status = outcome["status"]
                stderr = str(outcome.get("stderr") or "")
                returncode = outcome.get("returncode")
                confirmed = bool(outcome.get("confirmed"))
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
                    "returncode": returncode,
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
        command = container_run_command(
            container_runtime,
            semantist_image,
            mounts=[(experiment_dir, str(experiment_dir), True)],
            env={"ASAN_OPTIONS": "detect_leaks=0"},
            platform=container_platform,
            interactive_stdin=True,
            command=[str(target_bin)],
        )
    else:
        command = [str(target_bin)]
    try:
        completed = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stderr = (error.stderr or b"").decode("utf-8", errors="replace")
        return {
            "status": "unstable",
            "confirmed": False,
            "returncode": None,
            "stderr": stderr,
        }
    stderr = completed.stderr.decode("utf-8", errors="replace")
    confirmed = completed.returncode != 0 or "Sanitizer" in stderr or "runtime error:" in stderr
    return {
        "status": "confirmed" if confirmed else "not_reproduced",
        "confirmed": confirmed,
        "returncode": completed.returncode,
        "stderr": stderr,
    }


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
        by_candidate[str(record.get("candidate_id"))] = record
    for result_path in sorted((experiment_dir / "runs").rglob("run-result.json")):
        run = json.loads(result_path.read_text(encoding="utf-8"))
        run_records = by_run.get(str(run.get("run_key")), [])
        if not run_records:
            validate_run_result(run)
            continue
        confirmed_records = [record for record in run_records if record.get("confirmed")]
        for event in run.get("candidate_findings", []):
            record = by_candidate.get(str(event.get("candidate_id")))
            if record is not None:
                event["common_replay"] = {
                    "status": record["status"],
                    "confirmed": record["confirmed"],
                    "returncode": record["returncode"],
                }
        if confirmed_records:
            run["common_replay"] = {
                "status": "confirmed",
                "confirmed": True,
                "oracle": confirmed_records[0].get("oracle"),
            }
            run["unique_confirmed_fault_ids"] = sorted(
                set(run.get("unique_confirmed_fault_ids") or [])
                | {str(record.get("candidate_id")) for record in confirmed_records}
            )
        else:
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
            }
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
