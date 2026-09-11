"""SemantiST full adapter."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from experiments.comparison.adapters.base import (
    AdapterError,
    BaseAdapter,
    PreprocessingResult,
    SupportResult,
)
from experiments.comparison.lib.hashing import sha256_file
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.process import (
    container_cpu_bound_command,
    container_run_command,
    run_capture,
    run_with_budget,
)


class SemantiSTAdapter(BaseAdapter):
    tool_name = "semantist_full"
    supports_rng_seed = True
    initial_seed_provider = "semantist_harness_generator"
    initial_seed_format = "semantist_structured_seed"
    initial_seed_policy = "adapter_generated_default_seed"

    def detect_support(self, target: Target) -> SupportResult:
        base = super().detect_support(target)
        if base.status != "supported":
            return base
        return SupportResult.supported("SemantiST native ST compiler/harness path")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists() and self.config.reuse_preprocessing:
            return _reuse_preprocessing(preprocess_dir)
        if preprocess_dir.exists():
            shutil.rmtree(preprocess_dir)
        for child in ("stg", "base-seeds", "logs"):
            (preprocess_dir / child).mkdir(parents=True, exist_ok=True)

        runtime = self.config.container_runtime
        if runtime is None:
            now = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                now,
                now,
                failure_reason={
                    "code": "container_runtime_unavailable",
                    "message": self.config.container_runtime_status,
                    "log_tail_path": None,
                },
            )

        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.config.semantist_root),
            "SEMANTIST_SEMANTIC_INSTRUMENTATION": "ir",
            "SEMANTIST_STG_DIR": str(preprocess_dir / "stg"),
            "OUT_LL": str(preprocess_dir / "target.ll"),
            "OUT_OBJ": str(preprocess_dir / "target.o"),
            "OUT_BIN": str(preprocess_dir / "fuzz_target"),
            "HARNESS_OUT": str(preprocess_dir / "harness.c"),
            "GENERATED_SEED_DIR": str(preprocess_dir / "base-seeds"),
            "CARGO_TARGET_DIR": "/opt/semantist/cargo-target",
        }
        if target.compatibility_manifest is not None:
            env["SEMANTIST_COMPATIBILITY_MANIFEST"] = str(target.compatibility_manifest)
        inner_commands = [
            ["./compiler/scripts/compile_st.sh", str(target.st_file), target.function],
            ["./compiler/scripts/build_target.sh", str(target.st_file), target.function],
            [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "from fuzzer.semantic.planning import generate_semantic_task_plan; "
                    f"generate_semantic_task_plan(Path({str(preprocess_dir / 'stg' / 'stg-model.json')!r}), "
                    f"Path({str(preprocess_dir / 'stg' / 'stg-runtime-ids.json')!r}), "
                    f"Path({str(preprocess_dir / 'semantic-task-plan.json')!r}))"
                ),
            ],
        ]
        commands = [self._container_command(env, command) for command in inner_commands]
        self.write_preprocessing_command(
            preprocess_dir,
            [{"command": command, "environment": _filtered_env(env)} for command in commands],
        )
        start = 0
        end = 0
        for index, command in enumerate(commands, 1):
            result = run_capture(
                command,
                cwd=self.config.semantist_root,
                env=None,
                stdout_path=preprocess_dir / "logs" / f"{index:02d}.stdout.log",
                stderr_path=preprocess_dir / "logs" / f"{index:02d}.stderr.log",
            )
            start = start or int(result["start_monotonic_ns"])
            end = int(result["end_monotonic_ns"])
            if result.get("returncode") != 0:
                return PreprocessingResult(
                    "failed",
                    preprocess_dir,
                    start,
                    end,
                    failure_reason={
                        "code": "semantist_preprocessing_command_failed",
                        "message": f"command failed: {' '.join(command)}",
                        "log_tail_path": self.relative_artifact(
                            preprocess_dir / "logs" / f"{index:02d}.stderr.log"
                        ),
                    },
                )

        base_seeds = sorted(path for path in (preprocess_dir / "base-seeds").iterdir() if path.is_file())
        self.write_seed_manifest(
            preprocess_dir,
            base_seeds,
            note="Generated by the SemantiST C harness generator during target build; STG semantic initial generation happens after online t0.",
        )
        missing = [
            path
            for path in (
                preprocess_dir / "stg" / "stg-model.json",
                preprocess_dir / "stg" / "stg-runtime-ids.json",
                preprocess_dir / "semantic-task-plan.json",
                preprocess_dir / "fuzz_target",
                preprocess_dir / "harness.c",
                preprocess_dir / "seed-manifest.json",
            )
            if not path.exists()
        ]
        if missing:
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                end,
                failure_reason={
                    "code": "semantist_preprocessing_artifact_missing",
                    "message": "missing preprocessing artifacts: "
                    + ", ".join(str(path) for path in missing),
                    "log_tail_path": self.relative_artifact(preprocess_dir / "logs" / "02.stderr.log"),
                },
            )
        artifacts = {
            "target": str(preprocess_dir / "fuzz_target"),
            "harness": str(preprocess_dir / "harness.c"),
            "stg_model": str(preprocess_dir / "stg" / "stg-model.json"),
            "semantic_task_plan": str(preprocess_dir / "semantic-task-plan.json"),
            "seed_manifest": str(preprocess_dir / "seed-manifest.json"),
        }
        return PreprocessingResult("completed", preprocess_dir, start, end, artifacts=artifacts)

    def materialize_trial(
        self, target: Target, preprocessing: PreprocessingResult, trial_id: int
    ) -> Path:
        run_dir = super().materialize_trial(target, preprocessing, trial_id)
        seed_dir = run_dir / "unified-corpus" / "seeds"
        runtime_dir = run_dir / "unified-corpus" / "runtime"
        target_dir = run_dir / "target-corpus"
        for child in (seed_dir, runtime_dir, target_dir):
            child.mkdir(parents=True, exist_ok=True)
        _copy_dir(preprocessing.preprocess_dir / "base-seeds", seed_dir)
        shutil.copy2(preprocessing.preprocess_dir / "fuzz_target", run_dir / "tool-artifacts" / "fuzz_target")
        shutil.copy2(preprocessing.preprocess_dir / "harness.c", run_dir / "tool-artifacts" / "harness.c")
        _copy_dir(preprocessing.preprocess_dir / "stg", run_dir / "stg")
        shutil.copy2(preprocessing.preprocess_dir / "semantic-task-plan.json", run_dir / "semantic-task-plan.json")
        shutil.copy2(preprocessing.preprocess_dir / "seed-manifest.json", run_dir / "seed-manifest.json")
        if (preprocessing.preprocess_dir / "generated-seeds").exists():
            raise AdapterError(
                "semantist_generated_seed_before_t0",
                f"unexpected generated seeds in {preprocessing.preprocess_dir / 'generated-seeds'}",
            )
        return run_dir

    def run(self, target: Target, run_dir: Path, preprocessing: PreprocessingResult, rng_seed: int, cpu: int) -> dict[str, Any]:
        runtime = self.config.container_runtime
        if runtime is None:
            return {
                "run_status": "online_start_failed",
                "t0_monotonic_ns": None,
                "online_elapsed_seconds": 0.0,
                "failure_reason": {
                    "code": "container_runtime_unavailable",
                    "message": self.config.container_runtime_status,
                    "log_tail_path": None,
                },
                **self.collect_metrics(target, run_dir, []),
            }
        target_bin = run_dir / "tool-artifacts" / "fuzz_target"
        env = {
            "SEMANTIST_SEMANTIC_TRACE_FILE": str(run_dir / "semantic-coverage.jsonl"),
            "SEMANTIST_SEMANTIC_RUNTIME_IDS": str(run_dir / "stg" / "stg-runtime-ids.json"),
            "SEMANTIST_FB_MAX_CYCLES": str(self.config.semantist_fb_max_cycles),
            "SEMANTIST_FB_STALE_THRESHOLD": str(self.config.semantist_fb_stale_threshold),
            "SEMANTIST_RNG_SEED": str(rng_seed),
            "SEMANTIST_LLM_PROVIDER": "mock",
        }
        if self.config.semantist_fb_state_trace:
            env["SEMANTIST_FB_STATE_TRACE"] = "1"
            env["SEMANTIST_FB_STATE_TRACE_FILE"] = str(run_dir / "tool-artifacts" / "fb-state-trace.log")
        command = self._container_command(
            env,
            [
                "/opt/semantist/cargo-target/release/semantist",
                "--target",
                str(target_bin),
                "--function",
                target.function,
                "--run-dir",
                str(run_dir),
                "--semantic-task-budget",
                str(self.config.semantic_task_budget),
                "--semantic-task-plan",
                str(run_dir / "semantic-task-plan.json"),
                "--semantic-model",
                str(run_dir / "stg" / "stg-model.json"),
                "--timeout-ms",
                str(self.config.per_execution_timeout_ms),
                "--seed",
                str(rng_seed),
            ],
        )
        command = container_cpu_bound_command(command, runtime, cpu)
        self.write_command(run_dir, command, _filtered_env(env), {"assigned_cpu": cpu})
        result = run_with_budget(
            command,
            cwd=self.config.semantist_root,
            env=None,
            stdout_path=run_dir / "stdout.log",
            stderr_path=run_dir / "stderr.log",
            budget_seconds=self.config.online_budget_seconds,
            poll_interval_ms=self.config.observer_poll_interval_ms,
            scan_findings=lambda: self.scan_finding_events(target, run_dir),
        )
        metrics = self.collect_metrics(target, run_dir, result["events"])
        metrics.update(
            {
                "t0_monotonic_ns": result["t0_monotonic_ns"],
                "online_elapsed_seconds": result["elapsed_seconds"],
                "run_status": _status_from_process(result, metrics["finding_found"]),
            }
        )
        if not result["started"]:
            metrics["failure_reason"] = {
                "code": "semantist_online_start_failed",
                "message": str(result.get("error")),
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        elif not result["timed_out"] and result["returncode"] not in (0, None):
            metrics["failure_reason"] = {
                "code": "semantist_online_command_failed",
                "message": f"SemantiST exited with return code {result['returncode']}",
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        for json_path in sorted((run_dir / "findings").rglob("*.json")):
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            oracle = _oracle_from_kind(str(raw.get("kind") or raw.get("finding_kind") or "target_signal"))
            event = self.candidate_from_path(
                path=json_path,
                target=target,
                trial_id=trial_id,
                oracle=oracle,
                discovery_phase=_phase_from_source(str(raw.get("source") or raw.get("finding_source") or "fuzzing")),
                execution_ordinal=_safe_int(raw.get("executions")),
            )
            reproducer = raw.get("reproducer")
            if isinstance(reproducer, str) and Path(reproducer).is_file():
                reproducer_path = Path(reproducer)
                event["seed_sha256"] = sha256_file(reproducer_path)
                event["reproducer_artifact_path"] = self.relative_artifact(reproducer_path)
            events.append(event)
        return events

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        queue = run_dir / "unified-corpus" / "runtime"
        metrics["corpus_count"] = sum(1 for path in queue.rglob("*") if path.is_file())
        all_events = observed_events or self.scan_finding_events(target, run_dir)
        common_events = [event for event in all_events if event.get("oracle") != "semantic_objective"]
        semantic_events = [event for event in all_events if event.get("oracle") == "semantic_objective"]
        metrics["candidate_findings"] = all_events
        metrics["finding_found"] = bool(all_events)
        metrics["common_oracle_finding_found"] = bool(common_events)
        metrics["semantic_objective_found"] = bool(semantic_events)
        metrics["first_common_finding"] = common_events[0] if common_events else None
        metrics["first_semantic_objective"] = semantic_events[0] if semantic_events else None
        metrics["crash_count_raw"] = len(common_events)
        metrics["total_executions"] = _max_executions(run_dir / "events.jsonl")
        elapsed = _last_event_elapsed(run_dir / "events.jsonl")
        if metrics["total_executions"] is not None and elapsed:
            metrics["execs_per_sec"] = metrics["total_executions"] / elapsed
        metrics["artifact_paths"] = {
            "semantic_coverage": self.relative_artifact(run_dir / "semantic-coverage.jsonl"),
            "events": self.relative_artifact(run_dir / "events.jsonl"),
            "findings": self.relative_artifact(run_dir / "findings"),
            "seed_manifest": self.relative_artifact(run_dir / "seed-manifest.json"),
        }
        metrics["tool_specific_metrics"] = _semantic_coverage_metrics(run_dir)
        return metrics

    def _container_command(self, env: dict[str, str], command: list[str]) -> list[str]:
        runtime = self.config.container_runtime
        if runtime is None:
            raise AdapterError("container_runtime_unavailable", self.config.container_runtime_status)
        mounts = [
            (self.config.semantist_root, str(self.config.semantist_root), True),
            (self.config.artifact_root, str(self.config.artifact_root), False),
        ]
        return container_run_command(
            runtime,
            self.config.semantist_image,
            mounts=mounts,
            env=env,
            workdir=str(self.config.semantist_root),
            platform=self.config.container_platform,
            command=command,
        )


def _copy_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _reuse_preprocessing(preprocess_dir: Path) -> PreprocessingResult:
    now = 0
    return PreprocessingResult("completed", preprocess_dir, now, now)


def _filtered_env(env: dict[str, str]) -> dict[str, str]:
    keep = {
        "CARGO_TARGET_DIR",
        "GENERATED_SEED_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "OUT_LL",
        "OUT_OBJ",
        "OUT_BIN",
        "HARNESS_OUT",
        "SEMANTIST_SEMANTIC_INSTRUMENTATION",
        "SEMANTIST_STG_DIR",
        "SEMANTIST_SEMANTIC_RUNTIME_IDS",
        "SEMANTIST_SEMANTIC_TRACE_FILE",
        "SEMANTIST_FB_MAX_CYCLES",
        "SEMANTIST_FB_STALE_THRESHOLD",
        "SEMANTIST_FB_STATE_TRACE",
        "SEMANTIST_FB_STATE_TRACE_FILE",
        "SEMANTIST_COMPATIBILITY_MANIFEST",
        "SEMANTIST_RNG_SEED",
        "SEMANTIST_LLM_PROVIDER",
    }
    return {key: value for key, value in env.items() if key in keep}


def _status_from_process(result: dict[str, Any], finding_found: bool) -> str:
    if not result["started"]:
        return "online_start_failed"
    if result["timed_out"]:
        return "budget_completed"
    if result["returncode"] == 0:
        return "early_completed_with_finding" if finding_found else "early_completed_no_finding"
    return "online_crashed"


def _oracle_from_kind(kind: str) -> str:
    lower = kind.lower()
    if "semantic" in lower:
        return "semantic_objective"
    if "address" in lower or "asan" in lower:
        return "asan"
    if "undefined" in lower or "ubsan" in lower:
        return "ubsan"
    if "oom" in lower:
        return "target_oom"
    if "timeout" in lower:
        return "testcase_timeout"
    return "target_signal"


def _phase_from_source(source: str) -> str:
    lower = source.lower()
    if "initial" in lower:
        return "initial_ingress"
    if "semantic" in lower:
        return "semantic_generation"
    return "fuzzing"


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _max_executions(events_path: Path) -> int | None:
    if not events_path.is_file():
        return None
    maximum: int | None = None
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        executions = _safe_int(event.get("executions"))
        if executions is not None:
            maximum = max(maximum or 0, executions)
    return maximum


def _last_event_elapsed(events_path: Path) -> float | None:
    if not events_path.is_file():
        return None
    first: float | None = None
    last: float | None = None
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = event.get("unix_time_seconds")
        try:
            value = float(ts)
        except (TypeError, ValueError):
            continue
        first = value if first is None else first
        last = value
    if first is None or last is None:
        return None
    elapsed = last - first
    return elapsed if elapsed > 0 else None


def _semantic_coverage_metrics(run_dir: Path) -> dict[str, Any]:
    coverage_path = run_dir / "semantic-coverage.jsonl"
    if not coverage_path.is_file():
        return {}
    hit_ids: set[str] = set()
    state_signatures: set[str] = set()
    cycle_ids: set[int] = set()
    for line in coverage_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in ("target_id", "stable_target_id", "runtime_id"):
            if key in event:
                hit_ids.add(str(event[key]))
        if "state_signature" in event:
            state_signatures.add(str(event["state_signature"]))
        if "cycle_id" in event:
            try:
                cycle_ids.add(int(event["cycle_id"]))
            except (TypeError, ValueError):
                pass
    return {
        "semantic_hits_total": len(hit_ids),
        "state_signature_total": len(state_signatures),
        "cycle_ids_observed_total": len(cycle_ids),
    }
