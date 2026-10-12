#!/usr/bin/env python3
"""Run the full SemantiST comparison matrix over the external benchmark manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.adapters import ADAPTERS, DEFAULT_TOOL_ORDER  # noqa: E402
from experiments.comparison.ablations import ABLATION_PROFILES  # noqa: E402
from experiments.comparison.adapters.base import AdapterError, ExperimentConfig, PreprocessingResult  # noqa: E402
from experiments.comparison.analysis.aggregate import aggregate_experiment  # noqa: E402
from experiments.comparison.analysis.compare_tools import export_metrics  # noqa: E402
from experiments.comparison.analysis.coverage import coverage_experiment, merge_coverage_results  # noqa: E402
from experiments.comparison.analysis.replay import merge_replay_results, replay_experiment  # noqa: E402
from experiments.comparison.analysis.validate_complete import validate as validate_complete  # noqa: E402
from experiments.comparison.lib.hashing import canonical_json_sha256, rng_seed, sha256_bytes, sha256_file, sha256_text  # noqa: E402
from experiments.comparison.lib.manifest import experiment_manifest_entry, load_manifest, select_targets  # noqa: E402
from experiments.comparison.lib.process import (  # noqa: E402
    command_exists,
    container_image_id,
    container_run_command,
    container_runtime_version,
    parse_cpus,
    resolve_container_runtime,
    unavailable_cpus,
)
from experiments.comparison.lib.schema import run_result_template, validate_run_result, write_json  # noqa: E402
from experiments.comparison.lib.metrics import CONTRACT, paper_metrics  # noqa: E402

DEFAULT_EXPERIMENT_ID = "comparison_3tools_90targets_300s_5trials"
DEFAULT_TOOLS = ",".join(DEFAULT_TOOL_ORDER)
DEFAULT_SEED_NAMESPACE = "semantist-comparison-v1"
DEFAULT_SEMANTIST_IMAGE = os.environ.get("SEMANTIST_IMAGE", "semantist:0.1.0")
EXPERIMENT_PRESETS = {
    "main": {
        "experiment_id": DEFAULT_EXPERIMENT_ID,
        "target_id": (),
        "fuzz_time": 300,
        "trials": 5,
        "cpus": "1-8",
    },
    "smoke": {
        "experiment_id": "comparison_3tools_20s_smoke",
        "target_id": (
            "oscat_basic_charname", "icsfuzz_bf_mcpy_1", "scan_cycle_aircraft_oobw_4",
        ),
        "fuzz_time": 20,
        "trials": 1,
        "cpus": "1-2",
    },
}
EXPECTED_SOURCE_REVISIONS = {
    "ICSQuartz": "8021bd44f47147776c6394e008bbea23ac993076",
}
EXPECTED_INPUT_HASHES = {
    "SemantiST/benchmarks/external/manifest.json": "0ab4c3a89c1538fbbda8bb4443b8d72cb336df277850354a4becc8e25d78fce5",
    "ICSQuartz/run_experiment.py": "e8ba6f5683207b4ee6a73e1739fd6adaaeb0f3875bda8c2fc6c64a554e17ae72",
    "ICSQuartz/src/experiments.py": "d78e72690e8dc06c24714a660b264f34be927a58d011b45f3c70e7874cbc5fb4",
    "ICSQuartz/README.md": "60086385313ecf16e2def1590c61a845c4624f4c3f3a5e689554e315f4d4d063",
}


def parse_args(argv: list[str] | None = None, *, description: str | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description=description or __doc__, allow_abbrev=False,
        epilog=("Presets: main = all manifest targets, 300 seconds, 5 trials, CPUs 1-8; "
                "smoke = 3 representative targets, 20 seconds, 1 trial, CPUs 1-2. "
                "Explicit options override preset defaults. Use a new run ID for each run. "
                "Successful runs export metrics.csv; comparative analysis is a separate step."),
    )
    parser.add_argument("--help-all", action="help", help="show all options, including advanced settings")
    parser.add_argument("--experiment", choices=EXPERIMENT_PRESETS, default="main",
                        help="experiment preset (default: main)")
    parser.add_argument("--run-id", "--experiment-id", dest="experiment_id",
                        help="new output directory name (default: fixed preset ID)")
    parser.add_argument("--tools", default=DEFAULT_TOOLS, help=f"comma-separated tools; default: {DEFAULT_TOOLS}")
    parser.add_argument("--target-id", action="append", help="replace preset targets; can be repeated")
    parser.add_argument("--fuzz-time", type=int, help="online seconds per trial (default: preset)")
    parser.add_argument("--fuzz-trials", "--trials", dest="trials", type=int,
                        help="trials per tool and target (default: preset)")
    parser.add_argument("--cpus", help="CPU IDs for parallel trials, e.g. 1-8 or 1,3,5 (default: preset)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="write manifest/support/schedule and stop before preprocessing")
    advanced = parser.add_argument_group("Advanced options")

    def add_advanced(*flags: str, group: Any = advanced, **kwargs: Any) -> None:
        kwargs["help"] = kwargs.get("help", "") if "--help-all" in argv else argparse.SUPPRESS
        group.add_argument(*flags, **kwargs)

    add_advanced("--manifest", type=Path, default=ROOT / "benchmarks" / "external" / "manifest.json")
    add_advanced("--artifact-root", type=Path, default=ROOT / "artifacts" / "experiments")
    add_advanced("--workspace-root", type=Path, default=ROOT.parent)
    add_advanced("--icsquartz-root", type=Path, default=None)
    add_advanced("--limit-targets", type=int, help="use the first N selected targets")
    add_advanced(
        "--seed-namespace",
        default=DEFAULT_SEED_NAMESPACE,
        help="stable namespace shared by independent tool runs for paired target/trial seeds",
    )
    add_advanced("--timeout-ms", type=int, default=1000)
    add_advanced("--semantic-task-budget", type=int, default=1000)
    add_advanced(
        "--semantist-ablation",
        choices=ABLATION_PROFILES,
        default="full",
        help="SemantiST-only runtime mechanism ablation; use a separate one-tool run",
    )
    add_advanced("--semantist-fb-max-cycles", type=int, default=256)
    add_advanced("--semantist-fb-stale-threshold", type=int, default=0)
    trace_group = advanced.add_mutually_exclusive_group()
    add_advanced(
        "--semantist-fb-state-trace",
        group=trace_group,
        dest="semantist_fb_state_trace",
        action="store_true",
        help="enable SemantiST-only diagnostic state tracing (off by default for comparisons)",
    )
    add_advanced(
        "--no-semantist-fb-state-trace",
        group=trace_group,
        dest="semantist_fb_state_trace",
        action="store_false",
        help="disable diagnostic state tracing (default)",
    )
    parser.set_defaults(semantist_fb_state_trace=False)
    add_advanced(
        "--observer-poll-interval-ms", type=int, default=0,
        help="deprecated compatibility option; ignored because metrics are collected after each run",
    )
    add_advanced(
        "--cpus-isolated",
        action="store_true",
        help=(
            "record that the selected CPUs are administratively isolated; "
            "per-trial CPU binding is always enforced"
        ),
    )
    add_advanced(
        "--semantist-image",
        default=DEFAULT_SEMANTIST_IMAGE,
        help="SemantiST toolchain/container image used for SemantiST and AFL++ target builds",
    )
    add_advanced(
        "--container-runtime",
        choices=("auto", "docker", "podman"),
        default=os.environ.get("CONTAINER_RUNTIME", "auto"),
        help="container runtime; auto tries Docker then Podman (default: %(default)s)",
    )
    add_advanced(
        "--container-platform",
        "--docker-platform",
        dest="container_platform",
        help="container image platform; --docker-platform remains as a compatibility alias",
    )
    add_advanced("--with-replay", action="store_true", help="run common replay and merge it into run records")
    add_advanced("--with-coverage", action="store_true", help="run common coverage replay and merge it into run records")
    add_advanced("--analysis-timeout", type=float, default=2.0)
    add_advanced("--validate-complete", action="store_true", help="compatibility option; online validation is always enabled")
    add_advanced(
        "--require-valid-runs",
        action="store_true",
        default=True,
        help="require complete budgets and native executions (always enabled for online runs)",
    )
    analysis_group = advanced.add_mutually_exclusive_group()
    add_advanced("--aggregate", group=analysis_group, dest="skip_aggregate", action="store_false",
                 help="also generate legacy summaries after successful validation")
    add_advanced("--skip-aggregate", group=analysis_group, dest="skip_aggregate", action="store_true",
                 help="save raw results for separate offline analysis (default)")
    parser.set_defaults(skip_aggregate=True)
    add_advanced("--serial", action="store_true", help="run one trial at a time")
    args = parser.parse_args(argv)
    for name, value in EXPERIMENT_PRESETS[args.experiment].items():
        if getattr(args, name) is None:
            setattr(args, name, list(value) if name == "target_id" else value)
    if (not args.experiment_id or args.experiment_id in {".", ".."}
            or args.experiment_id != args.experiment_id.strip()
            or any(char in args.experiment_id for char in "/\\:|")):
        parser.error("--experiment-id must be a new directory name, not a path")
    for name in ("trials", "fuzz_time", "timeout_ms", "semantic_task_budget"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not 1 <= args.semantist_fb_max_cycles <= 10000:
        parser.error("--semantist-fb-max-cycles must be in [1, 10000]")
    if args.semantist_fb_stale_threshold < 0:
        parser.error("--semantist-fb-stale-threshold must not be negative")
    args.observer_poll_interval_ms_requested = args.observer_poll_interval_ms
    args.observer_poll_interval_ms = 0
    if not 0 < args.analysis_timeout < float("inf"):
        parser.error("--analysis-timeout must be finite and positive")
    return args


def main(argv: list[str] | None = None, *, description: str | None = None) -> int:
    args = parse_args(argv, description=description)
    tools = normalize_tools(args.tools)
    if args.semantist_ablation != "full" and tools != ["semantist_full"]:
        raise SystemExit("SemantiST ablations require --tools semantist_full in an independent run")
    experiment_dir = args.artifact_root.resolve() / args.experiment_id
    if experiment_dir.exists() or experiment_dir.is_symlink():
        raise SystemExit(f"experiment directory already exists; choose a new --experiment-id: {experiment_dir}")
    cpus = parse_cpus(args.cpus)
    unavailable = unavailable_cpus(cpus)
    if unavailable:
        raise SystemExit(
            "requested CPUs are outside the runner affinity mask: "
            + ", ".join(str(cpu) for cpu in unavailable)
        )
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
    if not args.preflight_only:
        validate_execution_environment(
            container_runtime,
            container_runtime_status,
            tools,
            args.semantist_image,
            args.container_platform,
        )
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
        semantist_fb_state_trace=args.semantist_fb_state_trace,
        observer_poll_interval_ms=args.observer_poll_interval_ms,
        cpuset=args.cpus,
        container_runtime=container_runtime,
        container_runtime_status=container_runtime_status,
        container_platform=args.container_platform,
        semantist_image=args.semantist_image,
        semantist_ablation=args.semantist_ablation,
    )
    manifest = load_manifest(args.manifest, ROOT)
    targets = select_targets(manifest.targets, args.target_id, args.limit_targets)
    if not targets:
        raise SystemExit("benchmark selection is empty")
    adapters = {tool: ADAPTERS[tool](config) for tool in tools}
    try:
        experiment_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise SystemExit(f"experiment directory already exists; choose a new --experiment-id: {experiment_dir}") from error

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
            "experiment": args.experiment,
            "benchmark_manifest": str(manifest.path),
            "benchmark_manifest_sha256": manifest.sha256,
            "tools": tools,
            "target_count": len(targets),
            "trial_count": args.trials,
            "seed_namespace": args.seed_namespace,
            "configuration_id": args.semantist_ablation,
            "collection_mode": "post_run",
            "targets": [experiment_manifest_entry(target, tools) for target in targets],
        },
    )

    support = write_support_matrix(experiment_dir, tools, targets, adapters)
    schedule = write_schedule(
        experiment_dir,
        config.experiment_id,
        tools,
        targets,
        args.trials,
        seed_namespace=args.seed_namespace,
    )
    print(
        f"[comparison] prepared schedule: {len(tools)} tools x {len(targets)} targets x {args.trials} trials = {len(schedule)} rows"
    )
    persist_campaign_state(experiment_dir, schedule, status="prepared")
    unsupported_tools = [tool for tool in tools if not any(
        support[(tool, target.target_id)].status == "supported" for target in targets
    )]
    if unsupported_tools:
        message = ("no supported targets for " + ", ".join(unsupported_tools)
                   + f"; inspect {experiment_dir / 'support-matrix.csv'}")
        persist_campaign_state(experiment_dir, schedule, status="failed", stop_reason=message)
        print(f"[comparison] {message}", file=sys.stderr)
        return 1
    if args.preflight_only:
        print(f"[comparison] preflight artifacts written to {experiment_dir}")
        return 0

    try:
        preprocessing = prepare_supported_targets(tools, targets, adapters, support)
        update_support_matrix_preprocessing(experiment_dir, preprocessing)
        run_online_matrix(experiment_dir, schedule, targets, adapters, support, preprocessing, cpus, serial=args.serial)
        persist_campaign_state(experiment_dir, schedule, status="validating")
        errors = validate_complete(experiment_dir, require_valid_runs=True)
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        message = f"{type(error).__name__}: {error}"
        persist_campaign_state(experiment_dir, schedule, status="failed", stop_reason=message)
        print(f"[comparison] campaign stopped: {message}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    if errors:
        persist_campaign_state(experiment_dir, schedule, status="invalid", validation_errors=errors)
        for error in errors:
            print(f"[comparison] validation error: {error}", file=sys.stderr)
        return 1
    try:
        if args.with_replay:
            replay_records = replay_experiment(
                experiment_dir,
                args.analysis_timeout,
                container_runtime=container_runtime,
                semantist_image=args.semantist_image,
                container_platform=args.container_platform,
            )
            merge_replay_results(experiment_dir, replay_records)
            persist_campaign_state(experiment_dir, schedule, status="analyzing")
        if args.with_coverage:
            coverage_records = coverage_experiment(
                experiment_dir,
                args.analysis_timeout,
                container_runtime=container_runtime,
                semantist_image=args.semantist_image,
                container_platform=args.container_platform,
            )
            merge_coverage_results(experiment_dir, coverage_records)
            persist_campaign_state(experiment_dir, schedule, status="analyzing")
        if not args.skip_aggregate:
            aggregate_experiment(experiment_dir)
        persist_campaign_state(experiment_dir, schedule, status="completed")
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        message = f"{type(error).__name__}: {error}"
        persist_campaign_state(experiment_dir, schedule, status="failed", stop_reason=message)
        print(f"[comparison] analysis stopped: {message}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(f"[comparison] experiment artifacts: {experiment_dir}")
    print(f"[comparison] per-trial metrics: {experiment_dir / 'metrics.csv'}")
    return 0


def normalize_tools(text: str) -> list[str]:
    aliases = {
        "semantist": "semantist_full",
        "aflplusplus": "aflplusplus_full",
        "afl++": "aflplusplus_full",
        "icsfuzz": "icsfuzz_full",
        "icsquartz": "icsquartz_full",
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


def validate_execution_environment(
    container_runtime: str | None,
    container_runtime_status: str,
    tools: list[str],
    semantist_image: str,
    container_platform: str | None,
) -> None:
    """Fail before creating run records when required runtime images are unusable."""
    if container_runtime is None:
        raise SystemExit(f"container runtime is unavailable: {container_runtime_status}")

    required_images: dict[str, str] = {}
    if any(tool in {"semantist_full", "aflplusplus_full", "icsquartz_full"} for tool in tools):
        required_images["SemantiST toolchain"] = semantist_image
    required_images.update(
        {f"{name} baseline": image for name, image in _baseline_images_for_tools(tools).items()}
    )
    missing = [
        f"{name} ({image})"
        for name, image in required_images.items()
        if container_image_id(container_runtime, image) is None
    ]
    if missing:
        raise SystemExit(
            "required container image(s) are missing or unreadable: " + ", ".join(missing)
            + "; build the pinned images before starting the experiment"
        )

    if "semantist_full" not in tools:
        return
    probe = container_run_command(
        container_runtime,
        semantist_image,
        platform=container_platform,
        command=["/opt/semantist/cargo-target/release/semantist", "--help"],
    )
    try:
        result = subprocess.run(
            probe,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"SemantiST image interface probe timed out: {semantist_image}") from error
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 or "--ablation" not in output:
        raise SystemExit(
            f"SemantiST image {semantist_image!r} does not contain the current experiment CLI "
            "(--ablation is missing); rebuild it from the current source tree"
        )


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
        "experiment": args.experiment,
        "host": {"hostname": platform.node(), "platform": platform.platform(),
                 "machine": platform.machine(), "logical_cpus": os.cpu_count()},
        "serial_trials": args.serial,
        "semantist_root": str(ROOT),
        "workspace_root": str(args.workspace_root.resolve()),
        "artifact_root": str(args.artifact_root.resolve()),
        "icsquartz_root": str(icsquartz_root) if icsquartz_root is not None else None,
        "tools": tools,
        "target_count": len(targets),
        "trial_count": args.trials,
        "seed_namespace": args.seed_namespace,
        "semantist_ablation": args.semantist_ablation,
        "online_budget_seconds": args.fuzz_time,
        "per_execution_timeout_ms": args.timeout_ms,
        "semantic_task_budget": args.semantic_task_budget,
        "semantist_fb_max_cycles": args.semantist_fb_max_cycles,
        "semantist_fb_stale_threshold": args.semantist_fb_stale_threshold,
        "semantist_fb_state_trace": args.semantist_fb_state_trace,
        "observer_poll_interval_ms": args.observer_poll_interval_ms,
        "observer_poll_interval_ms_requested": args.observer_poll_interval_ms_requested,
        "collection_mode": "post_run",
        "cpus": args.cpus,
        "cpus_bound": True,
        "cpus_isolated": args.cpus_isolated,
        "tool_cpu_slots": {
            tool: int(getattr(ADAPTERS[tool], "cpu_slots", 1))
            for tool in tools
        },
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
    *,
    seed_namespace: str | None = None,
) -> list[dict[str, Any]]:
    rows = []
    order = 0
    for tool in tools:
        for target in targets:
            for trial_id in range(1, trials + 1):
                run_key = f"{experiment_id}|{tool}|{target.suite}|{target.target_id}|trial_{trial_id:02d}"
                seed_key = (
                    f"{seed_namespace or experiment_id}|{target.suite}|"
                    f"{target.target_id}|trial_{trial_id:02d}"
                )
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
                        "rng_seed": rng_seed(seed_key),
                    }
                )
                order += 1
    rows.sort(key=lambda row: row["sort_key"])
    with (experiment_dir / "schedule.jsonl").open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
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
    *,
    serial: bool = False,
) -> None:
    started_run_keys: set[str] = set()
    by_target = {target.target_id: target for target in targets}
    immediate = []
    online = []
    for row in schedule:
        target = by_target[row["target_id"]]
        adapter = adapters[row["tool"]]
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

    persist_campaign_state(experiment_dir, schedule, status="running", started_run_keys=started_run_keys)

    print(f"[comparison] online rows: {len(online)}; immediate records: {len(immediate)}", flush=True)
    max_slots_required = max(
        int(getattr(adapters[row["tool"]], "cpu_slots", 1))
        for row in online
    ) if online else 1
    if len(cpus) < max_slots_required:
        raise SystemExit(
            f"selected tools require at least {max_slots_required} CPU slots; "
            f"got {len(cpus)} from --cpus"
        )

    batches = ([[(row, cpus[:int(getattr(adapters[row["tool"]], "cpu_slots", 1))])]
                for row in online] if serial else _cpu_scheduled_batches(online, adapters, cpus))
    for batch in batches:
        infrastructure_errors = []
        futures = {}
        try:
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                for row, assigned_cpus in batch:
                    target = by_target[row["target_id"]]
                    adapter = adapters[row["tool"]]
                    prep = preprocessing[(row["tool"], target.target_id)]
                    future = executor.submit(execute_online_row, row, target, adapter, prep, assigned_cpus)
                    futures[future] = row
                    started_run_keys.add(row["run_key"])
        finally:
            # Leaving the executor waits for submitted trials. Retain their
            # results even if scheduling was interrupted partway through.
            for future, row in futures.items():
                if not future.done():
                    infrastructure_errors.append(f"{row['run_key']}: trial termination is unconfirmed")
                    continue
                try:
                    row, run_dir, result = future.result()
                    write_run_result(run_dir, result)
                except (Exception, SystemExit, KeyboardInterrupt) as error:
                    infrastructure_errors.append(f"{row['run_key']}: {error!r}")
                    print(f"[comparison] cannot finalize {row['run_key']}: {error!r}", file=sys.stderr)
                    continue
                if result.get("tool_specific_metrics", {}).get("infrastructure_failure") is True:
                    infrastructure_errors.append(result["run_key"])
                print(
                    f"[comparison] completed {row['tool']} {row['target_id']} trial {row['trial_id']}: {result['run_status']}",
                    flush=True,
                )
            stop_reason = (
                "infrastructure failure or unconfirmed cleanup; remaining batches were not started: "
                + ", ".join(infrastructure_errors)
            ) if infrastructure_errors else None
            # The executor has finished this batch before reading run records.
            persist_campaign_state(
                experiment_dir, schedule,
                status="failed" if infrastructure_errors else "running",
                started_run_keys=started_run_keys,
                stop_reason=stop_reason,
            )
        if infrastructure_errors:
            raise RuntimeError(stop_reason)


def _cpu_scheduled_batches(
    online: list[dict[str, Any]],
    adapters: dict[str, Any],
    cpus: list[int],
) -> list[list[tuple[dict[str, Any], list[int]]]]:
    batches: list[list[tuple[dict[str, Any], list[int]]]] = []
    index = 0
    while index < len(online):
        batch: list[tuple[dict[str, Any], list[int]]] = []
        cursor = 0
        while index < len(online):
            row = online[index]
            slots = int(getattr(adapters[row["tool"]], "cpu_slots", 1))
            if slots < 1:
                slots = 1
            if slots > len(cpus):
                raise SystemExit(
                    f"tool {row['tool']} requires {slots} CPU slots; got {len(cpus)} from --cpus"
                )
            if cursor + slots > len(cpus):
                break
            batch.append((row, cpus[cursor : cursor + slots]))
            cursor += slots
            index += 1
        if not batch:
            raise RuntimeError("internal CPU scheduler could not place any online row")
        batches.append(batch)
    return batches


def execute_online_row(
    row: dict[str, Any],
    target: Any,
    adapter: Any,
    prep: PreprocessingResult,
    cpus: list[int],
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    run_dir = adapter.run_dir(target, row["trial_id"])
    result = make_base_result(row, target, adapter, "supported")
    result["assigned_cpus"] = cpus
    result.update(_preprocessing_fields(prep))
    try:
        run_dir = adapter.materialize_trial(target, prep, row["trial_id"])
        cpu_arg: int | list[int] = cpus[0] if len(cpus) == 1 else cpus
        updates = adapter.run(target, run_dir, prep, row["rng_seed"], cpu_arg)
        result.update(updates)
        if "candidate_findings" not in updates:
            result["candidate_collection_complete"] = False
    except AdapterError as error:
        result.update(
            {
                "run_status": "runner_error",
                "online_elapsed_seconds": None,
                "candidate_collection_complete": False,
                "failure_reason": error.to_failure_reason(),
            }
        )
        recover_process_result(run_dir, result)
    except Exception as error:  # noqa: BLE001 - preserve the failed row before stopping later batches.
        result.update(
            {
                "run_status": "runner_error",
                "online_elapsed_seconds": None,
                "candidate_collection_complete": False,
                "failure_reason": {
                    "code": "adapter_unhandled_exception",
                    "message": repr(error),
                    "log_tail_path": None,
                },
            }
        )
        recover_process_result(run_dir, result)
    return row, run_dir, result


def recover_process_result(run_dir: Path, result: dict[str, Any]) -> None:
    """Recover stopped-process timing and cleanup evidence after collection fails."""
    result.update({"corpus_count": None, "crash_count_raw": None, "timeout_count_raw": None})
    lifecycle: dict[str, Any] = {}
    try:
        value = json.loads((run_dir / "process-result.json").read_text(encoding="utf-8"))
        if isinstance(value, dict):
            lifecycle = value
    except (OSError, ValueError):
        pass
    cleanup_confirmed = lifecycle.get("cleanup_confirmed") is True
    result["t0_monotonic_ns"] = lifecycle.get("t0_monotonic_ns")
    result["online_elapsed_seconds"] = lifecycle.get("elapsed_seconds")
    events = lifecycle.get("events")
    if isinstance(events, list) and all(isinstance(event, dict) for event in events):
        common = [event for event in events if event.get("oracle") in {"asan", "ubsan", "target_signal"}]
        semantic = [event for event in events if event.get("oracle") == "semantic_objective"]
        result.update({
            "candidate_findings": events, "finding_found": bool(events),
            "common_oracle_finding_found": bool(common), "first_common_finding": common[0] if common else None,
            "semantic_objective_found": bool(semantic), "first_semantic_objective": semantic[0] if semantic else None,
            "candidate_collection_complete": lifecycle.get("collection_complete") is True,
            "crash_count_raw": len(common) if lifecycle.get("collection_complete") is True else None,
            "timeout_count_raw": sum(event.get("oracle") == "testcase_timeout" for event in events)
            if lifecycle.get("collection_complete") is True else None,
        })
    result["tool_specific_metrics"].update({
        "cleanup_confirmed": cleanup_confirmed,
        "infrastructure_failure": not cleanup_confirmed or lifecycle.get("infrastructure_failure") is True,
        "process_result_recovered": bool(lifecycle),
    })


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
            "measurement_contract": CONTRACT,
            "st_file_sha256": target.st_file_sha256,
            "benchmark_fixture_sha256": target.fixture_hashes,
            "cpu_slots": int(getattr(adapter, "cpu_slots", 1)),
            "fuzz_workers": 1,
            "replay_image": adapter.config.semantist_image,
            "collection_mode": "post_run",
            "observer_poll_interval_ms": 0,
        }
    )
    if row["tool"] == "semantist_full":
        result["configuration_id"] = adapter.config.semantist_ablation
    return result


def write_run_result(run_dir: Path, result: dict[str, Any]) -> None:
    result["paper_metrics"] = paper_metrics(result)
    result["artifact_paths"] = result.get("artifact_paths") or {}
    result["artifact_paths"]["run_result"] = "run-result.json"
    validate_run_result(result)
    write_json(run_dir / "run-result.json", result)


def _run_result_path(experiment_dir: Path, row: dict[str, Any]) -> Path:
    return (
        experiment_dir / "runs" / row["tool"] / row["suite"] / row["target_id"]
        / f"trial_{row['trial_id']:02d}" / "run-result.json"
    )


def write_raw_runs(
    experiment_dir: Path,
    schedule: list[dict[str, Any]],
    *,
    allow_partial: bool = False,
    read_errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    records = []
    for row in schedule:
        path = _run_result_path(experiment_dir, row)
        if not path.is_file():
            if allow_partial:
                continue
            raise RuntimeError(f"missing run result for {row['run_key']}: {path}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("run result must be an object")
            validate_run_result(record)
            if any(record.get(key) != row[key] for key in ("run_key", "tool", "suite", "target_id", "trial_id")):
                raise ValueError("run identity differs from schedule")
        except (OSError, ValueError, TypeError) as error:
            if not allow_partial:
                raise
            if read_errors is not None:
                read_errors.append(f"{row['run_key']}: cannot read run result: {error}")
            continue
        records.append(record)
    temporary = experiment_dir / "raw-runs.jsonl.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(experiment_dir / "raw-runs.jsonl")
    return records


def persist_campaign_state(
    experiment_dir: Path,
    schedule: list[dict[str, Any]],
    *,
    status: str,
    started_run_keys: set[str] | None = None,
    stop_reason: str | None = None,
    validation_errors: list[str] | None = None,
) -> None:
    """Save available rows and their status only between stopped batches."""
    status_path = experiment_dir / "campaign-status.json"
    if started_run_keys is None:
        previous = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
        started_run_keys = set(previous.get("started", []))
    read_errors: list[str] = []
    records = write_raw_runs(experiment_dir, schedule, allow_partial=True, read_errors=read_errors)
    by_key = {record["run_key"]: record for record in records}
    completed = []
    failed = []
    not_started = []
    for row in schedule:
        key = row["run_key"]
        record = by_key.get(key)
        if record is None:
            (failed if key in started_run_keys or _run_result_path(experiment_dir, row).is_file() else not_started).append(key)
        elif record.get("run_status") == "unsupported" or paper_metrics(record).get("valid_run") is True:
            completed.append(key)
        else:
            failed.append(key)
    errors = [*(validation_errors or []), *read_errors]
    payload = {
        "schema_version": "semantist.comparison-campaign-status/1.0.0",
        "status": "failed" if read_errors else status,
        "collection_mode": "post_run",
        "scheduled_count": len(schedule),
        "recorded_count": len(records),
        "completed": completed,
        "failed": failed,
        "not_started": not_started,
        "started": sorted(started_run_keys),
        "stop_reason": stop_reason,
        "validation_errors": errors,
    }
    temporary = experiment_dir / "campaign-status.json.tmp"
    write_json(temporary, payload)
    temporary.replace(status_path)
    # Partial campaigns still expose every available trial's validity and
    # failure status; comparative aggregation remains gated by validation.
    export_metrics(records, experiment_dir)


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
