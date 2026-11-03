"""STG expression and type helpers used by task and seed generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def kind(expr: dict[str, Any]) -> dict[str, Any]:
    value = expr.get("kind")
    return value if isinstance(value, dict) else {}


def member_name(expr: dict[str, Any]) -> str | None:
    if kind(expr).get("kind") == "member_access":
        return str(kind(expr).get("member"))
    if kind(expr).get("kind") == "variable":
        name = kind(expr).get("qualified_name")
        if isinstance(name, str):
            return name.rsplit(".", 1)[-1]
    return None


def literal(expr: dict[str, Any]) -> Any:
    if kind(expr).get("kind") != "literal":
        return None
    value = kind(expr).get("value")
    if not isinstance(value, dict):
        return None
    raw = value.get("value")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw, 0)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return raw
    return raw


def load_stg_types(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    types: dict[str, dict[str, Any]] = {}
    for pou in data.get("pous", []):
        for ty in pou.get("environment", {}).get("types", []):
            name = str(ty.get("name", "")).upper()
            if name:
                types[name] = ty
    return types


def collect_literals(stg_model: dict[str, Any]) -> set[int | float]:
    values: set[int | float] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            value = literal(node)
            if isinstance(value, (int, float)):
                values.add(value)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(stg_model)
    return values
