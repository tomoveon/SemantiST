"""Load dialect profiles and library configurations."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIALECTS = HERE / "dialects"
LIBRARIES = HERE / "libraries"


def load_dialect(name: str) -> dict:
    path = DIALECTS / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"unknown dialect profile: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_library(name: str) -> dict:
    path = Path(name) if name.endswith(".json") else LIBRARIES / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def dialect_identity(profile: dict) -> dict:
    """The part of a dialect profile that enters TargetIdentity."""
    return {"id": profile["id"], "version": profile["version"], "rules": list(profile["rules"]),
            "compiler_args": list(profile.get("compiler_args", []))}
