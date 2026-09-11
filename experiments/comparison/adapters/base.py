"""Base adapter contract shared by all comparison tools."""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.comparison.lib.hashing import canonical_json_sha256, sha256_file
from experiments.comparison.lib.manifest import Target
from experiments.comparison.lib.schema import failure_reason


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    semantist_root: Path
    workspace_root: Path
    artifact_root: Path
    icsquartz_root: Path | None
    online_budget_seconds: int
    per_execution_timeout_ms: int
    semantic_task_budget: int
    semantist_fb_max_cycles: int
    semantist_fb_stale_threshold: int
    semantist_fb_state_trace: bool
    observer_poll_interval_ms: int
    cpuset: str | None
    container_runtime: str | None
    container_runtime_status: str
    container_platform: str | None
    semantist_image: str
    reuse_preprocessing: bool = False


@dataclass(frozen=True)
class SupportResult:
    status: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def supported(cls, reason: str = "supported") -> "SupportResult":
        return cls("supported", reason)

    @classmethod
    def unsupported(cls, code: str, reason: str, **details: Any) -> "SupportResult":
        return cls("unsupported", reason, {"code": code, **details})


@dataclass
class PreprocessingResult:
    status: str
    preprocess_dir: Path
    start_monotonic_ns: int
    end_monotonic_ns: int
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    failure_reason: dict[str, object] | None = None

    @property
    def elapsed_seconds(self) -> float:
        return (self.end_monotonic_ns - self.start_monotonic_ns) / 1_000_000_000


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, log_tail_path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.log_tail_path = log_tail_path

    def to_failure_reason(self) -> dict[str, object]:
        return failure_reason(self.code, self.message, self.log_tail_path)


class BaseAdapter:
    tool_name = "base"
    supports_rng_seed = True
    initial_seed_provider = "adapter"
    initial_seed_format = "tool_native"
    initial_seed_policy = "adapter_provided"
    per_execution_timeout_model = "per_process"
    per_execution_timeout_ms: int | None = None

    def __init__(self, config: ExperimentConfig) -> None:
        self.config = config
        self.per_execution_timeout_ms = config.per_execution_timeout_ms

    def detect_support(self, target: Target) -> SupportResult:
        if target.kind not in {"FUNCTION", "FUNCTION_BLOCK"}:
            return SupportResult.unsupported(
                "unsupported_target_kind", f"unsupported target kind: {target.kind}"
            )
        if not target.st_file.is_file():
            return SupportResult.unsupported(
                "missing_st_file", f"missing ST file: {target.st_file}"
            )
        return SupportResult.supported()

    def preprocessing_dir(self, target: Target) -> Path:
        return (
            self.config.artifact_root
            / self.config.experiment_id
            / "preprocessing"
            / self.tool_name
            / target.suite
            / target.target_id
        )

    def run_dir(self, target: Target, trial_id: int) -> Path:
        return (
            self.config.artifact_root
            / self.config.experiment_id
            / "runs"
            / self.tool_name
            / target.suite
            / target.target_id
            / f"trial_{trial_id:02d}"
        )

    def relative_artifact(self, path: Path) -> str:
        experiment_root = self.config.artifact_root / self.config.experiment_id
        try:
            return str(path.relative_to(experiment_root))
        except ValueError:
            return str(path)

    def prepare(self, target: Target) -> PreprocessingResult:
        start = time.monotonic_ns()
        end = time.monotonic_ns()
        return PreprocessingResult("completed", self.preprocessing_dir(target), start, end)

    def materialize_trial(
        self, target: Target, preprocessing: PreprocessingResult, trial_id: int
    ) -> Path:
        run_dir = self.run_dir(target, trial_id)
        if run_dir.exists():
            shutil.rmtree(run_dir)
        for child in (
            "events",
            "corpus",
            "findings",
            "tool-artifacts",
            "logs",
        ):
            (run_dir / child).mkdir(parents=True, exist_ok=True)
        return run_dir

    def run(self, target: Target, run_dir: Path, preprocessing: PreprocessingResult, rng_seed: int, cpu: int) -> dict[str, Any]:
        raise NotImplementedError

    def seed_fields(self, rng_seed: int) -> dict[str, Any]:
        fields: dict[str, Any]
        if self.supports_rng_seed:
            fields = {
                "rng_seed_requested": rng_seed,
                "rng_seed_applied": rng_seed,
                "rng_seed_status": "applied",
            }
        else:
            fields = {
                "rng_seed_requested": rng_seed,
                "rng_seed_applied": None,
                "rng_seed_status": "tool_missing_seed_control",
            }
        fields["initial_seed_provider"] = self.initial_seed_provider
        fields["initial_seed_format"] = self.initial_seed_format
        fields["initial_seed_policy"] = self.initial_seed_policy
        return fields

    def copy_base_seeds(self, target: Target, destination: Path) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        for existing in destination.iterdir():
            if existing.is_file():
                existing.unlink()
            elif existing.is_dir():
                shutil.rmtree(existing)
        source_dir = target.st_file.parent / "seeds"
        copied: list[Path] = []
        if source_dir.is_dir():
            for source in sorted(source_dir.glob("*.seed")):
                if source.is_file() and source.stat().st_size:
                    dest = destination / source.name
                    shutil.copy2(source, dest)
                    copied.append(dest)
        if not copied:
            dest = destination / "seed"
            dest.write_bytes(b"X,INT,0\n")
            copied.append(dest)
        return copied

    def write_seed_manifest(
        self,
        destination: Path,
        seeds: list[Path],
        *,
        provider: str | None = None,
        seed_format: str | None = None,
        policy: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        manifest = {
            "schema_version": "semantist.comparison-seed-manifest/1.0.0",
            "tool": self.tool_name,
            "provider": provider or self.initial_seed_provider,
            "format": seed_format or self.initial_seed_format,
            "policy": policy or self.initial_seed_policy,
            "seed_count": len(seeds),
            "seeds": [
                {
                    "path": str(path),
                    "sha256": sha256_file(path) if path.is_file() else None,
                    "size": path.stat().st_size if path.is_file() else None,
                }
                for path in seeds
            ],
            "note": note,
        }
        (destination / "seed-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest

    def write_command(self, run_dir: Path, command: list[str], env: dict[str, str] | None, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "tool": self.tool_name,
            "command": command,
            "environment": env or {},
        }
        if extra:
            payload.update(extra)
        (run_dir / "command.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def write_preprocessing_command(
        self, preprocess_dir: Path, commands: list[dict[str, Any]]
    ) -> None:
        (preprocess_dir / "command.json").write_text(
            json.dumps({"tool": self.tool_name, "commands": commands}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def collect_common_metrics(self, run_dir: Path) -> dict[str, Any]:
        corpus_count = sum(1 for path in (run_dir / "corpus").rglob("*") if path.is_file())
        return {
            "corpus_count": corpus_count,
            "candidate_findings": [],
            "finding_found": False,
            "common_oracle_finding_found": False,
            "semantic_objective_found": False,
            "first_common_finding": None,
            "first_semantic_objective": None,
            "crash_count_raw": 0,
            "timeout_count_raw": 0,
            "tool_specific_metrics": {},
            "artifact_paths": {},
        }

    def baseline_image(self, key: str) -> str:
        lock_file = self.config.semantist_root / "experiments" / "baselines" / "baselines.lock.json"
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        return str(lock["images"][key]["tag"])

    def candidate_from_path(
        self,
        *,
        path: Path,
        target: Target,
        trial_id: int,
        oracle: str,
        discovery_phase: str = "fuzzing",
        execution_ordinal: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> dict[str, Any]:
        digest = sha256_file(path) if path.is_file() else canonical_json_sha256(str(path))
        return {
            "candidate_id": f"{self.tool_name}:{target.target_id}:{trial_id}:{oracle}:{digest}",
            "tool": self.tool_name,
            "target_id": target.target_id,
            "trial_id": trial_id,
            "discovery_phase": discovery_phase,
            "oracle": oracle,
            "observed_monotonic_ns": None,
            "observed_elapsed_seconds": elapsed_seconds,
            "tool_reported_time_seconds": elapsed_seconds,
            "tool_reported_unix_time_seconds": None,
            "execution_ordinal": execution_ordinal,
            "seed_sha256": digest if path.is_file() else None,
            "artifact_path": self.relative_artifact(path),
        }

    def parse_afl_stats(self, stats_path: Path) -> dict[str, Any]:
        if not stats_path.is_file():
            return {}
        parsed: dict[str, str] = {}
        for line in stats_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                parsed[key.strip()] = value.strip()
        run_time = _safe_float(parsed.get("run_time"))
        execs_done = _safe_int(parsed.get("execs_done"))
        execs_per_sec = _safe_float(parsed.get("execs_per_sec"))
        if run_time and execs_done is not None:
            execs_per_sec = execs_done / run_time
        return {
            "total_executions": execs_done,
            "execs_per_sec": execs_per_sec,
            "tool_specific_metrics": {"afl_fuzzer_stats": parsed},
        }

    def write_csv(self, path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in fieldnames})


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
