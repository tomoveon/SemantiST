"""Evaluate declared output properties against cold native frame traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fuzzer.corpus.frames import parse_testcase
from fuzzer.pipeline.probe import Observation


def load_properties(path: str | Path | None, function: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != "semantist.output-properties/1.0.0":
        raise ValueError("unsupported output property schema")
    properties = document.get("targets", {}).get(function, [])
    if not isinstance(properties, list):
        raise ValueError(f"properties for {function} must be a list")
    seen_ids: set[str] = set()
    for item in properties:
        if not isinstance(item, dict) or item.get("kind") != "initialized_window_range":
            raise ValueError("unsupported output property kind")
        if not all(isinstance(item.get(key), str) for key in ("id", "input", "window", "reset", "output")):
            raise ValueError("output property requires id, input, window, reset and output")
        if not item["id"] or item["id"] in seen_ids:
            raise ValueError("output property ids must be distinct non-empty strings")
        seen_ids.add(item["id"])
    return properties


def _number(value: Any) -> int:
    return int(str(value), 0) if isinstance(value, str) and value.lower().startswith("0x") else int(value)


def evaluate_properties(observation: Observation, testcase: bytes, abi_like: Any,
                        properties: list[dict[str, Any]], *,
                        expected_id: str | None = None) -> dict[str, Any] | None:
    if (observation.timed_out or observation.scan_budget or observation.returncode != 0
            or not observation.states or not properties):
        return None
    try:
        case = parse_testcase(testcase, abi_like)
    except (TypeError, ValueError):
        return None
    for spec in properties:
        if expected_id is not None and spec["id"] != expected_id:
            continue
        if spec["kind"] != "initialized_window_range":
            continue
        for cycle in range(1, min(len(case.frames), len(observation.states))):
            previous = {key.upper(): value for key, value in case.frames[cycle - 1].values.items()}
            current = {key.upper(): value for key, value in case.frames[cycle].values.items()}
            output = {key.upper(): value for key, value in observation.states[cycle].items()}
            try:
                reset = spec["reset"].upper()
                window = spec["window"].upper()
                value = spec["input"].upper()
                result = spec["output"].upper()
                if cycle > 1 and _number(previous[reset]) == 0:
                    continue
                n = _number(previous[window])
                if (n <= 0 or n != _number(current[window]) or _number(current[reset]) != 0):
                    continue
                before, after, actual = (_number(previous[value]), _number(current[value]),
                                         _number(output[result]))
            except (KeyError, TypeError, ValueError):
                continue
            low, high = min(before, after), max(before, after)
            if low <= actual <= high:
                continue
            return {
                "oracle": "output_invariant", "hazard": "user_property",
                "kind": "property_violation", "property_id": spec["id"],
                "property_spec": spec, "runtime_id": None, "cycle": cycle,
                "source": spec.get("source"), "modeling": "declared_property",
                "diagnostic": f"{result}={actual} outside [{low}, {high}] after initialization",
                "observed": actual, "expected_range": [low, high],
            }
    return None
