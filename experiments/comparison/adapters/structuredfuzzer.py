"""StructuredFuzzer baseline adapter."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from compiler.parser.st import Param, StTarget, TypeSpec, parse_target

from experiments.comparison.adapters.base import BaseAdapter, PreprocessingResult, SupportResult
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.process import (
    bind_mount,
    container_cpu_bound_command,
    run_capture,
    run_with_budget,
)


class StructuredFuzzerAdapter(BaseAdapter):
    tool_name = "structuredfuzzer_full"
    supports_rng_seed = False
    initial_seed_provider = "structuredfuzzer_adapter"
    initial_seed_format = "raw_bytes"
    initial_seed_policy = "single_zero_byte_seed"

    def detect_support(self, target: Target) -> SupportResult:
        base = super().detect_support(target)
        if base.status != "supported":
            return base
        try:
            parse_target(str(target.st_file), target.function)
        except Exception as error:  # noqa: BLE001 - support probing must not abort the matrix.
            return SupportResult.unsupported(
                "structuredfuzzer_signature_parse_failed",
                f"unable to parse target signature: {error}",
            )
        return SupportResult.supported("attempt via StructuredFuzzer ST compiler/runtime")

    def prepare(self, target: Target) -> PreprocessingResult:
        preprocess_dir = self.preprocessing_dir(target)
        if preprocess_dir.exists() and self.config.reuse_preprocessing:
            return PreprocessingResult("completed", preprocess_dir, 0, 0)
        if preprocess_dir.exists():
            shutil.rmtree(preprocess_dir)
        for child in ("base-seeds", "logs", "build"):
            (preprocess_dir / child).mkdir(parents=True, exist_ok=True)
        start = time.monotonic_ns()
        try:
            parsed = parse_target(str(target.st_file), target.function)
            program = _program_source(target, parsed)
            (preprocess_dir / "program.st").write_text(program, encoding="utf-8")
            (preprocess_dir / "harness.c").write_text(_harness_source(), encoding="utf-8")
            (preprocess_dir / "base-seeds" / "seed").write_bytes(b"\x00")
            self.write_seed_manifest(
                preprocess_dir,
                [preprocess_dir / "base-seeds" / "seed"],
                note="StructuredFuzzer receives its native byte-level seed corpus; it has no explicit RNG seed control.",
            )
        except Exception as error:  # noqa: BLE001
            now = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                now,
                failure_reason={
                    "code": "structuredfuzzer_wrapper_generation_failed",
                    "message": str(error),
                    "log_tail_path": None,
                },
            )
        runtime = self.config.container_runtime
        if runtime is None:
            now = time.monotonic_ns()
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                now,
                failure_reason={
                    "code": "container_runtime_unavailable",
                    "message": self.config.container_runtime_status,
                    "log_tail_path": None,
                },
            )
        image = self.baseline_image("structuredfuzzer")
        command = [
            runtime,
            "run",
            "--rm",
            "-v",
            bind_mount(runtime, preprocess_dir, "/work"),
            "-w",
            "/work",
            image,
            "/bin/bash",
            "-lc",
            "stcompile program.st harness.c -o build -n fuzz_target",
        ]
        if self.config.container_platform:
            command[2:2] = ["--platform", self.config.container_platform]
        self.write_preprocessing_command(
            preprocess_dir,
            [{"command": command, "environment": {}}],
        )
        result = run_capture(
            command,
            cwd=self.config.semantist_root,
            env=None,
            stdout_path=preprocess_dir / "logs" / "01.stdout.log",
            stderr_path=preprocess_dir / "logs" / "01.stderr.log",
        )
        start = min(start, int(result["start_monotonic_ns"]))
        end = int(result["end_monotonic_ns"])
        target_bin = _find_target_binary(preprocess_dir)
        if result.get("returncode") != 0 or target_bin is None:
            return PreprocessingResult(
                "failed",
                preprocess_dir,
                start,
                end,
                failure_reason={
                    "code": "structuredfuzzer_preprocessing_command_failed",
                    "message": "StructuredFuzzer stcompile failed or did not produce fuzz_target",
                    "log_tail_path": self.relative_artifact(preprocess_dir / "logs" / "01.stderr.log"),
                },
            )
        metadata = {"image": image, "target": str(target_bin)}
        (preprocess_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return PreprocessingResult(
            "completed",
            preprocess_dir,
            start,
            end,
            artifacts={
                "target": str(target_bin),
                "harness": str(preprocess_dir / "harness.c"),
                "seed_manifest": str(preprocess_dir / "seed-manifest.json"),
            },
            metrics=metadata,
        )

    def materialize_trial(
        self, target: Target, preprocessing: PreprocessingResult, trial_id: int
    ) -> Path:
        run_dir = super().materialize_trial(target, preprocessing, trial_id)
        _copy_dir(preprocessing.preprocess_dir / "base-seeds", run_dir / "inputs")
        target_bin = Path((json.loads((preprocessing.preprocess_dir / "metadata.json").read_text(encoding="utf-8")))["target"])
        shutil.copy2(target_bin, run_dir / "tool-artifacts" / "fuzz_target")
        shutil.copy2(preprocessing.preprocess_dir / "program.st", run_dir / "tool-artifacts" / "program.st")
        shutil.copy2(preprocessing.preprocess_dir / "harness.c", run_dir / "tool-artifacts" / "harness.c")
        shutil.copy2(preprocessing.preprocess_dir / "seed-manifest.json", run_dir / "seed-manifest.json")
        return run_dir

    def run(self, target: Target, run_dir: Path, preprocessing: PreprocessingResult, rng_seed: int, cpu: int) -> dict[str, Any]:
        image = self.baseline_image("structuredfuzzer")
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
            "-v",
            bind_mount(runtime, run_dir / "inputs", "/inputs", readonly=True),
            "-v",
            bind_mount(runtime, run_dir / "tool-artifacts", "/out"),
            image,
            "/bin/bash",
            "-lc",
            f"stfuzz -i /inputs -o /out/fuzzer -t {self.config.per_execution_timeout_ms} -l /out/libafl.log /out/fuzz_target",
        ]
        if self.config.container_platform:
            command[2:2] = ["--platform", self.config.container_platform]
        command = container_cpu_bound_command(command, runtime, cpu)
        self.write_command(
            run_dir,
            command,
            {},
            {
                "container_runtime": runtime,
                "rng_seed_note": "StructuredFuzzer uses current_nanos() internally and has no CLI seed",
            },
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
                "code": "structuredfuzzer_online_start_failed",
                "message": str(result.get("error")),
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        elif not result["timed_out"] and result["returncode"] not in (0, None):
            metrics["failure_reason"] = {
                "code": "structuredfuzzer_online_command_failed",
                "message": f"StructuredFuzzer exited with return code {result['returncode']}",
                "log_tail_path": self.relative_artifact(run_dir / "stderr.log"),
            }
        return metrics

    def scan_finding_events(self, target: Target, run_dir: Path) -> list[dict[str, Any]]:
        trial_id = int(run_dir.name.rsplit("_", 1)[-1])
        crash_dir = run_dir / "tool-artifacts" / "fuzzer" / "crashes"
        return [
            self.candidate_from_path(
                path=path,
                target=target,
                trial_id=trial_id,
                oracle="target_signal",
            )
            for path in sorted(crash_dir.rglob("*"))
            if path.is_file()
        ]

    def collect_metrics(self, target: Target, run_dir: Path, observed_events: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.collect_common_metrics(run_dir)
        events = observed_events or self.scan_finding_events(target, run_dir)
        metrics["candidate_findings"] = events
        metrics["finding_found"] = bool(events)
        metrics["common_oracle_finding_found"] = bool(events)
        metrics["first_common_finding"] = events[0] if events else None
        metrics["crash_count_raw"] = len(events)
        queue_dir = run_dir / "tool-artifacts" / "fuzzer" / "queue"
        metrics["corpus_count"] = sum(1 for path in queue_dir.rglob("*") if path.is_file())
        metrics["artifact_paths"] = {
            "structuredfuzzer_output": self.relative_artifact(run_dir / "tool-artifacts" / "fuzzer"),
            "libafl_log": self.relative_artifact(run_dir / "tool-artifacts" / "libafl.log"),
            "seed_manifest": self.relative_artifact(run_dir / "seed-manifest.json"),
        }
        metrics["tool_specific_metrics"] = _parse_libafl_log(run_dir / "tool-artifacts" / "libafl.log")
        return metrics


def _program_source(target: Target, parsed: StTarget) -> str:
    source = target.st_file.read_text(encoding="utf-8")
    declarations: list[str] = []
    for param in parsed.params:
        declarations.append(f"    {param.name} : {_st_type(param.spec)};")
    if parsed.kind == "FUNCTION":
        assert parsed.ret_spec is not None
        declarations.append(f"    __semantist_result : {_st_type(parsed.ret_spec)};")
        arguments = ", ".join(f"{param.name} := {param.name}" for param in parsed.params)
        body = f"__semantist_result := {parsed.name}({arguments});"
    else:
        declarations.append(f"    __semantist_fb : {parsed.name};")
        arguments = ", ".join(
            f"{param.name} := {param.name}"
            for param in parsed.params
            if param.block_kind in {"VAR_INPUT", "VAR_IN_OUT"}
        )
        body = f"__semantist_fb({arguments});"
    return (
        source.rstrip()
        + "\n\nPROGRAM PLC_PRG\nVAR\n"
        + "\n".join(declarations)
        + "\nEND_VAR\n"
        + body
        + "\nEND_PROGRAM\n"
    )


def _st_type(spec: TypeSpec) -> str:
    if spec.kind == "scalar":
        return spec.name
    if spec.kind == "string":
        base = "WSTRING" if spec.c_type == "uint16_t" else "STRING"
        return f"{base}({spec.length})"
    if spec.kind == "array":
        return f"ARRAY[0..{max((spec.count or 1) - 1, 0)}] OF {_st_type(spec.element)}"
    if spec.kind == "pointer":
        return f"REF_TO {_st_type(spec.element)}" if spec.element else "REF_TO BYTE"
    raise ValueError(f"unsupported StructuredFuzzer type: {spec.kind}")


def _harness_source() -> str:
    return r'''
#include <stdint.h>
#include <stddef.h>

void config_init__(void);
void config_run__(unsigned long tick);
void set_plc_input(uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {
    config_init__();
    set_plc_input((uint8_t *)Data, Size);
    config_run__(0);
    return 0;
}
'''.lstrip()


def _find_target_binary(preprocess_dir: Path) -> Path | None:
    for candidate in (
        preprocess_dir / "build" / "fuzz_target",
        preprocess_dir / "fuzz_target",
        preprocess_dir / "build" / "program",
    ):
        if candidate.is_file():
            return candidate
    for path in sorted((preprocess_dir / "build").rglob("*")):
        if path.is_file() and path.stat().st_mode & 0o111:
            return path
    return None


def _copy_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _parse_libafl_log(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    metrics: dict[str, Any] = {}
    exec_matches = re.findall(r"executions[:= ]+([0-9]+)", text, flags=re.IGNORECASE)
    if exec_matches:
        metrics["reported_executions"] = int(exec_matches[-1])
    return metrics


def _status_from_process(result: dict[str, Any], finding_found: bool) -> str:
    if not result["started"]:
        return "online_start_failed"
    if result["timed_out"]:
        return "budget_completed"
    if result["returncode"] == 0:
        return "early_completed_with_finding" if finding_found else "early_completed_no_finding"
    return "online_crashed"
