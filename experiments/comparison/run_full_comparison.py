#!/usr/bin/env python3
"""Run the full SemantiST comparison matrix over the external benchmark manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.adapters import ADAPTERS, DEFAULT_TOOL_ORDER  # noqa: E402
from experiments.comparison.adapters.base import AdapterError, ExperimentConfig, PreprocessingResult  # noqa: E402
from experiments.comparison.analysis.aggregate import aggregate_experiment  # noqa: E402
from experiments.comparison.analysis.coverage import coverage_experiment, merge_coverage_results  # noqa: E402
from experiments.comparison.analysis.replay import merge_replay_results, replay_experiment  # noqa: E402
from experiments.comparison.analysis.validate_complete import validate as validate_complete  # noqa: E402
from experiments.comparison.lib.hashing import canonical_json_sha256, rng_seed, sha256_bytes, sha256_file, sha256_text  # noqa: E402
from experiments.comparison.lib.manifest import experiment_manifest_entry, load_manifest, select_targets  # noqa: E402
from experiments.comparison.lib.process import (  # noqa: E402
    command_exists,
    container_image_id,
    container_runtime_version,
    parse_cpus,
    resolve_container_runtime,
    unavailable_cpus,
)
from experiments.comparison.lib.schema import run_result_template, validate_run_result, write_json  # noqa: E402

DEFAULT_EXPERIMENT_ID = "comparison_4tools_90targets_300s_5trials"
DEFAULT_TOOLS = ",".join(DEFAULT_TOOL_ORDER)
DEFAULT_SEMANTIST_IMAGE = os.environ.get("SEMANTIST_IMAGE", "semantist:0.1.0")
EXPECTED_SOURCE_REVISIONS = {
    "SemantiST": "5ae1b74d064d303ecb7fc5aa70bf85f944b36907",
    "ICSQuartz": "8021bd44f47147776c6394e008bbea23ac993076",
}
EXPECTED_INPUT_HASHES = {
    "SemantiST/benchmarks/external/manifest.json": "d9d12e1b2d2d406e5304745679bae0bf7c08b5a6afaba470607f2ece25976079",
    "ICSQuartz/run_experiment.py": "e8ba6f5683207b4ee6a73e1739fd6adaaeb0f3875bda8c2fc6c64a554e17ae72",
    "ICSQuartz/src/experiments.py": "d78e72690e8dc06c24714a660b264f34be927a58d011b45f3c70e7874cbc5fb4",
    "ICSQuartz/README.md": "60086385313ecf16e2def1590c61a845c4624f4c3f3a5e689554e315f4d4d063",
}


def parse_args(argv: list[str] | None = None, *, description: str | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmarks" / "external" / "manifest.json")
    parser.add_argument("--artifact-root", type=Path, default=ROOT / "artifacts" / "experiments")
    parser.add_argument("--workspace-root", type=Path, default=ROOT.parent)
    parser.add_argument("--icsquartz-root", type=Path, default=None)
    parser.add_argument("--tools", default=DEFAULT_TOOLS, help=f"comma-separated tools; default: {DEFAULT_TOOLS}")
    parser.add_argument("--target-id", action="append", default=[], help="target id to run; can be repeated")
    parser.add_argument("--limit-targets", type=int, help="use the first N manifest targets for smoke runs")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--fuzz-time", type=int, default=300)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--semantic-task-budget", type=int, default=1000)
    parser.add_argument("--semantist-fb-max-cycles", type=int, default=10000)
    parser.add_argument("--semantist-fb-stale-threshold", type=int, default=0)
    parser.add_argument("--no-semantist-fb-state-trace", action="store_true")
    parser.add_argument("--observer-poll-interval-ms", type=int, default=10)
    parser.add_argument("--cpus", default="1-8")
    parser.add_argument("--cpus-isolated", action="store_true")
    parser.add_argument(
        "--semantist-image",
        default=DEFAULT_SEMANTIST_IMAGE,
        help="SemantiST toolchain/container image used for SemantiST and AFL++ target builds",
    )
    parser.add_argument(
        "--container-runtime",
        choices=("auto", "docker", "podman"),
        default=os.environ.get("CONTAINER_RUNTIME", "auto"),
        help="container runtime; auto tries Docker then Podman (default: %(default)s)",
    )
    parser.add_argument(
        "--container-platform",
        "--docker-platform",
        dest="container_platform",
        help="container image platform; --docker-platform remains as a compatibility alias",
    )
    parser.add_argument("--reuse-preprocessing", action="store_true")
    parser.add_argument("--resume", action="store_true", help="reuse existing run-result.json files")
    parser.add_argument("--overwrite", action="store_true", help="remove an existing experiment directory first")
    parser.add_argument("--preflight-only", action="store_true", help="write manifest/support/schedule and stop before preprocessing")
    parser.add_argument("--with-replay", action="store_true", help="run common replay and merge it into run records")
    parser.add_argument("--with-coverage", action="store_true", help="run common coverage replay and merge it into run records")
    parser.add_argument("--analysis-timeout", type=float, default=2.0)
    parser.add_argument("--validate-complete", action="store_true")
    parser.add_argument("--skip-aggregate", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, description: str | None = None) -> int:
    args = parse_args(argv, description=description)
    tools = normalize_tools(args.tools)
    cpus = parse_cpus(args.cpus)
    unavailable = unavailable_cpus(cpus)
    if unavailable:
        raise SystemExit(
            "requested CPUs are outside the runner affinity mask: "
            + ", ".join(str(cpu) for cpu in unavailable)
        )
    if args.trials < 1:
        raise SystemExit("--trials must be positive")
    if args.fuzz_time < 1:
        raise SystemExit("--fuzz-time must be positive")
    if args.timeout_ms < 1:
        raise SystemExit("--timeout-ms must be positive")
    if args.observer_poll_interval_ms < 1 or args.observer_poll_interval_ms > 10:
        raise SystemExit("--observer-poll-interval-ms must be in [1, 10]")
    try:
        container_runtime, container_runtime_status = resolve_container_runtime(
            args.container_runtime
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    taskset_required = container_runtime == "podman"
    if taskset_required and not command_exists("taskset"):
        raise SystemExit("taskset was not found; selected experiment rows require CPU affinity")
    icsquartz_root = find_icsquartz_root(args)
    config = ExperimentConfig(
        experiment_id=args.experiment_id,
        semantist_root=ROOT,
        workspace_root=args.workspace_root.resolve(),
        artifact_root=args.artifact_root.resolve(),
        icsquartz_root=icsquartz_root,
        online_budget_seconds=args.fuzz_time,
        per_execution_timeout_ms=args.timeout_ms,
        semantic_task_budget=args.semantic_task_budget,
        semantist_fb_max_cycles=args.semantist_fb_max_cycles,
        semantist_fb_stale_threshold=args.semantist_fb_stale_threshold,
        semantist_fb_state_trace=not args.no_semantist_fb_state_trace,
        observer_poll_interval_ms=args.observer_poll_interval_ms,
        cpuset=args.cpus if args.cpus_isolated else None,
        container_runtime=container_runtime,
        container_runtime_status=container_runtime_status,
        container_platform=args.container_platform,
        semantist_image=args.semantist_image,
        reuse_preprocessing=args.reuse_preprocessing,
    )
    experiment_dir = config.artifact_root / config.experiment_id
    if experiment_dir.exists() and args.overwrite:
        shutil.rmtree(experiment_dir)
    if experiment_dir.exists() and not args.resume and not args.preflight_only:
        raise SystemExit(f"experiment directory exists; use --resume or --overwrite: {experiment_dir}")
    experiment_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(args.manifest, ROOT)
    targets = select_targets(manifest.targets, args.target_id, args.limit_targets)
    adapters = {tool: ADAPTERS[tool](config) for tool in tools}

    write_environment(
        experiment_dir,
        args,
        tools,
        targets,
        icsquartz_root,
        container_runtime,
        container_runtime_status,
    )
    write_json(experiment_dir / "manifest.snapshot.json", manifest.raw)
    write_json(
        experiment_dir / "experiment-manifest.json",
        {
            "schema_version": "semantist.full-comparison-manifest/1.0.0",
            "experiment_id": config.experiment_id,
            "benchmark_manifest": str(manifest.path),
            "benchmark_manifest_sha256": manifest.sha256,
            "tools": tools,
            "target_count": len(targets),
            "trial_count": args.trials,
            "targets": [experiment_manifest_entry(target, tools) for target in targets],
        },
    )

    support = write_support_matrix(experiment_dir, tools, targets, adapters)
    schedule = write_schedule(experiment_dir, config.experiment_id, tools, targets, args.trials)
    print(
        f"[comparison] prepared schedule: {len(tools)} tools x {len(targets)} targets x {args.trials} trials = {len(schedule)} rows"
    )
    if args.preflight_only:
        print(f"[comparison] preflight artifacts written to {experiment_dir}")
        return 0

    preprocessing = prepare_supported_targets(tools, targets, adapters, support)
    update_support_matrix_preprocessing(experiment_dir, preprocessing)
    run_online_matrix(experiment_dir, schedule, targets, adapters, support, preprocessing, cpus, args.resume)
    write_raw_runs(experiment_dir, schedule)
    if args.with_replay:
        replay_records = replay_experiment(
            experiment_dir,
            args.analysis_timeout,
            container_runtime=container_runtime,
            semantist_image=args.semantist_image,
            container_platform=args.container_platform,
        )
        merge_replay_results(experiment_dir, replay_records)
        write_raw_runs(experiment_dir, schedule)
    if args.with_coverage:
        coverage_records = coverage_experiment(
            experiment_dir,
            args.analysis_timeout,
            container_runtime=container_runtime,
            semantist_image=args.semantist_image,
            container_platform=args.container_platform,
        )
        merge_coverage_results(experiment_dir, coverage_records)
        write_raw_runs(experiment_dir, schedule)
    if not args.skip_aggregate:
        aggregate_experiment(experiment_dir)
    if args.validate_complete:
        errors = validate_complete(experiment_dir)
        if errors:
            for error in errors:
                print(f"[comparison] validation error: {error}", file=sys.stderr)
            return 1
    print(f"[comparison] experiment artifacts: {experiment_dir}")
    return 0


def normalize_tools(text: str) -> list[str]:
    aliases = {
        "semantist": "semantist_full",
        "aflplusplus": "aflplusplus_full",
        "afl++": "aflplusplus_full",
        "icsfuzz": "icsfuzz_full",
        "icsquartz": "icsquartz_full",
        "structuredfuzzer": "structuredfuzzer_full",
    }
    result = []
    for item in text.split(","):
        raw = item.strip()
        if not raw:
            continue
        tool = aliases.get(raw, raw)
        if tool not in ADAPTERS:
            raise SystemExit(f"unknown tool {raw!r}; choose from {', '.join(ADAPTERS)}")
        result.append(tool)
    normalized = list(dict.fromkeys(result))
    if not normalized:
        raise SystemExit("--tools must select at least one tool")
    return normalized


def find_icsquartz_root(args: argparse.Namespace) -> Path | None:
    candidates = [
        args.icsquartz_root,
        Path(os.environ["ICSQUARTZ_ROOT"]) if os.environ.get("ICSQUARTZ_ROOT") else None,
        args.workspace_root / "ICSQuartz",
        Path("/myfuzzer/ICSQuartz"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate.resolve()
    return None


def write_environment(
    experiment_dir: Path,
    args: argparse.Namespace,
    tools: list[str],
    targets: list[Any],
    icsquartz_root: Path | None,
    container_runtime: str | None,
    container_runtime_status: str,
) -> None:
    paths = {
        "SemantiST": ROOT,
    }
    if "icsquartz_full" in tools or "icsfuzz_full" in tools:
        paths["ICSQuartz"] = icsquartz_root
    if "icsfuzz_full" in tools:
        paths["ICSFuzz"] = args.workspace_root / "ICSFuzz"
    revisions = {name: git_state(path) for name, path in paths.items() if path is not None}
    baseline_images = _baseline_images_for_tools(tools)
    actual_hashes = {}
    for rel, expected in EXPECTED_INPUT_HASHES.items():
        if rel.startswith("SemantiST/"):
            path = ROOT / rel.removeprefix("SemantiST/")
        elif rel.startswith("ICSQuartz/") and icsquartz_root is not None:
            path = icsquartz_root / rel.removeprefix("ICSQuartz/")
        else:
            path = None
        actual_hashes[rel] = {
            "expected_sha256": expected,
            "actual_sha256": sha256_file(path) if path is not None and path.is_file() else None,
            "path": str(path) if path is not None else None,
        }
    environment = {
        "schema_version": "semantist.full-comparison-environment/1.0.0",
        "experiment_id": args.experiment_id,
        "semantist_root": str(ROOT),
        "workspace_root": str(args.workspace_root.resolve()),
        "artifact_root": str(args.artifact_root.resolve()),
        "icsquartz_root": str(icsquartz_root) if icsquartz_root is not None else None,
        "tools": tools,
        "target_count": len(targets),
        "trial_count": args.trials,
        "online_budget_seconds": args.fuzz_time,
        "per_execution_timeout_ms": args.timeout_ms,
        "cpus": args.cpus,
        "cpus_isolated": args.cpus_isolated,
        "expected_source_revisions": EXPECTED_SOURCE_REVISIONS,
        "actual_source_revisions": revisions,
        "input_hashes": actual_hashes,
        "toolchain": {
            "python": sys.version.split()[0],
            "cargo": shutil.which("cargo"),
            "afl_fuzz": shutil.which("afl-fuzz"),
            "afl_showmap": shutil.which("afl-showmap"),
            "llvm_21_clang": str(Path("/usr/lib/llvm-21/bin/clang"))
            if Path("/usr/lib/llvm-21/bin/clang").exists()
            else None,
            "semantist_afl_runtime": str(Path("/usr/local/lib/afl/afl-compiler-rt.o"))
            if Path("/usr/local/lib/afl/afl-compiler-rt.o").exists()
            else None,
            "container_runtime_requested": args.container_runtime,
            "container_runtime": container_runtime,
            "container_runtime_accessible": container_runtime is not None,
            "container_runtime_status": container_runtime_status,
            "container_runtime_version": container_runtime_version(container_runtime),
            "container_platform": args.container_platform,
            "semantist_image": args.semantist_image,
            "semantist_image_id": container_image_id(container_runtime, args.semantist_image),
            "baseline_images": baseline_images,
            "baseline_image_ids": {
                key: container_image_id(container_runtime, image)
                for key, image in baseline_images.items()
            },
            "container_cpu_binding": (
                "taskset-inherited"
                if container_runtime == "podman"
                else "runtime-cpuset"
                if container_runtime == "docker"
                else None
            ),
            "aslr": Path("/proc/sys/kernel/randomize_va_space").read_text(encoding="utf-8").strip()
            if Path("/proc/sys/kernel/randomize_va_space").is_file()
            else None,
            "runner_cpu_affinity": sorted(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else None,
        },
    }
    write_json(experiment_dir / "environment.json", environment)


def _baseline_images_for_tools(tools: list[str]) -> dict[str, str]:
    tool_to_lock_key = {
        "aflplusplus_full": "aflplusplus",
        "icsfuzz_full": "icsfuzz",
        "icsquartz_full": "icsquartz",
        "structuredfuzzer_full": "structuredfuzzer",
    }
    lock_file = ROOT / "experiments" / "baselines" / "baselines.lock.json"
    lock = json.loads(lock_file.read_text(encoding="utf-8"))
    images = {}
    for tool in tools:
        key = tool_to_lock_key.get(tool)
        if key is not None:
            images[key] = str(lock["images"][key]["tag"])
    return images


def git_state(path: Path) -> dict[str, Any]:
    if not (path / ".git").exists():
        return {"path": str(path), "commit": None, "dirty_files": [], "dirty_diff_sha256": None}
    commit = _git(path, "rev-parse", "HEAD").strip() or None
    status = _git(path, "status", "--short").splitlines()
    diff = _git_bytes(path, "diff")
    return {
        "path": str(path),
        "commit": commit,
        "dirty_files": status,
        "dirty_diff_sha256": sha256_bytes(diff) if diff else None,
    }


def write_support_matrix(experiment_dir: Path, tools: list[str], targets: list[Any], adapters: dict[str, Any]) -> dict[tuple[str, str], Any]:
    rows = []
    support = {}
    for tool in tools:
        adapter = adapters[tool]
        for target in targets:
            result = adapter.detect_support(target)
            support[(tool, target.target_id)] = result
            rows.append(
                {
                    "tool": tool,
                    "suite": target.suite,
                    "target_id": target.target_id,
                    "kind": target.kind,
                    "support_status": result.status,
                    "support_reason": result.reason,
                    "preprocessing_status": "pending" if result.status == "supported" else "not-run",
                    "preprocessing_time_seconds": "",
                    "preprocessing_failure_code": "",
                    "support_details_sha256": canonical_json_sha256(result.details),
                }
            )
    with (experiment_dir / "support-matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return support


def update_support_matrix_preprocessing(
    experiment_dir: Path,
    preprocessing: dict[tuple[str, str], PreprocessingResult],
) -> None:
    path = experiment_dir / "support-matrix.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    for row in rows:
        prep = preprocessing.get((row["tool"], row["target_id"]))
        if prep is None:
            continue
        row["preprocessing_status"] = "completed" if prep.status == "completed" else "failed"
        row["preprocessing_time_seconds"] = f"{prep.elapsed_seconds:.9f}"
        row["preprocessing_failure_code"] = (
            str((prep.failure_reason or {}).get("code") or "")
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_schedule(
    experiment_dir: Path,
    experiment_id: str,
    tools: list[str],
    targets: list[Any],
    trials: int,
) -> list[dict[str, Any]]:
    rows = []
    order = 0
    for tool in tools:
        for target in targets:
            for trial_id in range(1, trials + 1):
                run_key = f"{experiment_id}|{tool}|{target.suite}|{target.target_id}|trial_{trial_id:02d}"
                sort_key = sha256_text(
                    f"{experiment_id}|schedule|{tool}|{target.suite}|{target.target_id}|{trial_id}"
                )
                rows.append(
                    {
                        "schedule_index": order,
                        "sort_key": sort_key,
                        "run_key": run_key,
                        "tool": tool,
                        "suite": target.suite,
                        "target_id": target.target_id,
                        "trial_id": trial_id,
                        "rng_seed": rng_seed(run_key),
                    }
                )
                order += 1
    rows.sort(key=lambda row: row["sort_key"])
    with (experiment_dir / "schedule.jsonl").open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            row = dict(row)
            row["schedule_index"] = index
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    return rows


def prepare_supported_targets(
    tools: list[str],
    targets: list[Any],
    adapters: dict[str, Any],
    support: dict[tuple[str, str], Any],
) -> dict[tuple[str, str], PreprocessingResult]:
    preprocessing: dict[tuple[str, str], PreprocessingResult] = {}
    for tool in tools:
        adapter = adapters[tool]
        for target in targets:
            if support[(tool, target.target_id)].status != "supported":
                continue
            print(f"[comparison] preprocessing {tool} {target.target_id}", flush=True)
            preprocessing[(tool, target.target_id)] = adapter.prepare(target)
    return preprocessing


def run_online_matrix(
    experiment_dir: Path,
    schedule: list[dict[str, Any]],
    targets: list[Any],
    adapters: dict[str, Any],
    support: dict[tuple[str, str], Any],
    preprocessing: dict[tuple[str, str], PreprocessingResult],
    cpus: list[int],
    resume: bool,
) -> None:
    by_target = {target.target_id: target for target in targets}
    immediate = []
    online = []
    for row in schedule:
        target = by_target[row["target_id"]]
        adapter = adapters[row["tool"]]
        result_path = adapter.run_dir(target, row["trial_id"]) / "run-result.json"
        if resume and result_path.is_file():
            continue
        support_result = support[(row["tool"], target.target_id)]
        if support_result.status != "supported":
            immediate.append((row, "unsupported"))
            continue
        prep = preprocessing.get((row["tool"], target.target_id))
        if prep is None or prep.status != "completed":
            immediate.append((row, "preprocessing_failed"))
            continue
        online.append(row)

    for row, status in immediate:
        target = by_target[row["target_id"]]
        adapter = adapters[row["tool"]]
        support_result = support[(row["tool"], target.target_id)]
        prep = preprocessing.get((row["tool"], target.target_id))
        run_dir = adapter.run_dir(target, row["trial_id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        result = make_base_result(row, target, adapter, support_result.status)
        if status == "unsupported":
            result.update(
                {
                    "preprocessing_status": "not-run",
                    "preprocessing_time_seconds": 0.0,
                    "online_elapsed_seconds": 0.0,
                    "run_status": "unsupported",
                    "failure_reason": {
                        "code": support_result.details.get("code", "unsupported"),
                        "message": support_result.reason,
                        "log_tail_path": None,
                    },
                }
            )
        else:
            result.update(_preprocessing_fields(prep))
            result.update(
                {
                    "online_elapsed_seconds": 0.0,
                    "run_status": "preprocessing_failed",
                    "failure_reason": prep.failure_reason if prep else {
                        "code": "preprocessing_missing",
                        "message": "preprocessing result was not produced",
                        "log_tail_path": None,
                    },
                }
            )
        write_run_result(run_dir, result)

    print(f"[comparison] online rows: {len(online)}; immediate records: {len(immediate)}", flush=True)
    for offset in range(0, len(online), len(cpus)):
        batch = online[offset : offset + len(cpus)]
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = []
            for cpu, row in zip(cpus, batch):
                target = by_target[row["target_id"]]
                adapter = adapters[row["tool"]]
                prep = preprocessing[(row["tool"], target.target_id)]
                futures.append(executor.submit(execute_online_row, row, target, adapter, prep, cpu))
            for future in as_completed(futures):
                row, run_dir, result = future.result()
                write_run_result(run_dir, result)
                print(
                    f"[comparison] completed {row['tool']} {row['target_id']} trial {row['trial_id']}: {result['run_status']}",
                    flush=True,
                )


def execute_online_row(row: dict[str, Any], target: Any, adapter: Any, prep: PreprocessingResult, cpu: int) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    run_dir = adapter.run_dir(target, row["trial_id"])
    result = make_base_result(row, target, adapter, "supported")
    result.update(_preprocessing_fields(prep))
    try:
        run_dir = adapter.materialize_trial(target, prep, row["trial_id"])
        updates = adapter.run(target, run_dir, prep, row["rng_seed"], cpu)
        result.update(updates)
    except AdapterError as error:
        result.update(
            {
                "run_status": "runner_error",
                "online_elapsed_seconds": 0.0,
                "failure_reason": error.to_failure_reason(),
            }
        )
    except Exception as error:  # noqa: BLE001 - one failed row must not kill the matrix.
        result.update(
            {
                "run_status": "runner_error",
                "online_elapsed_seconds": 0.0,
                "failure_reason": {
                    "code": "adapter_unhandled_exception",
                    "message": repr(error),
                    "log_tail_path": None,
                },
            }
        )
    return row, run_dir, result


def make_base_result(row: dict[str, Any], target: Any, adapter: Any, support_status: str) -> dict[str, Any]:
    seeds = adapter.seed_fields(row["rng_seed"])
    result = run_result_template(
        experiment_id=row["run_key"].split("|", 1)[0],
        run_key=row["run_key"],
        tool=row["tool"],
        target=target,
        trial_id=row["trial_id"],
        rng_seed_requested=seeds["rng_seed_requested"],
        rng_seed_applied=seeds["rng_seed_applied"],
        rng_seed_status=seeds["rng_seed_status"],
        online_budget_seconds=adapter.config.online_budget_seconds,
        per_execution_timeout_ms=adapter.per_execution_timeout_ms,
        execution_timeout_model=adapter.per_execution_timeout_model,
        support_status=support_status,
        observer_poll_interval_ms=adapter.config.observer_poll_interval_ms,
    )
    result.update(
        {
            "initial_seed_provider": seeds["initial_seed_provider"],
            "initial_seed_format": seeds["initial_seed_format"],
            "initial_seed_policy": seeds["initial_seed_policy"],
        }
    )
    return result


def write_run_result(run_dir: Path, result: dict[str, Any]) -> None:
    result["artifact_paths"] = result.get("artifact_paths") or {}
    result["artifact_paths"]["run_result"] = "run-result.json"
    validate_run_result(result)
    write_json(run_dir / "run-result.json", result)


def write_raw_runs(experiment_dir: Path, schedule: list[dict[str, Any]]) -> None:
    records = []
    for row in schedule:
        path = (
            experiment_dir
            / "runs"
            / row["tool"]
            / row["suite"]
            / row["target_id"]
            / f"trial_{row['trial_id']:02d}"
            / "run-result.json"
        )
        if not path.is_file():
            raise RuntimeError(f"missing run result for {row['run_key']}: {path}")
        records.append(json.loads(path.read_text(encoding="utf-8")))
    with (experiment_dir / "raw-runs.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _preprocessing_fields(prep: PreprocessingResult | None) -> dict[str, Any]:
    if prep is None:
        return {}
    return {
        "preprocessing_status": "completed" if prep.status == "completed" else "failed",
        "preprocessing_start_monotonic_ns": prep.start_monotonic_ns,
        "preprocessing_end_monotonic_ns": prep.end_monotonic_ns,
        "preprocessing_time_seconds": prep.elapsed_seconds,
    }


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _git_bytes(path: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        return b""
    return result.stdout if result.returncode == 0 else b""


if __name__ == "__main__":
    raise SystemExit(main())
