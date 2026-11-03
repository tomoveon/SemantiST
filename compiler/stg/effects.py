"""Resolve available STG function bodies for transitive call-effect analysis."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def _names(pou: dict[str, Any]) -> set[str]:
    descriptor = pou.get("pou") or {}
    return {str(value).upper() for value in (
        descriptor.get("name"), descriptor.get("qualified_name")) if value}


def _calls(pou: dict[str, Any]) -> set[str]:
    return {str(call["callee"]).upper()
            for call in (pou.get("environment") or {}).get("calls") or []
            if not call.get("external") and call.get("callee")}


def resolve_effect_model(
    target_model: dict[str, Any],
    extract: Callable[[list[str]], dict[str, Any]],
    *,
    deadline: float,
    max_callees: int = 96,
) -> dict[str, Any]:
    """Extract reachable function implementations without changing target probes."""
    pous = list(target_model.get("pous") or [])
    known = {name for pou in pous for name in _names(pou)}
    attempted: set[str] = set()
    errors: list[str] = []
    pending = sorted(set().union(*(_calls(pou) for pou in pous)) - known) if pous else []
    while pending and len(attempted) < max_callees and time.monotonic() < deadline:
        batch = pending[:max_callees - len(attempted)]
        attempted.update(batch)
        try:
            extracted = extract(batch)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            break
        for pou in extracted.get("pous") or []:
            names = _names(pou)
            if names and not names.intersection(known):
                pous.append(pou)
                known.update(names)
        pending = sorted(set().union(*(_calls(pou) for pou in pous)) - known - attempted)
    unresolved = sorted(set().union(*(_calls(pou) for pou in pous)) - known) if pous else []
    status = ("COMPLETE" if not unresolved and not errors else
              "BUDGET_EXHAUSTED" if pending and (len(attempted) >= max_callees or
                                                time.monotonic() >= deadline) else
              "INCOMPLETE")
    return {
        "schema_version": target_model.get("schema_version"),
        "pous": pous,
        "effect_resolution": {"status": status, "attempted_callees": sorted(attempted),
                              "unresolved_callees": unresolved, "errors": errors},
    }
