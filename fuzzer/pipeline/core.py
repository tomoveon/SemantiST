"""STG semantic-guided fuzzing pipeline.

The JSON bootstrap file is an interchange/debug artifact. During fuzzing, the
Rust binary loads its records into LibAFL Testcase metadata, which remains the
runtime source of truth.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from compiler.instrumentation.compile import target_name, with_llvm_toolchain_env  # noqa: E402
from compiler.toolchain.compatibility import load_project  # noqa: E402
from fuzzer.corpus.store import CorpusStore, append_event, source_constraints  # noqa: E402
from fuzzer.runtime.paths import build_artifact_dir, runs_artifact_dir  # noqa: E402
from fuzzer.reporting.report import generate_report  # noqa: E402
from fuzzer.semantic.planning import generate_semantic_task_plan  # noqa: E402
from fuzzer.pipeline.campaign import Campaign  # noqa: E402
from fuzzer.pipeline.goal_tasks import write_goal_tasks  # noqa: E402


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)



@dataclass(frozen=True)
class Layout:
    run_dir: Path
    unified: Path
    unified_seeds: Path
    unified_runtime: Path
    target: Path
    findings: Path
    build: Path
    generated_seeds: Path
    bootstrap: Path
    events: Path

    @classmethod
    def from_run_dir(cls, run_dir: Path, build_dir: Path) -> "Layout":
        return cls(
            run_dir=run_dir,
            unified=run_dir / "unified-corpus",
            unified_seeds=run_dir / "unified-corpus" / "seeds",
            unified_runtime=run_dir / "unified-corpus" / "runtime",
            target=run_dir / "target-corpus",
            findings=run_dir / "findings",
            build=build_dir,
            generated_seeds=build_dir / "generated-seeds",
            bootstrap=run_dir / "testcase-metadata.bootstrap.json",
            events=run_dir / "events.jsonl",
        )

    def mkdirs(self) -> None:
        for path in (
            self.unified_seeds,
            self.unified_runtime,
            self.target,
            self.findings,
        ):
            path.mkdir(parents=True, exist_ok=True)


def run_command(command: list[str], env: dict[str, str] | None = None, timeout: float | None = None) -> None:
    print("+", " ".join(command))
    process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


def ingest_initial_seeds(st_file: Path, layout: Layout, store: CorpusStore) -> int:
    added = 0
    seed_dir = st_file.parent / "seeds"
    seed_paths = sorted(seed_dir.glob("*.seed")) if seed_dir.exists() else []
    seed_paths += sorted(layout.generated_seeds.glob("*"))
    for path in seed_paths:
        if path.is_file() and path.stat().st_size:
            _, is_new = store.add_seed(path.read_bytes(), "Initial")
            added += int(is_new)
    store.persist()
    append_event(layout, "initial_ingress", new_seed_count=added)
    return added


def target_build_dir(function: str) -> Path:
    return build_artifact_dir() / "targets" / target_name(function)


def create_layout(run_dir: Path, report_only: bool, build_dir: Path) -> Layout:
    layout = Layout.from_run_dir(run_dir, build_dir)
    if report_only:
        if not run_dir.is_dir():
            raise RuntimeError(f"--report-only requires an existing run directory: {run_dir}")
    elif run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory already contains results: {run_dir}")
    layout.mkdirs()
    return layout


def build_targets(args: argparse.Namespace, layout: Layout) -> Path:
    layout.build.mkdir(parents=True, exist_ok=True)
    if layout.generated_seeds.exists():
        shutil.rmtree(layout.generated_seeds)
    layout.generated_seeds.mkdir(parents=True, exist_ok=True)
    llvm_ir = layout.build / "target.ll"
    target_bin = layout.build / "fuzz_target"
    env = with_llvm_toolchain_env(os.environ.copy())
    configured_build_timeout = getattr(args, "build_timeout", 120.0)
    build_timeout_s = 120.0 if configured_build_timeout is None else float(configured_build_timeout)
    if build_timeout_s <= 0:
        raise ValueError("--build-timeout must be greater than zero")
    build_deadline = time.monotonic() + build_timeout_s
    # The parent process enforces this deadline for each phase.  The absolute
    # wall-clock value lets build_target.sh pass the same remaining budget to
    # its symbolic build instead of restarting a fresh default timeout.
    build_deadline_ns = time.time_ns() + int(build_timeout_s * 1_000_000_000)
    env["SEMANTIST_BUILD_DEADLINE_EPOCH_NS"] = str(build_deadline_ns)
    vclock_lib = env.get("SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB")
    if not vclock_lib:
        candidate = "/opt/rusty-stdlib-vclock/target/release/libiec61131std.a"
        if Path(candidate).exists():
            env["SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB"] = candidate
    env.update(
        {
            "OUT_LL": str(llvm_ir),
            "OUT_OBJ": str(layout.build / "target.o"),
            "OUT_BIN": str(target_bin),
            "HARNESS_OUT": str(layout.build / "harness.c"),
            "GENERATED_SEED_DIR": str(layout.generated_seeds),
            "SEMANTIST_SEMANTIC_INSTRUMENTATION": "ir",
            "SEMANTIST_BUILD_COMMON": os.environ.get("SEMANTIST_BUILD_COMMON", "0"),
            "SEMANTIST_INPROCESS_REQUIRED": "1",
        }
    )
    stg_dir = layout.run_dir / "stg"
    env["SEMANTIST_STG_DIR"] = str(stg_dir)
    env["SEMANTIST_SEMANTIC_RUNTIME_IDS"] = str(stg_dir / "stg-runtime-ids.json")
    if getattr(args, "semantic_debug_artifacts", False):
        env["SEMANTIST_STG_DEBUG_SIDECARS"] = "1"
        env["SEMANTIST_STG_DOT"] = "1"
    library = getattr(args, "library", None)
    stubs = getattr(args, "stubs", None)
    compatibility_manifest = getattr(args, "compatibility_manifest", None)
    profile = getattr(args, "profile", None)
    if library is not None:
        env["SEMANTIST_LIBRARY_FILE"] = str(library)
    if stubs is not None:
        env["SEMANTIST_LIBRARY_STUBS"] = str(stubs)
    if compatibility_manifest is not None:
        env["SEMANTIST_COMPATIBILITY_MANIFEST"] = str(compatibility_manifest)
    if profile:
        env["SEMANTIST_PROFILE"] = str(profile)

    remaining = max(0.01, build_deadline - time.monotonic())
    run_command(["./compiler/scripts/compile_st.sh", str(args.st_file), args.function], env=env, timeout=remaining)
    remaining = max(0.01, build_deadline - time.monotonic())
    run_command(["./compiler/scripts/build_target.sh", str(args.st_file), args.function], env=env, timeout=remaining)
    append_event(
        layout,
        "build_targets",
        build_dir=str(layout.build),
        target=str(target_bin),
        llvm_ir=str(llvm_ir),
        generated_seeds=str(layout.generated_seeds),
        stg_dir=str(stg_dir),
        semantic_instrumentation="ir",
        build_timeout_s=build_timeout_s,
    )
    return target_bin


def prepare_bundle(args: argparse.Namespace, layout: Layout) -> dict:
    import shutil

    source_bundle = layout.build / "bundle"
    bundle = layout.run_dir / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    for name in ("execution-contract.json", "abi.json", "state-layout.json"):
        if (source_bundle / name).exists():
            shutil.copy2(source_bundle / name, bundle / name)
    if (source_bundle / "klee").is_dir():
        shutil.copytree(source_bundle / "klee", bundle / "klee", dirs_exist_ok=True)
    contract = {}
    contract_path = bundle / "execution-contract.json"
    if contract_path.exists():
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    plan_path = layout.run_dir / "semantic-task-plan.json"
    write_goal_tasks(
        plan_path,
        bundle / "abi.json",
        bundle / "state-layout.json",
        bundle / "goal-tasks.json",
        contract_id=contract.get("contract_id"),
        runtime_ids_path=layout.run_dir / "stg" / "stg-runtime-ids.json",
        stg_model_path=layout.run_dir / "stg" / "stg-model.json",
        stg_effect_model_path=layout.run_dir / "stg" / "stg-effect-model.json",
    )
    return contract


def run_campaign(args: argparse.Namespace, layout: Layout, target_bin: Path, contract: dict) -> None:
    config = {
        "mode": "hybrid",
        "engine": "rust",
        "function": args.function,
        "st_file": str(args.st_file),
        "library": str(args.library) if getattr(args, "library", None) else None,
        "stubs": str(args.stubs) if getattr(args, "stubs", None) else None,
        "compatibility_manifest": (str(args.compatibility_manifest)
                                   if getattr(args, "compatibility_manifest", None) else None),
        "profile": getattr(args, "profile", None),
        "contract_id": contract.get("contract_id"),
        "scan_period_ns": contract.get("time", {}).get("scan_period_ns"),
        "timer_semantics": contract.get("time", {}).get("timer_semantics"),
        "fuzz_timeout": args.fuzz_timeout,
        "max_evaluations": args.fuzz_iterations,
        "seed": args.seed,
        "fb_max_cycles": getattr(args, "fb_max_cycles", None),
        "fb_stale_threshold": getattr(args, "fb_stale_threshold", None),
        "fb_state_trace": getattr(args, "fb_state_trace", False),
        "fb_state_trace_file": (str(args.fb_state_trace_file)
                                 if getattr(args, "fb_state_trace_file", None) else None),
        "ablations": {
            "no_symbolic": args.no_symbolic,
            "symbolic_initial_only": args.symbolic_initial_only,
            "symbolic_start": args.symbolic_start,
            "no_goal_prefix": args.no_goal_prefix,
            "no_distance": args.no_distance,
            "no_time_guidance": args.no_time_guidance,
            "no_snapshot": args.no_snapshot,
            "no_state_copy": args.no_state_copy,
            "llm": args.llm,
            "llm_initial_only": args.llm_initial_only,
        },
        "solver_jobs": args.solver_jobs,
        "full_fuzz_duration": args.full_fuzz_duration,
        "full_hybrid_duration": getattr(args, "full_hybrid_duration", False),
        "llm_max_calls": args.llm_max_calls,
        "llm_plan_rounds": getattr(args, "llm_plan_rounds", 3),
        "llm_plan_batch_size": getattr(args, "llm_plan_batch_size", 3),
        "llm_plan_token_budget": getattr(args, "llm_plan_token_budget", 20000),
        "llm_strict_planning": getattr(args, "llm_strict_planning", False),
        "symbolic_startup_budget_s": getattr(args, "symbolic_startup_budget", 30.0),
        "symbolic_startup_max_queries": 8,
        "online_symbolic_max_queries": 4,
        "solver_feedback_margin_s": getattr(args, "solver_feedback_margin", 15.0),
        "llm_offline": args.llm_offline,
        "llm_cooldown_s": 120.0,
        "llm_stagnation_s": 5.0,
        "llm_online_timeout_s": 10.0,
        "command": " ".join(sys.argv),
    }
    (layout.run_dir / "campaign.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        # Constructing Campaign loads every exchange document. Keep that
        # validation inside the failure boundary so a malformed bundle is
        # recorded as campaign_failed instead of escaping as an unreported
        # Python exception.
        campaign = Campaign(
            layout.run_dir,
            layout.run_dir / "bundle",
            target_bin,
            function=args.function,
            st_file=args.st_file,
            config=config,
            time_budget_s=float(args.fuzz_timeout or 600) + (180 if args.full_fuzz_duration else 0),
            jobs=max(1, args.search_jobs),
            seed=args.seed,
            max_evaluations=args.fuzz_iterations if args.fuzz_iterations is not None else 40000,
        )
        result = campaign.run()
    except Exception as error:  # noqa: BLE001 - preserve a machine-readable failed run
        append_event(layout, "campaign_failed", error=str(error), execution_status="campaign_failed")
        (layout.run_dir / "campaign-status.json").write_text(
            json.dumps({"status": "campaign_failed", "error": str(error),
                        "execution_status": {"build": "completed", "native": "failed"}},
                       indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    append_event(layout, "campaign", **result)
    (layout.run_dir / "campaign-status.json").write_text(
        json.dumps({"status": "completed", "execution_status": {"build": "completed",
                    "native": "completed"}, **result}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def write_vulnerability_report(args: argparse.Namespace, layout: Layout) -> bool:
    if args.skip_report:
        return True
    try:
        report, paths = generate_report(
            layout.run_dir,
            function=args.function,
            st_file=args.st_file,
            output_dir=args.report_output_dir,
            replay_limit=args.report_replay_limit,
            replay_timeout=args.report_replay_timeout,
            replay=not args.report_no_replay,
            minimize_findings=args.report_minimize_findings,
        )
    except Exception as error:  # noqa: BLE001 - report generation must not hide fuzzing results.
        append_event(layout, "report_failed", error=str(error))
        print(f"[pipeline] vulnerability report failed: {error}", file=sys.stderr)
        return False
    append_event(
        layout,
        "report_generated",
        status=report.summary["status"],
        confirmed_findings=report.summary["confirmed_findings"],
        outputs={kind: str(path) for kind, path in paths.items()},
    )
    print(f"[pipeline] vulnerability report: {paths['markdown']}")
    return True


def record_build_failure(args: argparse.Namespace, layout: Layout, error: Exception) -> bool:
    """Persist a failed build before attempting the best-effort public report."""
    append_event(
        layout,
        "build_failed",
        error=str(error),
        returncode=getattr(error, "returncode", None),
        execution_status="build_failed",
    )
    (layout.run_dir / "campaign-status.json").write_text(
        json.dumps(
            {
                "status": "build_failed",
                "error": str(error),
                "execution_status": {"build": "failed", "native": "UNSUPPORTED"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_vulnerability_report(args, layout)
    return False


def record_campaign_failure(layout: Layout, error: Exception) -> None:
    append_event(layout, "campaign_failed", error=str(error), execution_status="campaign_failed")
    (layout.run_dir / "campaign-status.json").write_text(
        json.dumps(
            {
                "status": "campaign_failed",
                "error": str(error),
                "execution_status": {"build": "completed", "native": "failed"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--st-file", required=True, type=Path)
    parser.add_argument("--function", required=True)
    parser.add_argument(
        "--library",
        type=Path,
        help="one-time-normalized ST library containing the selected POU",
    )
    parser.add_argument(
        "--stubs",
        type=Path,
        help="library-specific stubs from the completed intake workspace",
    )
    parser.add_argument(
        "--compatibility-manifest",
        type=Path,
        help="intake-generated manifest for ST sources and native compatibility providers",
    )
    parser.add_argument(
        "--profile",
        help="compatibility profile name; must be declared by --compatibility-manifest",
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help=f"run output directory; use {display_path(runs_artifact_dir())}/NAME to keep artifacts centralized",
    )
    parser.add_argument("--fuzz-iterations", type=int)
    parser.add_argument("--fuzz-timeout", type=int,
                        help="time limit in seconds; with --full-hybrid-duration this is the total campaign budget")
    parser.add_argument("--full-fuzz-duration", action="store_true",
                        help="run the native fuzz loop for the entire --fuzz-timeout duration")
    parser.add_argument("--full-hybrid-duration", action="store_true",
                        help="keep the native loop running within the total --fuzz-timeout campaign budget")
    parser.add_argument(
        "--build-timeout",
        type=float,
        default=120.0,
        help="shared deadline in seconds for compiler, native link, and symbolic build phases",
    )
    parser.add_argument(
        "--semantic-runtime-trace",
        action="store_true",
        help="write per-hit semantic runtime trace; useful for debugging but noisy for normal runs",
    )
    parser.add_argument(
        "--semantic-debug-artifacts",
        action="store_true",
        help="emit verbose semantic sidecars/traces for debugging; normal runs keep only mainline artifacts",
    )
    parser.add_argument("--fb-max-cycles", type=int, help="set SEMANTIST_FB_MAX_CYCLES for FUNCTION_BLOCK targets")
    parser.add_argument(
        "--fb-stale-threshold",
        type=int,
        help="set SEMANTIST_FB_STALE_THRESHOLD; 0 disables stale-cycle stopping",
    )
    parser.add_argument("--fb-state-trace", action="store_true", help="enable FUNCTION_BLOCK state trace")
    parser.add_argument(
        "--fb-state-trace-file",
        type=Path,
        help="write FUNCTION_BLOCK state trace to this file when --fb-state-trace is enabled",
    )
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    parser.add_argument("--report-output-dir", type=Path)
    parser.add_argument("--report-replay-limit", type=int, default=200)
    parser.add_argument("--report-replay-timeout", type=float, default=2.0)
    parser.add_argument("--report-no-replay", action="store_true")
    parser.add_argument(
        "--report-minimize-findings",
        action="store_true",
        help="run deterministic minimization for confirmed finding reproducers during report generation",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--search-jobs", type=int, default=12, help="parallel native probes in a campaign")
    parser.add_argument("--solver-jobs", type=int, default=1)
    parser.add_argument("--no-symbolic", action="store_true")
    parser.add_argument("--symbolic-initial-only", action="store_true")
    parser.add_argument("--symbolic-start", choices=("import", "replay"), default="import")
    parser.add_argument("--no-goal-prefix", action="store_true")
    parser.add_argument("--no-distance", action="store_true")
    parser.add_argument("--no-time-guidance", action="store_true")
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--no-state-copy", action="store_true")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-initial-only", action="store_true")
    parser.add_argument("--llm-max-calls", type=int, default=40)
    parser.add_argument("--llm-plan-rounds", type=int, choices=(3, 4, 5), default=3)
    parser.add_argument("--llm-plan-batch-size", type=int, default=3)
    parser.add_argument("--llm-plan-token-budget", type=int, default=20000)
    parser.add_argument("--llm-strict-planning", action="store_true")
    parser.add_argument("--symbolic-startup-budget", type=float, default=30.0)
    parser.add_argument("--solver-feedback-margin", type=float, default=15.0)
    parser.add_argument(
        "--llm-offline",
        action="store_true",
        help="read cached LLM plans only; no endpoint or API key is required",
    )
    args = parser.parse_args()
    if args.full_fuzz_duration and args.full_hybrid_duration:
        parser.error("--full-fuzz-duration and --full-hybrid-duration are mutually exclusive")
    return args


def main() -> int:
    args = parse_args()
    args.st_file = args.st_file.resolve()
    if args.library is not None:
        args.library = args.library.resolve()
    if args.stubs is not None:
        args.stubs = args.stubs.resolve()
    if args.compatibility_manifest is not None:
        args.compatibility_manifest = args.compatibility_manifest.resolve()
    if args.profile:
        if args.compatibility_manifest is None:
            raise SystemExit("--profile requires --compatibility-manifest")
        try:
            project = load_project(args.compatibility_manifest)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"invalid compatibility manifest for --profile: {error}") from error
        if args.profile not in project.profiles:
            declared = ", ".join(project.profiles) or "<none>"
            raise SystemExit(
                f"profile {args.profile!r} is not declared by {args.compatibility_manifest}; "
                f"declared profiles: {declared}"
            )
    args.run_dir = args.run_dir.resolve()
    layout = create_layout(args.run_dir, args.report_only, target_build_dir(args.function))
    st_source = args.st_file.read_text(encoding="utf-8")
    constraints = source_constraints(st_source)
    store = CorpusStore(layout, constraints)

    if args.report_only:
        return 0 if write_vulnerability_report(args, layout) else 1

    try:
        target_bin = build_targets(args, layout)
    except Exception as error:  # noqa: BLE001 - expose compiler/codegen failures in run artifacts
        record_build_failure(args, layout, error)
        return 1
    try:
        stg_dir = layout.run_dir / "stg"
        plan = generate_semantic_task_plan(
            stg_dir / "stg-model.json",
            stg_dir / "stg-runtime-ids.json",
            layout.run_dir / "semantic-task-plan.json",
        )
        print(f"[pipeline] generated {len(plan['tasks'])} STG semantic tasks")
        ingest_initial_seeds(args.st_file, layout, store)
    except Exception as error:  # noqa: BLE001 - setup failures must remain machine-readable
        record_campaign_failure(layout, error)
        write_vulnerability_report(args, layout)
        return 1
    try:
        contract = prepare_bundle(args, layout)
    except Exception as error:  # noqa: BLE001 - malformed exchange JSON is a failed campaign
        record_campaign_failure(layout, error)
        write_vulnerability_report(args, layout)
        return 1
    if args.build_only:
        append_event(layout, "build_only_complete")
        report_ok = write_vulnerability_report(args, layout)
        print(f"[pipeline] build-only complete: {layout.run_dir}")
        return 0 if report_ok else 1
    report_ok = True
    try:
        run_campaign(args, layout, target_bin, contract)
    finally:
        report_ok = write_vulnerability_report(args, layout)
    return 0 if report_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
