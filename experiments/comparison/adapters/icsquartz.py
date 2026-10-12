"""ICSQuartz baseline adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.lib.st_extract import extract_pou_text, strip_pou
from experiments.comparison.adapters.base import AdapterError, BaseAdapter, PreprocessingResult, SupportResult
from experiments.comparison.lib.hashing import sha256_file
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.metrics import execution_count, metadata_for, native_inputs
from experiments.comparison.lib.process import (
    container_cpu_bound_command,
    container_image_id,
    container_run_command,
    run_capture,
)
from experiments.comparison.lib.schema import write_json

CONTAINER_COMMAND_TIMEOUT_SECONDS = 10
CONTAINER_START_TIMEOUT_SECONDS = 30
STATS_TAIL_BYTES = 256 * 1024


class ICSQuartzAdapter(BaseAdapter):
    tool_name = "icsquartz_full"
    supports_rng_seed = True
    initial_seed_provider = "icsquartz_native_initial_generator"
    initial_seed_format = "raw_bytes"
    initial_seed_policy = "native_forced_evaluation_of_default_plus_nine_pattern_inputs"
    cpu_slots = 1

    def detect_support(self, target: Target) -> SupportResult:
        base = super().detect_support(target)
        if base.status != "supported":
            return base
        required = [
            self.config.semantist_root
            / "experiments"
            / "baselines"
            / "icsquartz"
            / "target.Dockerfile",
            target.fixture("target.st"),
            target.fixture("program.st"),
            target.fixture("harness.c"),
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return SupportResult.unsupported(
                "icsquartz_missing_benchmark_layout",
                "ICSQuartz infrastructure or fixed local benchmark fixtures are missing",
                missing=missing,
            )
        return SupportResult.supported("ICSQuartz engine with the fixed local benchmark package")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists():
            raise AdapterError(
                "preprocessing_directory_exists",
                f"preprocessing directory exists; use a fresh experiment ID: {preprocess_dir}",
            )
        (preprocess_dir / "logs").mkdir(parents=True, exist_ok=True)
        benchmark_dir = preprocess_dir / "benchmark"
        (benchmark_dir / "src").mkdir(parents=True, exist_ok=True)
        (benchmark_dir / "icsquartz").mkdir(parents=True, exist_ok=True)
        (benchmark_dir / "compatibility").mkdir(parents=True, exist_ok=True)
        shutil.copy2(target.fixture("target.st"), benchmark_dir / "src" / "target.st")
        shutil.copy2(target.fixture("program.st"), benchmark_dir / "src" / "program.st")
        shutil.copy2(target.fixture("harness.c"), benchmark_dir / "icsquartz" / "harness.c")
        benchmark_sources = {
            name: sha256_file(target.fixture(name))
            for name in ("target.st", "program.st", "harness.c")
        }
        if target.compatibility_manifest is not None:
            compatibility = json.loads(
                target.compatibility_manifest.read_text(encoding="utf-8")
            )
            native = compatibility.get("native") or {}
            for relative in native.get("sources") or []:
                source = target.compatibility_manifest.parent / str(relative)
                destination = benchmark_dir / "compatibility" / source.name
                shutil.copy2(source, destination)
                benchmark_sources[f"compatibility/{source.name}"] = sha256_file(source)
        if target.suite == "oscat_basic":
            oscat_source = (
                self.config.semantist_root / "benchmarks" / "oscat_basic" / "source" / "oscat.st"
            )
            target_pou = extract_pou_text(str(target.st_file), target.function)
            library_pou = extract_pou_text(str(oscat_source), target.function)
            if target_pou.strip() != library_pou.strip():
                now = time.monotonic_ns()
                return PreprocessingResult(
                    "failed",
                    preprocess_dir,
                    now,
                    now,
                    failure_reason={
                        "code": "icsquartz_oscat_target_mismatch",
                        "message": "fixed target POU differs from the pinned OSCAT dependency source",
                        "log_tail_path": None,
                    },
                )
            deps_path = benchmark_dir / "src" / "oscat-deps.st"
            deps_path.write_text(
                strip_pou(oscat_source.read_text(encoding="utf-8"), target.function),
                encoding="utf-8",
                newline="\n",
            )
            stubs_source = oscat_source.with_name("stubs.st")
            stubs_path = benchmark_dir / "src" / "oscat-stubs.st"
            shutil.copy2(stubs_source, stubs_path)
            benchmark_sources["oscat-deps.st"] = sha256_file(deps_path)
            benchmark_sources["oscat-stubs.st"] = sha256_file(stubs_path)
        (benchmark_dir / "source-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "semantist.fixed-benchmark-context/1.0.0",
                    "target_id": target.target_id,
                    "fixture_sha256": benchmark_sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
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
        compiler = "semantist_common_ir"
        fuzzer_image = _image_tag("semantist-exp-icsquartz", "common-ir", target.target_id)
        fuzzer_dockerfile = (
            self.config.semantist_root
            / "experiments"
            / "baselines"
            / "icsquartz"
            / "target.Dockerfile"
        )
        semantist_env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/repo",
            "SEMANTIST_ARTIFACT_DIR": self.container_path(preprocess_dir),
            "SEMANTIST_BUILD_ARTIFACT_DIR": self.container_path(preprocess_dir / "build"),
            "SEMANTIST_SEMANTIC_INSTRUMENTATION": "off",
            "OUT_LL": self.container_path(benchmark_dir / "target.ll"),
            "PREPARED_LL": self.container_path(benchmark_dir / "target.prepared.ll"),
            "HARNESS_OUT": self.container_path(benchmark_dir / "icsquartz" / "harness.c"),
            "RUSTY_STDLIB_OUT": self.container_path(benchmark_dir / "libiec61131std.a"),
            "SEMANTIST_HARNESS_SOURCE": self.container_path(target.fixture("harness.c")),
            "SEMANTIST_PROGRAM_FILE": self.container_path(target.fixture("program.st")),
            "SEMANTIST_PUBLIC_API_EXTRA": "PLC_PRG,PLC_PRG__ctor,PLC_PRG_instance",
            "CARGO_TARGET_DIR": "/opt/semantist/cargo-target",
        }
        if target.compatibility_manifest is not None:
            semantist_env["SEMANTIST_COMPATIBILITY_MANIFEST"] = self.container_path(
                target.compatibility_manifest
            )
        target_path = self.container_path(target.st_file)
        semantist_commands = [
            self._semantist_container_command(
                semantist_env,
                ["./compiler/scripts/compile_st.sh", target_path, target.function],
            ),
            self._semantist_container_command(
                semantist_env,
                ["bash", "./compiler/scripts/prepare_baseline_target.sh", target.function],
            ),
        ]
        scan_cycle = "1" if target.suite == "icsquartz_scan_cycle" else "0"
        fuzzer_cmd = [
            runtime,
            "build",
            "-t",
            fuzzer_image,
            "--file",
            str(fuzzer_dockerfile),
            "--build-context",
            f"fuzztarget={benchmark_dir}",
            "--build-arg",
            f"ICSQUARTZ_BASE_IMAGE={self.baseline_image('icsquartz')}",
            "--build-arg",
            f"SCAN_CYCLE={scan_cycle}",
            "--build-arg",
            "ASAN_ALT=0",
            str(fuzzer_dockerfile.parent),
        ]
        if self.config.container_platform:
            fuzzer_cmd[2:2] = ["--platform", self.config.container_platform]
        steps = [
            *((command, self.config.semantist_root, None) for command in semantist_commands),
            (fuzzer_cmd, self.config.semantist_root, os.environ.copy()),
        ]
        self.write_preprocessing_command(
            preprocess_dir,
            [
                *(
                    {
                        "command": command,
                        "environment": _filtered_semantist_env(semantist_env),
                    }
                    for command in semantist_commands
                ),
                {"command": fuzzer_cmd, "environment": {}},
            ],
        )
        for index, (command, cwd, process_env) in enumerate(steps, 1):
            result = run_capture(
                command,
                cwd=cwd,
                env=process_env,
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
                        "code": "icsquartz_preprocessing_command_failed",
                        "message": f"preprocessing command failed: {' '.join(command)}",
                        "log_tail_path": self.relative_artifact(
                            preprocess_dir / "logs" / f"{index:02d}.stderr.log"
                        ),
                    },
                )
        end = time.monotonic_ns()
        metadata = {
            "compiler": compiler,
            "compiler_image": self.config.semantist_image,
            "compiler_image_id": container_image_id(runtime, self.config.semantist_image),
            "fuzzer_image": fuzzer_image,
            "fuzzer_image_id": container_image_id(runtime, fuzzer_image),
            "scan_cycle": scan_cycle == "1",
            "asan_alt": False,
            "container_runtime": runtime,
            "benchmark_context": str(benchmark_dir),
            "benchmark_fixture_sha256": benchmark_sources,
        }
        (preprocess_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not metadata["fuzzer_image_id"]:
            return PreprocessingResult(
                "failed", preprocess_dir, start, end, metrics=metadata,
                failure_reason={
                    "code": "icsquartz_target_image_id_missing",
                    "message": f"Cannot resolve the newly built target image ID: {fuzzer_image}",
                    "log_tail_path": self.relative_artifact(preprocess_dir / "metadata.json"),
                },
            )
        return PreprocessingResult(
            "completed",
            preprocess_dir,
            start,
            end,
            artifacts={"image": fuzzer_image},
            metrics=metadata,
        )

    def _semantist_container_command(
        self, env: dict[str, str], command: list[str]
    ) -> list[str]:
        runtime = self.config.container_runtime
        if runtime is None:
            raise RuntimeError(self.config.container_runtime_status)
        return container_run_command(
            runtime,
            self.config.semantist_image,
            mounts=[
                (self.config.semantist_root, "/repo", True),
                (self.config.artifact_root, "/artifacts", False),
            ],
            env=env,
            workdir="/repo",
            platform=self.config.container_platform,
            command=command,
        )

    def run(
        self,
        target: Target,
        run_dir: Path,
        preprocessing: PreprocessingResult,
        rng_seed: int,
        cpu: int | list[int],
    ) -> dict[str, Any]:
        assigned_cpus = [cpu] if isinstance(cpu, int) else list(cpu)
        if len(assigned_cpus) < self.cpu_slots:
            return {
                "run_status": "online_start_failed",
                "t0_monotonic_ns": None,
                "online_elapsed_seconds": 0.0,
                "failure_reason": {
                    "code": "icsquartz_insufficient_cpu_slots",
                    "message": "ICSQuartz needs one CPU slot for its fuzzing client",
                    "log_tail_path": None,
                },
                **self.collect_metrics(target, run_dir, []),
            }
        metadata = json.loads((preprocessing.preprocess_dir / "metadata.json").read_text(encoding="utf-8"))
        image = metadata.get("fuzzer_image_id")
        if not isinstance(image, str) or not image:
            raise AdapterError(
                "icsquartz_target_image_id_missing",
                "Preprocessing did not record a target image ID; start a fresh experiment",
            )
        container_name = self.container_name(target, run_dir, "online")
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
            # One upstream Launcher client is requested. Its broker/respawner
            # shares the same container cpuset and does not add a fuzz worker.
            # Launcher selects by ordinal within sched_getaffinity(), not by
            # the host CPU id, so the sole allowed CPU is always ordinal 0.
            "cores": _launcher_core_spec(assigned_cpus),
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
        command = container_cpu_bound_command(command, runtime, assigned_cpus)
        self.write_command(
            run_dir,
            command,
            env_args,
            {
                "assigned_cpus": assigned_cpus,
                "container_runtime": runtime,
                "icsquartz_args": icsquartz_args,
                "measurement_mode": "post_run_native_artifacts",
                "progress_check_note": "No online findings, statistics, or container-state polling; native counters are collected after stopping.",
                "initial_seed_note": (
                    "ICSQuartz forcibly evaluates its native ten-input initial generator "
                    "before entering the fuzz loop."
                ),
            },
        )
        t0 = time.monotonic_ns()
        deadline = t0 + self.config.online_budget_seconds * 1_000_000_000
        status = "online_start_failed"
        failure = None
        warnings: list[dict[str, Any]] = []
        infrastructure_failure = False
        started = False

        try:
            start = subprocess.run(
                command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=min(CONTAINER_START_TIMEOUT_SECONDS, self.config.online_budget_seconds),
            )
            (run_dir / "stdout.log").write_text(start.stdout, encoding="utf-8")
            (run_dir / "stderr.log").write_text(start.stderr, encoding="utf-8")
            if start.returncode != 0:
                failure = {
                    "code": "icsquartz_container_start_failed",
                    "message": start.stderr.strip() or start.stdout.strip(),
                    "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
                }
            else:
                started = True
                # Let the native fuzzer own its run. There is no observation,
                # counter watchdog, or container inspect in the online window.
                threading.Event().wait(max(0.0, (deadline - time.monotonic_ns()) / 1e9))
                status = "budget_completed"
        except Exception as error:
            status = "runner_error"
            failure = {"code": "icsquartz_online_launch_failed", "message": repr(error), "log_tail_path": self.relative_artifact(run_dir / "stderr.log")}
            infrastructure_failure = isinstance(error, OSError) and not isinstance(error, subprocess.SubprocessError)
        finally:
            # Stop before inspecting state or collecting any native artifacts.
            # KeyboardInterrupt propagates only after bounded cleanup.
            stop_result = _stop_container(runtime, container_name, run_dir)
            cleanup = _cleanup_container(runtime, container_name, run_dir, stop_result=stop_result)

        online_elapsed = (cleanup["stop_requested_monotonic_ns"] - t0) / 1e9
        stop_failed = cleanup.get("stop_returncode") != 0 and cleanup.get("kill_returncode") != 0
        infrastructure_failure = (
            infrastructure_failure or not cleanup["removed"] or bool(cleanup.get("diagnostic_error"))
            or (stop_failed and started)
            or (cleanup.get("state_after_stop") or {}).get("Running") is True
            or bool(cleanup.get("artifact_io_error"))
        )
        lifecycle = {
            "started": started, "timed_out": status == "budget_completed", "returncode": None,
            "t0_monotonic_ns": t0, "elapsed_seconds": online_elapsed,
            "stop_request_monotonic_ns": cleanup["stop_requested_monotonic_ns"],
            "stop_request_elapsed_seconds": online_elapsed,
            "cleanup_inclusive_elapsed_seconds": (time.monotonic_ns() - t0) / 1e9,
            "cleanup_confirmed": cleanup["removed"], "infrastructure_failure": infrastructure_failure,
            "container_stop": cleanup, "collection_mode": "post_run", "collection_complete": False,
            "events": [],
        }
        write_json(run_dir / "process-result.json", lifecycle)
        events: list[dict[str, Any]] = []
        candidate_collection_complete = True
        try:
            if not cleanup["removed"]:
                raise RuntimeError("Candidate collection skipped because container cleanup is unconfirmed")
            events = self.scan_finding_events(target, run_dir)
        except Exception as error:
            candidate_collection_complete = False
            warnings.append({"code": "icsquartz_post_run_collection_failed", "message": repr(error)})
            if failure is None:
                status = "runner_error"
                failure = {"code": "icsquartz_post_run_collection_failed", "message": repr(error), "log_tail_path": None}
        if status == "budget_completed":
            state = cleanup.get("state_after_stop") or {}
            finished = _container_time(state.get("FinishedAt"))
            requested = _container_time(cleanup.get("logs_until"))
            if finished is not None and requested is not None and finished < requested:
                status = "online_crashed"
                failure = {"code": "icsquartz_exited_before_budget", "message": f"Container had exited before the stop request (exit code {state.get('ExitCode')})", "log_tail_path": self.relative_artifact(run_dir / "container-cleanup.json")}
            elif finished is None or state.get("Running") is not False:
                status = "runner_error"
                failure = {"code": "icsquartz_exit_state_unavailable", "message": "Post-stop container state could not establish the native lifecycle", "log_tail_path": self.relative_artifact(run_dir / "container-cleanup.json")}
        if status not in {"online_start_failed", "runner_error"}:
            # Timestamp-filtered container streams and the pre-stop byte prefix
            # distinguish native failures from errors induced by our SIGTERM.
            for log_path, limit in (
                (fuzzer_log, cleanup.get("fuzzer_log_online_bytes", 0)),
                (run_dir / "container.pre-stop.stdout.log", None),
                (run_dir / "container.pre-stop.stderr.log", None),
            ):
                try:
                    log_failure = _fuzzer_log_failure(log_path, unrecoverable_only=True, max_bytes=limit)
                except OSError as error:
                    status = "runner_error"
                    failure = {"code": "icsquartz_online_log_read_failed", "message": repr(error), "log_tail_path": self.relative_artifact(log_path)}
                    break
                if log_failure is not None:
                    status = "online_crashed"
                    failure = {"code": "icsquartz_client_fatal_log", "message": log_failure, "log_tail_path": self.relative_artifact(log_path)}
                    break
        if status == "budget_completed" and (cleanup.get("online_logs_error") or cleanup.get("log_boundary_error")):
            status = "runner_error"
            failure = {"code": "icsquartz_online_logs_incomplete", "message": "Online client liveness evidence could not be fully collected; retained metrics require review", "log_tail_path": self.relative_artifact(run_dir / "container-cleanup.json")}
        container_stdout = run_dir / "container.stdout.log"
        try:
            if fuzzer_log.stat().st_size == 0 and container_stdout.is_file():
                shutil.copyfile(container_stdout, fuzzer_log)
        except OSError as error:
            warnings.append({"code": "icsquartz_fuzzer_log_copy_failed", "message": repr(error)})
        lifecycle.update({"events": events, "collection_complete": candidate_collection_complete})
        write_json(run_dir / "process-result.json", lifecycle)
        metrics = self.collect_stopped_metrics(target, run_dir, lifecycle)
        if metrics["run_status"] == "runner_error":
            status = "runner_error"
            failure = failure or metrics.get("failure_reason")
            candidate_collection_complete = metrics["candidate_collection_complete"]
        if status == "budget_completed" and not metrics.get("total_executions"):
            status = "runner_error"
            failure = {"code": "native_execution_count_missing", "message": "Post-run artifacts contain no positive native execution count", "log_tail_path": self.relative_artifact(stats_file)}
        metrics.update(
            {
                "t0_monotonic_ns": t0,
                "online_elapsed_seconds": online_elapsed,
                "total_executions_window": "native_stop_snapshot_may_include_shutdown_tail",
                "measurement_mode": "post_run_native_artifacts",
                "collection_mode": "post_run",
                "candidate_collection_complete": candidate_collection_complete,
                "cleanup_inclusive_elapsed_seconds": (time.monotonic_ns() - t0) / 1e9,
                "run_status": status,
            }
        )
        if failure is not None:
            metrics["failure_reason"] = failure
        metrics["tool_specific_metrics"]["container_cleanup"] = cleanup
        metrics["tool_specific_metrics"]["cleanup_confirmed"] = cleanup["removed"]
        metrics["tool_specific_metrics"]["measurement_warnings"] = warnings
        metrics["tool_specific_metrics"]["infrastructure_failure"] = infrastructure_failure
        metrics["tool_specific_metrics"]["stop_request_elapsed_seconds"] = online_elapsed
        if not cleanup["removed"]:
            cleanup["prior_run_status"] = status
            cleanup["prior_failure_reason"] = failure
            metrics["run_status"] = "runner_error"
            metrics["failure_reason"] = {
                "code": "icsquartz_container_cleanup_failed",
                "message": "Container removal failed; stop the remaining container before any further experiment",
                "log_tail_path": self.relative_artifact(run_dir / "container-cleanup.json"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        events: list[dict[str, Any]] = []
        crash_dir = run_dir / "tool-artifacts" / "crashes"
        for seed_path in native_inputs(crash_dir):
            raw = metadata_for(seed_path)
            executions = execution_count(raw.get("executions"))
            event = self.candidate_from_path(
                path=seed_path,
                target=target,
                trial_id=trial_id,
                oracle="target_signal",
                execution_ordinal=executions,
            )
            event["execution_count_source"] = "libafl_testcase_metadata" if executions is not None else "unavailable"
            event["time_source"] = "unmeasured_post_run"
            event["collection_phase"] = "post_run"
            event["native_time_to_find_seconds"] = _native_time_to_find_seconds(raw)
            event["native_time_origin"] = "libafl_state_start_time"
            event["native_time_alignment"] = "not_aligned_to_runner_launch"
            if raw:
                event["metadata_artifact_path"] = self.relative_artifact(seed_path.with_name(f".{seed_path.name}.metadata"))
            events.append(
                event
            )
        return events

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        latest = _last_json_line(run_dir / "tool-artifacts" / "fuzzer_stats.json")
        if latest:
            metrics["execs_per_sec"] = _safe_float(latest.get("exec_sec"))
            metrics["total_executions"] = _latest_total_executions(run_dir / "tool-artifacts" / "fuzzer_stats.json")
            active_client = _active_client_stats(latest)
            try:
                metrics["tool_specific_metrics"]["state_resets"] = int(
                    active_client["user_monitor"]["stale_state_"]["value"]["Number"]
                )
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            metrics["tool_specific_metrics"]["icsquartz_stats"] = latest
        events = observed_events
        metrics["candidate_findings"] = events
        metrics["finding_found"] = bool(events)
        metrics["common_oracle_finding_found"] = bool(events)
        metrics["first_common_finding"] = events[0] if events else None
        metrics["crash_count_raw"] = len(events)
        corpus_dir = run_dir / "tool-artifacts" / "corpus"
        try:
            metrics["corpus_count"] = len(native_inputs(corpus_dir))
        except OSError as error:
            metrics["corpus_count"] = None
            metrics["tool_specific_metrics"]["corpus_collection_error"] = repr(error)
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


def _image_tag(prefix: str, compiler: str, target_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", target_id).lower()
    return f"{prefix}-{compiler}:{safe}"


def _container_state(runtime: str, name: str) -> dict[str, Any]:
    result = subprocess.run(
        [runtime, "inspect", "-f", "{{json .State}}", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=CONTAINER_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot inspect ICSQuartz container {name}: {result.stderr.strip()}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("container inspect returned no state object")
    return value


def _container_time(value: object) -> datetime | None:
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _stop_container(runtime: str, name: str, run_dir: Path) -> dict[str, Any]:
    """Request the budget stop without collecting logs on the deadline path."""
    result: dict[str, Any] = {
        "stop_requested_monotonic_ns": time.monotonic_ns(),
        "logs_until": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }
    try:
        result["fuzzer_log_online_bytes"] = (run_dir / "tool-artifacts" / "fuzzer_log").stat().st_size
    except OSError as error:
        result["log_boundary_error"] = repr(error)
    try:
        stopped = subprocess.run(
            [runtime, "stop", "--time", "2", name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CONTAINER_COMMAND_TIMEOUT_SECONDS,
        )
        result["stop_returncode"] = stopped.returncode
        if stopped.returncode != 0:
            result["stop_error"] = stopped.stderr[-2000:]
    except Exception as error:
        result["stop_error"] = repr(error)
    if result.get("stop_returncode") != 0:
        # A failed graceful stop must not leave the target running while logs
        # are read. Force removal below remains the final cleanup guarantee.
        try:
            killed = subprocess.run(
                [runtime, "kill", name], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=CONTAINER_COMMAND_TIMEOUT_SECONDS,
            )
            result["kill_returncode"] = killed.returncode
            if killed.returncode != 0:
                result["kill_error"] = killed.stderr[-2000:]
        except Exception as error:
            result["kill_error"] = repr(error)
    return result


def _cleanup_container(
    runtime: str, name: str, run_dir: Path, *, stop_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bound every cleanup operation and preserve evidence before removal.

    Do not mask an in-flight observer error or KeyboardInterrupt. The caller
    rejects otherwise successful runs when removal cannot be confirmed.
    """
    cleanup: dict[str, Any] = {
        "container": name, "removed": False,
        **(stop_result if stop_result is not None else _stop_container(runtime, name, run_dir)),
    }
    stopped = cleanup.get("stop_returncode") == 0 or cleanup.get("kill_returncode") == 0
    if stopped:
        try:
            cleanup["state_after_stop"] = _container_state(runtime, name)
        except Exception as error:
            cleanup["state_inspection_error"] = repr(error)
        try:
            _write_container_logs(
                runtime, name,
                run_dir / "container.pre-stop.stdout.log", run_dir / "container.pre-stop.stderr.log",
                until=cleanup["logs_until"],
            )
        except Exception as error:
            cleanup["online_logs_error"] = repr(error)
            if isinstance(error, OSError):
                cleanup["artifact_io_error"] = repr(error)
        try:
            _write_container_logs(runtime, name, run_dir / "container.stdout.log", run_dir / "container.stderr.log")
        except Exception as error:
            cleanup["logs_error"] = repr(error)
            if isinstance(error, OSError):
                cleanup["artifact_io_error"] = repr(error)
    else:
        # Do not postpone the final force removal to collect diagnostics while
        # the target might still be executing beyond its budget.
        cleanup["online_logs_error"] = "Stop and kill could not be confirmed; force removal takes priority"
    try:
        removed = subprocess.run(
            [runtime, "rm", "--force", name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CONTAINER_COMMAND_TIMEOUT_SECONDS,
        )
        cleanup["remove_returncode"] = removed.returncode
        cleanup["removed"] = removed.returncode == 0 or "no such container" in removed.stderr.lower()
        if not cleanup["removed"]:
            cleanup["remove_error"] = removed.stderr[-2000:]
    except Exception as error:
        cleanup["remove_error"] = repr(error)
    try:
        (run_dir / "container-cleanup.json").write_text(
            json.dumps(cleanup, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as error:
        cleanup["diagnostic_error"] = repr(error)
    return cleanup


def _write_container_logs(
    runtime: str, name: str, stdout_path: Path, stderr_path: Path, *, until: str | None = None,
) -> None:
    command = [runtime, "logs"]
    if until is not None:
        command.extend(["--until", until])
    command.append(name)
    # Stream to files instead of buffering the entire container history in
    # memory. TimeoutExpired kills/reaps the CLI but keeps its partial output.
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(
            command, stdout=stdout, stderr=stderr,
            timeout=CONTAINER_COMMAND_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        raise RuntimeError(f"cannot collect ICSQuartz container logs (exit {result.returncode}); see {stderr_path}")


def _input_for_metadata(metadata_path: Path) -> Path | None:
    name = metadata_path.name
    if not (name.startswith(".") and name.endswith(".metadata")):
        return None
    candidate = metadata_path.with_name(name[1 : -len(".metadata")])
    return candidate if candidate.is_file() else None


def _native_time_to_find_seconds(metadata: dict[str, Any]) -> float | None:
    """Retain the native Duration without inventing alignment to host launch."""
    pending: list[Any] = [metadata]
    values: list[float] = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            duration = value.get("time_to_find")
            if isinstance(duration, dict):
                seconds = execution_count(duration.get("secs"))
                nanos = execution_count(duration.get("nanos"))
                if seconds is not None and nanos is not None and nanos < 1_000_000_000:
                    values.append(seconds + nanos / 1e9)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return values[0] if len(values) == 1 else None


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


def _launcher_core_spec(assigned_cpus: list[int]) -> str:
    """Map a singleton host affinity mask to LibAFL's visible-core ordinal."""
    if len(assigned_cpus) != 1:
        raise ValueError("the comparison ICSQuartz client requires exactly one CPU slot")
    return "0"


def _last_json_line(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - STATS_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(STATS_TAIL_BYTES)
    if start:
        # The first bytes may be the middle of a record; never parse them.
        data = data.partition(b"\n")[2]
    candidates = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    for candidate in reversed(candidates[-2:]):
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue
    return None


def _fuzzer_log_failure(
    path: Path, *, unrecoverable_only: bool = False, max_bytes: int | None = None,
) -> str | None:
    if not path.is_file():
        return None
    # These terminate the sole client's respawner/launcher rather than an
    # individual target child. A generic panic alone can be recovered by LibAFL.
    markers = (
        "Fuzzer-respawner: Storing state in crashed fuzzer instance did not work",
        "Target received SIGKILL!. This could indicate the target crashed due to OOM",
        "Failed to run launcher:",
    )
    if not unrecoverable_only:
        markers += ("fatal runtime error:", "thread '<unnamed>' panicked at")
    # Scan incrementally and, for the native fuzzer log, only inspect bytes
    # present before our stop request. Shutdown panics are not online failures.
    overlap = max(map(len, markers)) - 1
    previous = b""
    remaining = max_bytes
    with path.open("rb") as handle:
        while remaining is None or remaining > 0:
            data = handle.read(65536 if remaining is None else min(65536, remaining))
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            text = previous + data
            for marker in markers:
                if marker.encode() in text:
                    return f"ICSQuartz client log contains fatal marker: {marker}"
            previous = text[-overlap:]
    return None


def _active_client_stats(stats: dict[str, Any]) -> dict[str, Any]:
    clients = stats.get("client_stats")
    if not isinstance(clients, list):
        return {}
    candidates = [client for client in clients if isinstance(client, dict) and client.get("enabled")]
    if not candidates:
        candidates = [client for client in clients if isinstance(client, dict)]
    return max(candidates, key=lambda client: _safe_int(client.get("executions")) or 0, default={})


def _latest_total_executions(path: Path) -> int | None:
    latest = _last_json_line(path)
    if not latest:
        return None
    values = [_safe_int(latest.get("executions"))]
    client = _active_client_stats(latest)
    values.append(_safe_int(client.get("executions")))
    values.append(_client_reported_executions(client))
    measured = [value for value in values if value is not None and value >= 0]
    return max(measured) if measured else None


def _client_reported_executions(client: dict[str, Any]) -> int | None:
    try:
        return _safe_int(client["user_monitor"]["executions_"]["value"]["Number"])
    except (KeyError, TypeError):
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


def _filtered_semantist_env(env: dict[str, str]) -> dict[str, str]:
    keep = {
        "CARGO_TARGET_DIR",
        "HARNESS_OUT",
        "OUT_LL",
        "PREPARED_LL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "RUSTY_STDLIB_OUT",
        "SEMANTIST_ARTIFACT_DIR",
        "SEMANTIST_BUILD_ARTIFACT_DIR",
        "SEMANTIST_COMPATIBILITY_MANIFEST",
        "SEMANTIST_HARNESS_SOURCE",
        "SEMANTIST_PROGRAM_FILE",
        "SEMANTIST_PUBLIC_API_EXTRA",
        "SEMANTIST_SEMANTIC_INSTRUMENTATION",
    }
    return {key: value for key, value in env.items() if key in keep}
