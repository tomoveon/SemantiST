#!/usr/bin/env python3
"""Build-check imported external benchmarks through the SemantiST pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "external" / "manifest.json"
DEFAULT_ARTIFACT_DIR = ROOT / "artifacts" / "benchmarks" / "external_compile"


def load_targets(manifest: Path, suites: set[str], ids: set[str], limit: int | None) -> list[dict]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    targets = data["targets"]
    if suites:
        targets = [target for target in targets if target["suite"] in suites]
    if ids:
        targets = [target for target in targets if target["id"] in ids or target["function"] in ids]
    if limit is not None:
        targets = targets[:limit]
    return targets


def command_for(target: dict, run_dir: Path) -> list[str]:
    command = [
        "python3",
        "-m",
        "fuzzer.pipeline.core",
        "--st-file",
        str(ROOT / target["st_file"]),
        "--function",
        target["function"],
        "--run-dir",
        str(run_dir),
        "--build-only",
        "--skip-report",
    ]
    compat = target.get("compatibility_manifest")
    if compat:
        command.extend(["--compatibility-manifest", str(ROOT / compat)])
    return command


def run_target(target: dict, artifact_dir: Path, timeout: int) -> dict:
    run_dir = artifact_dir / "runs" / target["suite"] / target["id"]
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("CARGO_TARGET_DIR", str(ROOT / "artifacts" / "cargo-target"))
    start = time.time()
    proc = subprocess.run(
        command_for(target, run_dir),
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    elapsed = time.time() - start
    log_path = run_dir / "build.log"
    log_path.write_text(proc.stdout, encoding="utf-8")
    return {
        "suite": target["suite"],
        "id": target["id"],
        "function": target["function"],
        "kind": target["kind"],
        "status": "pass" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "run_dir": str(run_dir.relative_to(ROOT)),
        "log": str(log_path.relative_to(ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--suite", action="append", default=[])
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args()

    manifest = args.manifest.resolve()
    artifact_dir = args.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    targets = load_targets(manifest, set(args.suite), set(args.id), args.limit)
    if not targets:
        raise SystemExit("[external_compile] no targets selected")
    results_path = artifact_dir / "compile-results.jsonl"
    if results_path.exists():
        results_path.unlink()

    failures = 0
    for index, target in enumerate(targets, start=1):
        print(f"[external_compile] {index}/{len(targets)} {target['suite']}::{target['id']} ({target['function']})", flush=True)
        try:
            result = run_target(target, artifact_dir, args.timeout)
        except subprocess.TimeoutExpired as error:
            failures += 1
            run_dir = artifact_dir / "runs" / target["suite"] / target["id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = run_dir / "build.log"
            stdout = error.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            log_path.write_text(stdout, encoding="utf-8")
            result = {
                "suite": target["suite"],
                "id": target["id"],
                "function": target["function"],
                "kind": target["kind"],
                "status": "timeout",
                "returncode": None,
                "elapsed_seconds": args.timeout,
                "run_dir": str(run_dir.relative_to(ROOT)),
                "log": str(log_path.relative_to(ROOT)),
            }
        else:
            failures += int(result["status"] != "pass")
        with results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, sort_keys=True) + "\n")
        print(f"[external_compile] {result['status']} in {result['elapsed_seconds']}s", flush=True)
        if result["status"] != "pass" and not args.keep_going:
            break

    print(f"[external_compile] results: {results_path}", flush=True)
    print(f"[external_compile] pass={len(targets) - failures} fail_or_timeout={failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
