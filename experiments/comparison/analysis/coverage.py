#!/usr/bin/env python3
"""Best-effort common coverage replay using afl-showmap when available."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from experiments.comparison.lib.process import container_run_command
from experiments.comparison.lib.schema import validate_run_result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def coverage_experiment(
    experiment_dir: Path,
    timeout_seconds: float,
    *,
    container_runtime: str | None = None,
    semantist_image: str = "semantist:0.1.0",
    container_platform: str | None = None,
) -> list[dict[str, Any]]:
    experiment_dir = experiment_dir.resolve()
    afl_showmap = shutil.which("afl-showmap")
    use_container = container_runtime is not None
    records = []
    tmp_dir = experiment_dir / "summaries" / "coverage-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for run in read_jsonl(experiment_dir / "raw-runs.jsonl"):
        run_dir = experiment_dir / "runs" / run["tool"] / run["suite"] / run["target_id"] / f"trial_{int(run['trial_id']):02d}"
        target_bin = run_dir / "tool-artifacts" / "fuzz_target"
        corpus = corpus_paths(run_dir, run["tool"])
        if (afl_showmap is None and not use_container) or not target_bin.is_file() or not corpus:
            records.append(_coverage_record(run, "not-run", None, None, 0))
            continue
        slots: set[str] = set()
        failures = 0
        for testcase in corpus:
            with tempfile.NamedTemporaryFile(dir=tmp_dir) as trace_file:
                trace_path = Path(trace_file.name)
                completed = run_showmap(
                    experiment_dir,
                    target_bin,
                    testcase,
                    trace_path,
                    timeout_seconds,
                    afl_showmap=afl_showmap,
                    container_runtime=container_runtime,
                    semantist_image=semantist_image,
                    container_platform=container_platform,
                )
                if completed is None:
                    failures += 1
                    continue
                if completed.returncode not in (0, 1, 2):
                    failures += 1
                    continue
                for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if ":" in line:
                        slots.add(line.split(":", 1)[0])
        digest = hashlib.sha256("\n".join(sorted(slots)).encode("utf-8")).hexdigest()
        records.append(_coverage_record(run, "completed", len(slots), digest, failures))
    write_json(experiment_dir / "summaries" / "coverage_common.json", records)
    return records


def run_showmap(
    experiment_dir: Path,
    target_bin: Path,
    testcase: Path,
    trace_path: Path,
    timeout_seconds: float,
    *,
    afl_showmap: str | None,
    container_runtime: str | None = None,
    semantist_image: str = "semantist:0.1.0",
    container_platform: str | None = None,
) -> subprocess.CompletedProcess[bytes] | None:
    data = testcase.read_bytes()
    if container_runtime:
        command = container_run_command(
            container_runtime,
            semantist_image,
            mounts=[(experiment_dir, str(experiment_dir), False)],
            env={"ASAN_OPTIONS": "detect_leaks=0"},
            platform=container_platform,
            interactive_stdin=True,
            command=[
                "afl-showmap",
                "-q",
                "-o",
                str(trace_path),
                "--",
                str(target_bin),
            ],
        )
    elif afl_showmap is not None:
        command = [
            afl_showmap,
            "-q",
            "-o",
            str(trace_path),
            "--",
            str(target_bin),
        ]
    else:
        return None
    try:
        return subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None


def corpus_paths(run_dir: Path, tool: str) -> list[Path]:
    candidates = {
        "semantist_full": run_dir / "unified-corpus" / "runtime",
        "aflplusplus_full": run_dir / "tool-artifacts" / "afl" / "default" / "queue",
        "icsquartz_full": run_dir / "tool-artifacts" / "corpus",
        "structuredfuzzer_full": run_dir / "tool-artifacts" / "fuzzer" / "queue",
    }
    root = candidates.get(tool, run_dir / "corpus")
    if not root.is_dir():
        return []
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


def _coverage_record(
    run: dict[str, Any],
    status: str,
    covered_slots: int | None,
    digest: str | None,
    failures: int,
) -> dict[str, Any]:
    return {
        "run_key": run["run_key"],
        "tool": run["tool"],
        "suite": run["suite"],
        "target_id": run["target_id"],
        "trial_id": run["trial_id"],
        "status": status,
        "covered_slots": covered_slots,
        "covered_slots_sha256": digest,
        "conversion_failures": failures,
    }


def merge_coverage_results(experiment_dir: Path, records: list[dict[str, Any]]) -> None:
    by_run = {str(record["run_key"]): record for record in records}
    for result_path in sorted((experiment_dir / "runs").rglob("run-result.json")):
        run = json.loads(result_path.read_text(encoding="utf-8"))
        record = by_run.get(str(run.get("run_key")))
        if record is not None:
            run["common_coverage"] = {
                "status": record["status"],
                "covered_slots": record["covered_slots"],
                "covered_slots_sha256": record["covered_slots_sha256"],
                "conversion_failures": record["conversion_failures"],
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
    records = coverage_experiment(
        args.experiment_dir,
        args.timeout,
        container_runtime=args.container_runtime,
        semantist_image=args.semantist_image,
        container_platform=args.container_platform,
    )
    merge_coverage_results(args.experiment_dir.resolve(), records)
    print(f"coverage records: {len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
