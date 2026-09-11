"""ICSFuzz adapter using the ICSQuartz CODESYS reproduction layout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from experiments.comparison.adapters.base import BaseAdapter, PreprocessingResult, SupportResult
from experiments.comparison.lib.hashing import sha256_text
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.process import (
    bind_mount,
    container_cpu_bound_command,
    run_with_budget,
)


class ICSFuzzAdapter(BaseAdapter):
    tool_name = "icsfuzz_full"
    supports_rng_seed = False
    per_execution_timeout_model = "codesys_scan_cycle"
    per_execution_timeout_ms = None

    def __init__(self, config):
        super().__init__(config)
        self.per_execution_timeout_ms = None

    def detect_support(self, target: Target) -> SupportResult:
        if self.config.icsquartz_root is None:
            return SupportResult.unsupported(
                "icsfuzz_missing_icsquartz_root", "ICSQuartz root was not found"
            )
        benchmark_dir = self.config.icsquartz_root / "benchmarks" / target.target_id
        required = [
            benchmark_dir / "codesys",
            benchmark_dir / "icsfuzz",
            benchmark_dir / "icsfuzz" / "harness.env",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return SupportResult.unsupported(
                "icsfuzz_missing_codesys_layout",
                "target has no complete ICSFuzz/CODESYS harness layout",
                missing=missing,
            )
        return SupportResult.supported("ICSQuartz benchmark has codesys/ and icsfuzz/harness.env")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists():
            shutil.rmtree(preprocess_dir)
        for child in ("logs", "target"):
            (preprocess_dir / child).mkdir(parents=True, exist_ok=True)
        start = time.monotonic_ns()
        assert self.config.icsquartz_root is not None
        benchmark_dir = self.config.icsquartz_root / "benchmarks" / target.target_id
        area_zero_file = self.config.icsquartz_root / ".config" / "codesys-area-zero"
        aslr_path = Path("/proc/sys/kernel/randomize_va_space")
        aslr_value = aslr_path.read_text(encoding="utf-8").strip() if aslr_path.is_file() else None
        if aslr_value != "0":
            end = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                end,
                failure_reason={
                    "code": "icsfuzz_aslr_enabled",
                    "message": f"ICSFuzz requires ASLR disabled; /proc/sys/kernel/randomize_va_space={aslr_value}",
                    "log_tail_path": None,
                },
            )
        if not area_zero_file.is_file():
            end = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                end,
                failure_reason={
                    "code": "icsfuzz_codesys_calibration_missing",
                    "message": f"missing CODESYS calibration file: {area_zero_file}",
                    "log_tail_path": None,
                },
            )
        if self.config.container_runtime is None:
            end = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                end,
                failure_reason={
                    "code": "container_runtime_unavailable",
                    "message": self.config.container_runtime_status,
                    "log_tail_path": None,
                },
            )
        harness_env = benchmark_dir / "icsfuzz" / "harness.env"
        shutil.copy2(harness_env, preprocess_dir / "target" / "harness.env")
        shutil.copytree(benchmark_dir / "codesys", preprocess_dir / "target" / "codesys")
        metadata = {
            "codesys_area_zero": area_zero_file.read_text(encoding="utf-8").strip(),
            "scan_cycle_ms": 35,
            "harness_env": harness_env.read_text(encoding="utf-8", errors="replace"),
            "image": self.baseline_image("icsfuzz"),
        }
        (preprocess_dir / "metadata.json").write_text(
            _json(metadata), encoding="utf-8"
        )
        end = time.monotonic_ns()
        return PreprocessingResult(
            "completed",
            preprocess_dir,
            start,
            end,
            artifacts={
                "codesys": str(preprocess_dir / "target" / "codesys"),
                "harness_env": str(preprocess_dir / "target" / "harness.env"),
            },
            metrics=metadata,
        )

    def materialize_trial(
        self, target: Target, preprocessing: PreprocessingResult, trial_id: int
    ) -> Path:
        run_dir = super().materialize_trial(target, preprocessing, trial_id)
        (run_dir / "tool-artifacts" / "wrapper.log").touch()
        (run_dir / "tool-artifacts" / "icsfuzz.log").touch()
        return run_dir

    def run(self, target: Target, run_dir: Path, preprocessing: PreprocessingResult, rng_seed: int, cpu: int) -> dict[str, Any]:
        metadata = {}
        metadata_path = preprocessing.preprocess_dir / "metadata.json"
        if metadata_path.is_file():
            import json

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        image = str(metadata.get("image") or self.baseline_image("icsfuzz"))
        codesys_dir = preprocessing.preprocess_dir / "target" / "codesys"
        container_name = _container_name(run_dir)
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
        command = [
            runtime,
            "run",
            "--rm",
            "--name",
            container_name,
            "--cap-add",
            "SYS_NICE",
            "--cap-add",
            "SYS_PTRACE",
            "-e",
            "CODESYS_LOG=/wrapper.log",
            "-e",
            f"CODESYS_AREA_ZERO={metadata.get('codesys_area_zero', '0x7ffff53da000')}",
            "-e",
            f"SEED={rng_seed}",
            "-v",
            bind_mount(
                runtime,
                codesys_dir / "Application",
                "/var/opt/codesys/PlcLogic/Application",
                readonly=True,
            ),
            "-v",
            bind_mount(
                runtime,
                codesys_dir / "SysFileMap.cfg",
                "/var/opt/codesys/SysFileMap.cfg",
                readonly=True,
            ),
            "-v",
            bind_mount(
                runtime,
                preprocessing.preprocess_dir / "target" / "harness.env",
                "/opt/icsfuzz/harness.env",
                readonly=True,
            ),
            "-v",
            bind_mount(runtime, run_dir / "tool-artifacts" / "wrapper.log", "/wrapper.log"),
            "-v",
            bind_mount(runtime, run_dir / "tool-artifacts" / "icsfuzz.log", "/icsfuzz.log"),
            image,
            "/bin/bash",
            "-lc",
            "ln -sf /usr/local/bin/start-codesys.sh /start.sh && cd /opt/icsfuzz && exec /usr/local/bin/start-icsfuzz.sh",
        ]
        if self.config.container_platform:
            command[2:2] = ["--platform", self.config.container_platform]
        command = container_cpu_bound_command(command, runtime, cpu)
        self.write_command(
            run_dir,
            command,
            {"SEED": str(rng_seed), "CODESYS_LOG": "/wrapper.log"},
            {
                "container_runtime": runtime,
                "rng_seed_note": "ICSFuzz ignores SEED; kept only as runner request trace",
            },
        )
        result = run_with_budget(
            command,
            cwd=self.config.semantist_root,
            env=os.environ.copy(),
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
        if result["started"]:
            subprocess.run(
                [runtime, "rm", "--force", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if not result["started"]:
            metrics["failure_reason"] = {
                "code": "icsfuzz_online_start_failed",
                "message": str(result.get("error")),
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        elif not result["timed_out"] and result["returncode"] not in (0, None):
            metrics["failure_reason"] = {
                "code": "icsfuzz_online_command_failed",
                "message": f"ICSFuzz {runtime} run exited with return code {result['returncode']}",
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        events: list[dict[str, Any]] = []
        wrapper_log = run_dir / "tool-artifacts" / "wrapper.log"
        if not wrapper_log.is_file():
            return events
        for index, line in enumerate(wrapper_log.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "Crash detected" not in line:
                continue
            event = {
                "candidate_id": f"{self.tool_name}:{target.target_id}:{trial_id}:crash:{sha256_text(line)}:{index}",
                "tool": self.tool_name,
                "target_id": target.target_id,
                "trial_id": trial_id,
                "discovery_phase": "fuzzing",
                "oracle": "target_signal",
                "observed_monotonic_ns": None,
                "observed_elapsed_seconds": None,
                "tool_reported_time_seconds": None,
                "tool_reported_unix_time_seconds": _line_timestamp(line),
                "execution_ordinal": None,
                "seed_sha256": None,
                "artifact_path": self.relative_artifact(wrapper_log),
            }
            events.append(event)
        return events

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        all_events = observed_events or self.scan_finding_events(target, run_dir)
        metrics["candidate_findings"] = all_events
        metrics["finding_found"] = bool(all_events)
        metrics["common_oracle_finding_found"] = bool(all_events)
        metrics["first_common_finding"] = all_events[0] if all_events else None
        metrics["crash_count_raw"] = len(all_events)
        exec_times = _icsfuzz_exec_times(run_dir / "tool-artifacts" / "icsfuzz.log")
        metrics["total_executions"] = len(exec_times)
        if len(exec_times) >= 2:
            elapsed = exec_times[-1] - exec_times[0]
            if elapsed > 0:
                metrics["execs_per_sec"] = len(exec_times) / elapsed
        metrics["corpus_count"] = len(exec_times)
        metrics["artifact_paths"] = {
            "wrapper_log": self.relative_artifact(run_dir / "tool-artifacts" / "wrapper.log"),
            "icsfuzz_log": self.relative_artifact(run_dir / "tool-artifacts" / "icsfuzz.log"),
        }
        metrics["tool_specific_metrics"] = {"execution_timeout_model": self.per_execution_timeout_model}
        return metrics


def _container_name(run_dir: Path) -> str:
    return "semantist-" + re.sub(r"[^A-Za-z0-9_.-]", "-", str(run_dir)[-80:])


def _line_timestamp(line: str) -> float | None:
    head = line.split(":", 1)[0].strip()
    try:
        return float(head)
    except ValueError:
        return None


def _icsfuzz_exec_times(path: Path) -> list[float]:
    if not path.is_file():
        return []
    times: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ";" not in line:
            continue
        try:
            times.append(float(line.split(";", 1)[0]))
        except ValueError:
            pass
    return times


def _status_from_process(result: dict[str, Any], finding_found: bool) -> str:
    if not result["started"]:
        return "online_start_failed"
    if result["timed_out"]:
        return "budget_completed"
    if result["returncode"] == 0:
        return "early_completed_with_finding" if finding_found else "early_completed_no_finding"
    return "online_crashed"


def _json(value: object) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True) + "\n"
