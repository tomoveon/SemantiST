"""ICSQuartz baseline adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from experiments.comparison.adapters.base import BaseAdapter, PreprocessingResult, SupportResult
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.process import container_cpu_bound_command, container_run_command, run_capture


ASAN_ALT_TARGETS = {
    "icsfuzz_bf_mcpy_1",
    "icsfuzz_bf_mcpy_6",
    "icsfuzz_bf_mcpy_8",
    "icsfuzz_bf_mcpy_12",
    "icsfuzz_bf_mmove_1",
    "icsfuzz_bf_mmove_4",
    "icsfuzz_bf_mmove_7",
    "icsfuzz_bf_mmove_12",
    "icsfuzz_bf_mset_1",
    "icsfuzz_bf_mset_3",
    "icsfuzz_bf_mset_5",
}


class ICSQuartzAdapter(BaseAdapter):
    tool_name = "icsquartz_full"
    supports_rng_seed = True
    initial_seed_provider = "icsquartz_native_initial_generator"
    initial_seed_format = "raw_bytes"
    initial_seed_policy = "tool_internal_default_plus_pattern_inputs"

    def detect_support(self, target: Target) -> SupportResult:
        if self.config.icsquartz_root is None:
            return SupportResult.unsupported(
                "icsquartz_missing_root", "ICSQuartz root was not found"
            )
        benchmark_dir = self.config.icsquartz_root / "benchmarks" / target.target_id
        required = [
            benchmark_dir / "src",
            benchmark_dir / "icsquartz" / "harness.c",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return SupportResult.unsupported(
                "icsquartz_missing_benchmark_layout",
                "target has no upstream ICSQuartz src/ and icsquartz/harness.c layout",
                missing=missing,
            )
        return SupportResult.supported("ICSQuartz benchmark has src/ and icsquartz/harness.c")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists() and self.config.reuse_preprocessing:
            return PreprocessingResult("completed", preprocess_dir, 0, 0)
        if preprocess_dir.exists():
            shutil.rmtree(preprocess_dir)
        (preprocess_dir / "logs").mkdir(parents=True, exist_ok=True)
        start = time.monotonic_ns()
        runtime = self.config.container_runtime
        if runtime is None:
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
        assert self.config.icsquartz_root is not None
        compiler = _compiler_version(target)
        benchmark_dir = self.config.icsquartz_root / "benchmarks" / target.target_id
        compiler_image = _image_tag("semantist-exp-plc-compiler", compiler, target.target_id)
        fuzzer_image = _image_tag("semantist-exp-icsquartz", compiler, target.target_id)
        compile_cmd = [
            runtime,
            "build",
            "-t",
            compiler_image,
            "--file",
            str(self.config.icsquartz_root / "compiler" / f"{compiler}.Dockerfile"),
            "--build-context",
            f"fuzztarget={benchmark_dir}",
            str(self.config.icsquartz_root / "compiler"),
        ]
        scan_cycle = "1" if target.suite == "icsquartz_scan_cycle" else "0"
        fuzzer_cmd = [
            runtime,
            "build",
            "-t",
            fuzzer_image,
            "--build-context",
            f"icsbuild=docker-image://{compiler_image}",
            "--build-context",
            f"fuzztarget={benchmark_dir}",
            "--build-arg",
            f"SCAN_CYCLE={scan_cycle}",
            "--build-arg",
            f"ASAN_ALT={1 if target.target_id in ASAN_ALT_TARGETS else 0}",
            str(self.config.icsquartz_root / "fuzzers" / "icsquartz"),
        ]
        if self.config.container_platform:
            compile_cmd[2:2] = ["--platform", self.config.container_platform]
            fuzzer_cmd[2:2] = ["--platform", self.config.container_platform]
        self.write_preprocessing_command(
            preprocess_dir,
            [
                {"command": compile_cmd, "environment": {}},
                {"command": fuzzer_cmd, "environment": {}},
            ],
        )
        for index, command in enumerate((compile_cmd, fuzzer_cmd), 1):
            result = run_capture(
                command,
                cwd=self.config.icsquartz_root,
                env=os.environ.copy(),
                stdout_path=preprocess_dir / "logs" / f"{index:02d}.stdout.log",
                stderr_path=preprocess_dir / "logs" / f"{index:02d}.stderr.log",
            )
            if result.get("returncode") != 0:
                return PreprocessingResult(
                    "failed",
                    preprocess_dir,
                    start,
                    int(result["end_monotonic_ns"]),
                    failure_reason={
                        "code": "icsquartz_container_build_failed",
                        "message": f"{runtime} build failed: {' '.join(command)}",
                        "log_tail_path": self.relative_artifact(
                            preprocess_dir / "logs" / f"{index:02d}.stderr.log"
                        ),
                    },
                )
        end = time.monotonic_ns()
        metadata = {
            "compiler": compiler,
            "compiler_image": compiler_image,
            "fuzzer_image": fuzzer_image,
            "scan_cycle": scan_cycle == "1",
            "asan_alt": target.target_id in ASAN_ALT_TARGETS,
            "container_runtime": runtime,
        }
        (preprocess_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return PreprocessingResult(
            "completed",
            preprocess_dir,
            start,
            end,
            artifacts={"image": fuzzer_image},
            metrics=metadata,
        )

    def run(self, target: Target, run_dir: Path, preprocessing: PreprocessingResult, rng_seed: int, cpu: int) -> dict[str, Any]:
        metadata = json.loads((preprocessing.preprocess_dir / "metadata.json").read_text(encoding="utf-8"))
        image = str(metadata["fuzzer_image"])
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
        tool_artifacts = run_dir / "tool-artifacts"
        corpus_dir = tool_artifacts / "corpus"
        crashes_dir = tool_artifacts / "crashes"
        stats_file = tool_artifacts / "fuzzer_stats.json"
        fuzzer_log = tool_artifacts / "fuzzer_log"
        for path in (corpus_dir, crashes_dir):
            path.mkdir(parents=True, exist_ok=True)
        for path in (stats_file, fuzzer_log):
            path.touch(exist_ok=True)

        env_args = {
            "ASAN_OPTIONS": _asan_options(bool(metadata.get("asan_alt"))),
        }
        icsquartz_args = {
            "seed": str(rng_seed),
            "cores": "none",
            "scan_cycle_max": "10000" if metadata.get("scan_cycle") else "2",
            "mutator_pow": "4",
            "min_input_generation": "128",
            "timeout_ms": str(self.config.per_execution_timeout_ms),
        }
        inner_command = [
            "/out/icsfuzz-demo",
            "--seed",
            icsquartz_args["seed"],
            "--cores",
            icsquartz_args["cores"],
            "--mutator-pow",
            icsquartz_args["mutator_pow"],
            "--fuzzer-log",
            "/out/fuzzer_log",
            "--scan-cycle-max",
            icsquartz_args["scan_cycle_max"],
            "--min-input-generation",
            icsquartz_args["min_input_generation"],
            "--timeout",
            icsquartz_args["timeout_ms"],
            "--corpus",
            "/out/corpus",
            "--crashes",
            "/out/crashes",
            "--fuzzer-stats",
            "/out/fuzzer_stats.json",
        ]
        if metadata.get("scan_cycle"):
            inner_command.extend(["--state-resets", "--dynamic-scan-cycle"])
        _remove_container_quiet(runtime, container_name)
        command = container_run_command(
            runtime,
            image,
            mounts=[
                (corpus_dir, "/out/corpus", False),
                (crashes_dir, "/out/crashes", False),
                (stats_file, "/out/fuzzer_stats.json", False),
                (fuzzer_log, "/out/fuzzer_log", False),
            ],
            env=env_args,
            platform=self.config.container_platform,
            name=container_name,
            detach=True,
            remove=False,
            cap_add=["SYS_NICE"],
            command=inner_command,
        )
        command = container_cpu_bound_command(command, runtime, cpu)
        self.write_command(
            run_dir,
            command,
            env_args,
            {
                "assigned_cpu": cpu,
                "container_runtime": runtime,
                "icsquartz_args": icsquartz_args,
                "initial_seed_note": "ICSQuartz constructs its own initial BytesInput corpus at startup.",
            },
        )
        t0 = time.monotonic_ns()
        start = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (run_dir / "stdout.log").write_text(start.stdout, encoding="utf-8")
        (run_dir / "stderr.log").write_text(start.stderr, encoding="utf-8")
        if start.returncode != 0:
            return {
                "run_status": "online_start_failed",
                "t0_monotonic_ns": t0,
                "online_elapsed_seconds": (time.monotonic_ns() - t0) / 1_000_000_000,
                "failure_reason": {
                    "code": "icsquartz_container_start_failed",
                    "message": start.stderr.strip() or start.stdout.strip(),
                    "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
                },
                **self.collect_metrics(target, run_dir, []),
            }
        deadline = t0 + self.config.online_budget_seconds * 1_000_000_000
        timed_out = True
        events: list[dict[str, Any]] = []
        seen: set[str] = set()

        def observe_findings() -> None:
            for event in self.scan_finding_events(target, run_dir):
                candidate_id = str(event.get("candidate_id"))
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                observed = dict(event)
                observed["observed_monotonic_ns"] = time.monotonic_ns()
                observed["observed_elapsed_seconds"] = (
                    observed["observed_monotonic_ns"] - t0
                ) / 1_000_000_000
                events.append(observed)

        while time.monotonic_ns() < deadline:
            observe_findings()
            if not _container_running(runtime, container_name):
                timed_out = False
                break
            time.sleep(self.config.observer_poll_interval_ms / 1000)
        observe_findings()
        if timed_out:
            subprocess.run(
                [runtime, "stop", "--time", "5", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        exit_code = _container_exit_code(runtime, container_name)
        _write_container_logs(runtime, container_name, run_dir / "container.stdout.log", run_dir / "container.stderr.log")
        subprocess.run(
            [runtime, "rm", "--force", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        elapsed = (time.monotonic_ns() - t0) / 1_000_000_000
        metrics = self.collect_metrics(target, run_dir, events)
        metrics.update(
            {
                "t0_monotonic_ns": t0,
                "online_elapsed_seconds": elapsed,
                "run_status": "budget_completed" if timed_out else (
                    "early_completed_with_finding" if metrics["finding_found"] else "early_completed_no_finding"
                ),
            }
        )
        if not timed_out and exit_code not in (0, None):
            metrics["run_status"] = "online_crashed"
            metrics["failure_reason"] = {
                "code": "icsquartz_container_exited_nonzero",
                "message": f"ICSQuartz container exited with code {exit_code}",
                "log_tail_path": self.relative_artifact(run_dir / "container.stderr.log"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        events: list[dict[str, Any]] = []
        crash_dir = run_dir / "tool-artifacts" / "crashes"
        for metadata_path in sorted(crash_dir.glob(".*.metadata")):
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
            executions = _safe_int(raw.get("executions"))
            elapsed = None
            execs_per_sec = self._latest_execs_per_sec(run_dir)
            if executions is not None and execs_per_sec:
                elapsed = executions / execs_per_sec
            seed_path = _input_for_metadata(metadata_path)
            event_path = seed_path if seed_path is not None else metadata_path
            event = self.candidate_from_path(
                path=event_path,
                target=target,
                trial_id=trial_id,
                oracle="target_signal",
                execution_ordinal=executions,
                elapsed_seconds=elapsed,
            )
            if seed_path is not None:
                event["metadata_artifact_path"] = self.relative_artifact(metadata_path)
            else:
                event["conversion_note"] = "LibAFL metadata was present but the paired crash input was not found"
            events.append(
                event
            )
        return events

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        latest = _last_json_line(run_dir / "tool-artifacts" / "fuzzer_stats.json")
        if latest:
            metrics["execs_per_sec"] = _safe_float(latest.get("exec_sec"))
            metrics["total_executions"] = _safe_int(latest.get("executions"))
            try:
                metrics["total_executions"] = max(
                    metrics["total_executions"] or 0,
                    int(latest["client_stats"][1]["user_monitor"]["executions_"]["value"]["Number"]),
                )
                metrics["tool_specific_metrics"]["state_resets"] = int(
                    latest["client_stats"][1]["user_monitor"]["stale_state_"]["value"]["Number"]
                )
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            metrics["tool_specific_metrics"]["icsquartz_stats"] = latest
        events = observed_events or self.scan_finding_events(target, run_dir)
        metrics["candidate_findings"] = events
        metrics["finding_found"] = bool(events)
        metrics["common_oracle_finding_found"] = bool(events)
        metrics["first_common_finding"] = events[0] if events else None
        metrics["crash_count_raw"] = len(events)
        corpus_dir = run_dir / "tool-artifacts" / "corpus"
        metrics["corpus_count"] = sum(1 for path in corpus_dir.rglob("*") if path.is_file())
        metrics["artifact_paths"] = {
            "fuzzer_stats": self.relative_artifact(run_dir / "tool-artifacts" / "fuzzer_stats.json"),
            "crashes": self.relative_artifact(run_dir / "tool-artifacts" / "crashes"),
            "corpus": self.relative_artifact(run_dir / "tool-artifacts" / "corpus"),
            "fuzzer_log": self.relative_artifact(run_dir / "tool-artifacts" / "fuzzer_log"),
        }
        return metrics

    def _latest_execs_per_sec(self, run_dir: Path) -> float | None:
        latest = _last_json_line(run_dir / "tool-artifacts" / "fuzzer_stats.json")
        return _safe_float(latest.get("exec_sec")) if latest else None


def _compiler_version(target: Target) -> str:
    return "bug" if target.target_id.startswith("oscat_") else "latest"


def _image_tag(prefix: str, compiler: str, target_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", target_id).lower()
    return f"{prefix}-{compiler}:{safe}"


def _container_name(run_dir: Path) -> str:
    return "semantist-" + re.sub(r"[^A-Za-z0-9_.-]", "-", str(run_dir)[-80:])


def _container_running(runtime: str, name: str) -> bool:
    result = subprocess.run(
        [runtime, "inspect", "-f", "{{.State.Running}}", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _container_exit_code(runtime: str, name: str) -> int | None:
    result = subprocess.run(
        [runtime, "inspect", "-f", "{{.State.ExitCode}}", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _remove_container_quiet(runtime: str, name: str) -> None:
    subprocess.run(
        [runtime, "rm", "--force", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_container_logs(runtime: str, name: str, stdout_path: Path, stderr_path: Path) -> None:
    result = subprocess.run(
        [runtime, "logs", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")


def _input_for_metadata(metadata_path: Path) -> Path | None:
    name = metadata_path.name
    if not (name.startswith(".") and name.endswith(".metadata")):
        return None
    candidate = metadata_path.with_name(name[1 : -len(".metadata")])
    return candidate if candidate.is_file() else None


def _asan_options(asan_alt: bool) -> str:
    options = {
        "halt_on_error": 1,
        "abort_on_error": 1,
        "exitcode": 0,
        "detect_leaks": 0,
        "malloc_context_size": 0,
        "symbolize": 0,
        "allocator_may_return_null": 1,
        "detect_odr_violation": 0,
        "handle_segv": 0,
        "handle_sigbus": 1,
        "handle_abort": 0,
        "handle_sigfpe": 0,
        "handle_sigill": 1,
        "print_summary": 0,
        "print_legend": 0,
        "print_full_thread_history": 0,
        "symbolize_inline_frames": 0,
    }
    if asan_alt:
        options["halt_on_error"] = 0
        options["abort_on_error"] = 0
        options["handle_sigbus"] = 0
        options["handle_sigill"] = 0
        options["log_path"] = "./asanlog"
    return ":".join(f"{key}={value}" for key, value in options.items())


def _last_json_line(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    previous = ""
    current = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            previous = current
            current = line
    for candidate in (current, previous):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
