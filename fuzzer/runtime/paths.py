"""Shared default paths for generated semantist artifacts."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def artifact_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    path = Path(env.get("SEMANTIST_ARTIFACT_DIR", "artifacts"))
    return path if path.is_absolute() else ROOT / path


def build_artifact_dir(env: dict[str, str] | None = None) -> Path:
    return artifact_dir(env) / "build"


def runs_artifact_dir(env: dict[str, str] | None = None) -> Path:
    return artifact_dir(env) / "runs"


def cargo_target_dir(env: dict[str, str] | None = None) -> Path:
    return artifact_dir(env) / "cargo-target"
