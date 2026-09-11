"""AFL++ baseline adapter."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from experiments.comparison.adapters.base import BaseAdapter, PreprocessingResult, SupportResult
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.process import (
    container_cpu_bound_command,
    container_run_command,
    run_capture,
    run_with_budget,
)


class AFLPlusPlusAdapter(BaseAdapter):
    tool_name = "aflplusplus_full"
    supports_rng_seed = True
    initial_seed_provider = "aflplusplus_adapter"
    initial_seed_format = "raw_bytes"
    initial_seed_policy = "single_zero_byte_seed"

    def detect_support(self, target: Target) -> SupportResult:
        base = super().detect_support(target)
        if base.status != "supported":
            return base
        return SupportResult.supported("RuSTy semantic-off target with AFL++ runtime")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists() and self.config.reuse_preprocessing:
            return PreprocessingResult("completed", preprocess_dir, 0, 0)
        if preprocess_dir.exists():
            shutil.rmtree(preprocess_dir)
        for child in ("base-seeds", "harness-seeds", "logs"):
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
            "SEMANTIST_SEMANTIC_INSTRUMENTATION": "off",
            "OUT_LL": str(preprocess_dir / "target.ll"),
            "OUT_OBJ": str(preprocess_dir / "target.o"),
            "OUT_BIN": str(preprocess_dir / "fuzz_target"),
            "HARNESS_OUT": str(preprocess_dir / "harness.c"),
            "GENERATED_SEED_DIR": str(preprocess_dir / "harness-seeds"),
            "CARGO_TARGET_DIR": "/opt/semantist/cargo-target",
        }
        if target.compatibility_manifest is not None:
            env["SEMANTIST_COMPATIBILITY_MANIFEST"] = str(target.compatibility_manifest)
        inner_commands = [
            ["./compiler/scripts/compile_st.sh", str(target.st_file), target.function],
            ["./compiler/scripts/build_target.sh", str(target.st_file), target.function],
        ]
        commands = [self._semantist_container_command(env, command) for command in inner_commands]
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
                        "code": "aflplusplus_preprocessing_command_failed",
                        "message": f"command failed: {' '.join(command)}",
                        "log_tail_path": self.relative_artifact(
                            preprocess_dir / "logs" / f"{index:02d}.stderr.log"
                        ),
                    },
                )
        (preprocess_dir / "base-seeds" / "seed").write_bytes(b"\x00")
        base_seeds = sorted(path for path in (preprocess_dir / "base-seeds").iterdir() if path.is_file())
        self.write_seed_manifest(
            preprocess_dir,
            base_seeds,
            note="Adapter-owned AFL++ byte corpus. The SemantiST harness generator output is isolated under harness-seeds/ and is not copied into AFL++ online inputs.",
        )
        missing = [
            path
            for path in (
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
                    "code": "aflplusplus_preprocessing_artifact_missing",
                    "message": "missing preprocessing artifacts: "
                    + ", ".join(str(path) for path in missing),
                    "log_tail_path": self.relative_artifact(preprocess_dir / "logs" / "02.stderr.log"),
                },
            )
        return PreprocessingResult(
            "completed",
            preprocess_dir,
            start,
            end,
            artifacts={
                "target": str(preprocess_dir / "fuzz_target"),
                "harness": str(preprocess_dir / "harness.c"),
                "seed_manifest": str(preprocess_dir / "seed-manifest.json"),
            },
        )

    def materialize_trial(
        self, target: Target, preprocessing: PreprocessingResult, trial_id: int
    ) -> Path:
        run_dir = super().materialize_trial(target, preprocessing, trial_id)
        inputs = run_dir / "inputs"
        _copy_dir(preprocessing.preprocess_dir / "base-seeds", inputs)
        shutil.copy2(preprocessing.preprocess_dir / "fuzz_target", run_dir / "tool-artifacts" / "fuzz_target")
        shutil.copy2(preprocessing.preprocess_dir / "harness.c", run_dir / "tool-artifacts" / "harness.c")
        shutil.copy2(preprocessing.preprocess_dir / "seed-manifest.json", run_dir / "seed-manifest.json")
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
        env = {
            "AFL_NO_UI": "1",
            "AFL_NO_AFFINITY": "1",
            "AFL_SKIP_CPUFREQ": "1",
            "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
        }
        image = self.baseline_image("aflplusplus")
        command = container_run_command(
            runtime,
            image,
            mounts=[
                (run_dir / "inputs", "/inputs", True),
                (run_dir / "tool-artifacts", "/out", False),
            ],
            env=env,
            platform=self.config.container_platform,
            command=[
                "afl-fuzz",
                "-i",
                "/inputs",
                "-o",
                "/out/afl",
                "-s",
                str(rng_seed),
                "-t",
                f"{self.config.per_execution_timeout_ms}+",
                "--",
                "/out/fuzz_target",
            ],
        )
        command = container_cpu_bound_command(command, runtime, cpu)
        self.write_command(
            run_dir,
            command,
            env,
            {"assigned_cpu": cpu, "container_runtime": runtime, "image": image},
        )
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
                "code": "aflplusplus_online_start_failed",
                "message": str(result.get("error")),
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        elif not result["timed_out"] and result["returncode"] not in (0, None):
            metrics["failure_reason"] = {
                "code": "aflplusplus_online_command_failed",
                "message": f"AFL++ exited with return code {result['returncode']}",
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        events: list[dict[str, Any]] = []
        base = run_dir / "tool-artifacts" / "afl" / "default"
        for directory, oracle in ((base / "crashes", "target_signal"), (base / "hangs", "testcase_timeout")):
            for path in sorted(directory.glob("id:*")):
                if path.name.startswith("README"):
                    continue
                elapsed, executions = _parse_afl_name(path.name)
                events.append(
                    self.candidate_from_path(
                        path=path,
                        target=target,
                        trial_id=trial_id,
                        oracle=oracle,
                        execution_ordinal=executions,
                        elapsed_seconds=elapsed,
                    )
                )
        return events

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        stats = self.parse_afl_stats(run_dir / "tool-artifacts" / "afl" / "default" / "fuzzer_stats")
        metrics.update({key: value for key, value in stats.items() if value is not None})
        all_events = observed_events or self.scan_finding_events(target, run_dir)
        common_events = [event for event in all_events if event.get("oracle") != "testcase_timeout"]
        metrics["candidate_findings"] = all_events
        metrics["finding_found"] = bool(all_events)
        metrics["common_oracle_finding_found"] = bool(common_events)
        metrics["first_common_finding"] = common_events[0] if common_events else None
        metrics["crash_count_raw"] = len(common_events)
        metrics["timeout_count_raw"] = len(all_events) - len(common_events)
        queue_dir = run_dir / "tool-artifacts" / "afl" / "default" / "queue"
        metrics["corpus_count"] = sum(1 for path in queue_dir.glob("id:*") if path.is_file())
        metrics["artifact_paths"] = {
            "afl_output": self.relative_artifact(run_dir / "tool-artifacts" / "afl"),
            "inputs": self.relative_artifact(run_dir / "inputs"),
            "seed_manifest": self.relative_artifact(run_dir / "seed-manifest.json"),
        }
        return metrics

    def _semantist_container_command(self, env: dict[str, str], command: list[str]) -> list[str]:
        runtime = self.config.container_runtime
        if runtime is None:
            raise RuntimeError(self.config.container_runtime_status)
        return container_run_command(
            runtime,
            self.config.semantist_image,
            mounts=[
                (self.config.semantist_root, str(self.config.semantist_root), True),
                (self.config.artifact_root, str(self.config.artifact_root), False),
            ],
            env=env,
            workdir=str(self.config.semantist_root),
            platform=self.config.container_platform,
            command=command,
        )


def _copy_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


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
        "SEMANTIST_COMPATIBILITY_MANIFEST",
    }
    return {key: value for key, value in env.items() if key in keep}


def _parse_afl_name(name: str) -> tuple[float | None, int | None]:
    time_match = re.search(r"(?:^|,)time:(\d+)", name)
    execs_match = re.search(r"(?:^|,)execs:(\d+)", name)
    elapsed = int(time_match.group(1)) / 1000 if time_match else None
    executions = int(execs_match.group(1)) if execs_match else None
    return elapsed, executions


def _status_from_process(result: dict[str, Any], finding_found: bool) -> str:
    if not result["started"]:
        return "online_start_failed"
    if result["timed_out"]:
        return "budget_completed"
    if result["returncode"] == 0:
        return "early_completed_with_finding" if finding_found else "early_completed_no_finding"
    return "online_crashed"
