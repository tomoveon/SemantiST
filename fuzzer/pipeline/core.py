"""STG semantic-guided fuzzing pipeline.

The JSON bootstrap file is an interchange/debug artifact. During fuzzing, the
Rust binary loads its records into LibAFL Testcase metadata, which remains the
runtime source of truth.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from compiler.instrumentation.compile import target_name, with_llvm_toolchain_env  # noqa: E402
from fuzzer.corpus.store import CorpusStore, append_event, source_constraints  # noqa: E402
from fuzzer.runtime.paths import build_artifact_dir, cargo_target_dir, runs_artifact_dir  # noqa: E402
from fuzzer.reporting.report import generate_report  # noqa: E402
from fuzzer.semantic.planning import generate_semantic_task_plan  # noqa: E402


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


def run_command(command: list[str], env: dict[str, str] | None = None, timeout: int | None = None) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, check=True)


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
    env.update(
        {
            "OUT_LL": str(llvm_ir),
            "OUT_OBJ": str(layout.build / "target.o"),
            "OUT_BIN": str(target_bin),
            "HARNESS_OUT": str(layout.build / "harness.c"),
            "GENERATED_SEED_DIR": str(layout.generated_seeds),
            "SEMANTIST_SEMANTIC_INSTRUMENTATION": "ir",
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
    if library is not None:
        env["SEMANTIST_LIBRARY_FILE"] = str(library)
    if stubs is not None:
        env["SEMANTIST_LIBRARY_STUBS"] = str(stubs)
    if compatibility_manifest is not None:
        env["SEMANTIST_COMPATIBILITY_MANIFEST"] = str(compatibility_manifest)

    run_command(["./compiler/scripts/compile_st.sh", str(args.st_file), args.function], env=env)
    run_command(["./compiler/scripts/build_target.sh", str(args.st_file), args.function], env=env)
    append_event(
        layout,
        "build_targets",
        build_dir=str(layout.build),
        target=str(target_bin),
        llvm_ir=str(llvm_ir),
        generated_seeds=str(layout.generated_seeds),
        stg_dir=str(stg_dir),
        semantic_instrumentation="ir",
    )
    return target_bin


def launch_fuzzer(
    args: argparse.Namespace,
    layout: Layout,
    target_bin: Path,
) -> None:
    command = [
        "cargo",
        "run",
        "-p",
        "semantist",
        "--release",
        "--locked",
        "--",
        "--target",
        str(target_bin),
        "--function",
        args.function,
        "--run-dir",
        str(layout.run_dir),
        "--metadata-bootstrap",
        str(layout.bootstrap),
        "--semantic-task-budget",
        str(args.semantic_task_budget),
    ]
    command += [
        "--semantic-task-plan",
        str(layout.run_dir / "semantic-task-plan.json"),
        "--semantic-model",
        str(layout.run_dir / "stg" / "stg-model.json"),
    ]
    if args.fuzz_iterations:
        command += ["--fuzz-iterations", str(args.fuzz_iterations)]
    env = os.environ.copy()
    env.setdefault("CARGO_TARGET_DIR", str(cargo_target_dir()))
    env["SEMANTIST_SEMANTIC_TRACE_FILE"] = str(layout.run_dir / "semantic-coverage.jsonl")
    semantic_debug_artifacts = getattr(args, "semantic_debug_artifacts", False)
    if args.semantic_runtime_trace or semantic_debug_artifacts:
        env["SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE"] = str(
            layout.run_dir / "semantic-runtime.jsonl"
        )
        env["SEMANTIST_SEMANTIC_TRACE_RUNTIME"] = "1"
    if semantic_debug_artifacts:
        env["SEMANTIST_SEMANTIC_STATE_DETAIL"] = "1"
    runtime_ids = layout.run_dir / "stg" / "stg-runtime-ids.json"
    if runtime_ids.exists():
        env["SEMANTIST_SEMANTIC_RUNTIME_IDS"] = str(runtime_ids)
    apply_fb_runtime_env(args, env)
    try:
        run_command(command, env=env, timeout=args.fuzz_timeout)
    except subprocess.TimeoutExpired:
        append_event(layout, "fuzz_timeout", timeout_seconds=args.fuzz_timeout)
        print(f"[pipeline] fuzzing reached --fuzz-timeout={args.fuzz_timeout}s; generating report")


def apply_fb_runtime_env(args: argparse.Namespace, env: dict[str, str]) -> None:
    if getattr(args, "fb_max_cycles", None) is not None:
        env["SEMANTIST_FB_MAX_CYCLES"] = str(args.fb_max_cycles)
    if getattr(args, "fb_stale_threshold", None) is not None:
        env["SEMANTIST_FB_STALE_THRESHOLD"] = str(args.fb_stale_threshold)
    if getattr(args, "fb_state_trace", False):
        env["SEMANTIST_FB_STATE_TRACE"] = "1"
        trace_file = getattr(args, "fb_state_trace_file", None)
        if trace_file is not None:
            env["SEMANTIST_FB_STATE_TRACE_FILE"] = str(trace_file)


def write_vulnerability_report(args: argparse.Namespace, layout: Layout) -> None:
    if args.skip_report:
        return
    try:
        report, paths = generate_report(
            layout.run_dir,
            function=args.function,
            st_file=args.st_file,
            llm_provider=args.report_llm_provider or "mock",
            output_dir=args.report_output_dir,
            replay_limit=args.report_replay_limit,
            replay_timeout=args.report_replay_timeout,
            replay=not args.report_no_replay,
            minimize_findings=args.report_minimize_findings,
        )
    except Exception as error:  # noqa: BLE001 - report generation must not hide fuzzing results.
        append_event(layout, "report_failed", error=str(error))
        print(f"[pipeline] vulnerability report failed: {error}", file=sys.stderr)
        return
    append_event(
        layout,
        "report_generated",
        status=report.summary["status"],
        confirmed_findings=report.summary["confirmed_findings"],
        outputs={kind: str(path) for kind, path in paths.items()},
    )
    print(f"[pipeline] vulnerability report: {paths['html']}")


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
        "--run-dir",
        required=True,
        type=Path,
        help=f"run output directory; use {display_path(runs_artifact_dir())}/NAME to keep artifacts centralized",
    )
    parser.add_argument(
        "--semantic-task-budget",
        type=int,
        default=1_000,
        help="executions per semantic GuideTarget before failure decay",
    )
    parser.add_argument("--fuzz-iterations", type=int)
    parser.add_argument("--fuzz-timeout", type=int, help="stop fuzzing after N seconds and still generate a report")
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
    parser.add_argument("--report-llm-provider", choices=("mock", "openai-compatible"))
    parser.add_argument("--report-output-dir", type=Path)
    parser.add_argument("--report-replay-limit", type=int, default=200)
    parser.add_argument("--report-replay-timeout", type=float, default=2.0)
    parser.add_argument("--report-no-replay", action="store_true")
    parser.add_argument(
        "--report-minimize-findings",
        action="store_true",
        help="run deterministic minimization for confirmed finding reproducers during report generation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.st_file = args.st_file.resolve()
    if args.library is not None:
        args.library = args.library.resolve()
    if args.stubs is not None:
        args.stubs = args.stubs.resolve()
    if args.compatibility_manifest is not None:
        args.compatibility_manifest = args.compatibility_manifest.resolve()
    args.run_dir = args.run_dir.resolve()
    layout = create_layout(args.run_dir, args.report_only, target_build_dir(args.function))
    st_source = args.st_file.read_text(encoding="utf-8")
    constraints = source_constraints(st_source)
    store = CorpusStore(layout, constraints)

    if args.report_only:
        write_vulnerability_report(args, layout)
        return 0

    target_bin = build_targets(args, layout)
    stg_dir = layout.run_dir / "stg"
    plan = generate_semantic_task_plan(
        stg_dir / "stg-model.json",
        stg_dir / "stg-runtime-ids.json",
        layout.run_dir / "semantic-task-plan.json",
    )
    print(f"[pipeline] generated {len(plan['tasks'])} STG semantic tasks")
    ingest_initial_seeds(args.st_file, layout, store)
    if args.build_only:
        append_event(layout, "build_only_complete")
        write_vulnerability_report(args, layout)
        print(f"[pipeline] build-only complete: {layout.run_dir}")
        return 0

    try:
        launch_fuzzer(args, layout, target_bin)
    finally:
        write_vulnerability_report(args, layout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
