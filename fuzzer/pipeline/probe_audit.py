"""Cross-check source risks against STG targets and generated IR probes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _risk_kind(expression: dict[str, Any], expressions: dict[str, dict[str, Any]],
               types: dict[str, dict[str, Any]]) -> str | None:
    kind = expression.get("kind") or {}
    name = kind.get("kind")
    if name == "array_index":
        return "array_access"
    if name == "cast":
        return "dangerous_conversion"
    if name == "binary":
        operator = str(kind.get("operator") or "").upper()
        if operator in {"+", "-", "*", "**"}:
            return "arithmetic_boundary"
        if operator == "/":
            return "division"
        if operator == "MOD":
            return "modulo"
    if name == "call" and "_TO_" in str(kind.get("callee") or "").upper():
        operands = expression.get("operands") or []
        source = expressions.get(operands[0], {}) if operands else {}
        source_type = types.get(str(source.get("type_name") or "").upper(), {})
        target_type = types.get(str(expression.get("type_name") or "").upper(), {})
        if source_type.get("kind") == "float" and target_type.get("kind") == "integer":
            return "dangerous_conversion"
    return None


def _platform_assumptions(pou: dict[str, Any], types: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    symbols = {item.get("qualified_name"): item.get("type_name")
               for item in (pou.get("environment") or {}).get("symbols", [])}
    tainted: set[str] = set()
    pointer_width = 0
    for name, type_name in symbols.items():
        ty = types.get(str(type_name or "").upper(), {})
        if ty.get("kind") == "pointer":
            tainted.add(name)
            pointer_width = max(pointer_width, int(ty.get("bit_width") or 0))
    if not pointer_width:
        return []
    assignments = [operation for edge in (pou.get("graph") or {}).get("edges", [])
                   for operation in ((edge.get("transfer") or {}).get("operations") or [])
                   if operation.get("kind") == "assign"]
    assumptions: dict[tuple[str, int], dict[str, Any]] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for operation in assignments:
            target = operation.get("target_symbol")
            if not target or not tainted.intersection(operation.get("reads") or []):
                continue
            ty = types.get(str(symbols.get(target) or "").upper(), {})
            width = int(ty.get("semantic_bit_width") or ty.get("bit_width") or 0)
            if ty.get("kind") == "integer" and 0 < width < pointer_width:
                source = operation.get("source") or {}
                key = (str(source.get("file") or ""), int(source.get("start_line") or 0))
                assumptions[key] = {
                    "kind": "pointer_integer_width",
                    "source": source,
                    "pointer_bits": pointer_width,
                    "integer_bits": width,
                    "target_symbol": target,
                    "status": "platform_assumption_model_incomplete",
                }
            if target not in tainted:
                tainted.add(target)
                changed = True
        if not changed:
            break
    return list(assumptions.values())


def audit_probe_completeness(run_dir: Path) -> dict[str, Any]:
    stg = run_dir / "stg"
    try:
        model = json.loads((stg / "stg-model.json").read_text(encoding="utf-8"))
        runtime = json.loads((stg / "stg-runtime-ids.json").read_text(encoding="utf-8"))
        ir = json.loads((stg / "stg-ir-mapping.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {"status": "FAILED", "reason": str(error), "risks": [],
                "platform_assumptions": []}
    runtime_by_node: dict[str, list[dict[str, Any]]] = {}
    for entry in runtime.get("entries", []):
        if entry.get("kind") == "hazard_violation":
            runtime_by_node.setdefault(str(entry.get("node_id")), []).append(entry)
    mapped = {entry.get("runtime_id") for entry in ir.get("mappings", [])
              if entry.get("mapping_kind") == "hazard_violation"}
    tasks_path = run_dir / "bundle" / "goal-tasks.json"
    task_runtime_ids: set[int] | None = None
    if tasks_path.is_file():
        try:
            task_document = json.loads(tasks_path.read_text(encoding="utf-8"))
            task_runtime_ids = {int((task.get("goal") or {})["runtime_id"])
                                for task in task_document.get("tasks", [])
                                if (task.get("goal") or {}).get("runtime_id") is not None}
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            task_runtime_ids = set()
    risks = []
    assumptions = []
    for pou in model.get("pous", []):
        environment = pou.get("environment") or {}
        types = {str(item.get("name") or "").upper(): item
                 for item in environment.get("types", [])}
        expressions = {item.get("id"): item for item in environment.get("expressions", [])}
        hazards: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for node in (pou.get("graph") or {}).get("nodes", []):
            hazard = node.get("hazard") or {}
            if hazard:
                hazards.setdefault((hazard.get("expression_id"), hazard.get("kind")), []).append(node)
        assumptions.extend(_platform_assumptions(pou, types))
        expected: dict[tuple[str, str], dict[str, Any] | None] = {}
        for expression in expressions.values():
            kind = _risk_kind(expression, expressions, types)
            if kind is not None:
                expected[(str(expression.get("id")), kind)] = expression.get("source")
        for edge in (pou.get("graph") or {}).get("edges", []):
            for operation in ((edge.get("transfer") or {}).get("operations") or []):
                conversion = operation.get("conversion") or {}
                if operation.get("kind") != "assign" or not conversion.get("potentially_narrowing"):
                    continue
                value = operation.get("value")
                if value:
                    expected.setdefault((str(value), "dangerous_conversion"), operation.get("source"))
        for (expression_id, kind), source in expected.items():
            nodes = hazards.get((expression_id, kind), [])
            targets = [target for node in nodes
                       for target in runtime_by_node.get(str(node.get("id")), [])]
            mapped_ids = [target.get("runtime_id") for target in targets
                          if target.get("runtime_id") in mapped]
            status = ("MISSING_SOURCE_TARGET" if not nodes else
                      "MISSING_RUNTIME_TARGET" if not targets else
                      "MISSING_PREDICATE" if any(target.get("violation_predicate") is None
                                                 for target in targets) else
                      "MISSING_IR_PROBE" if not mapped_ids else
                      "MISSING_SEARCH_TASK" if task_runtime_ids is not None
                      and not task_runtime_ids.intersection(mapped_ids) else "INSTRUMENTED")
            risks.append({
                "pou": (pou.get("pou") or {}).get("qualified_name"),
                "expression_id": expression_id,
                "kind": kind,
                "source": source,
                "status": status,
                "runtime_ids": [target.get("runtime_id") for target in targets],
                "mapped_runtime_ids": mapped_ids,
                "search_task_runtime_ids": sorted(task_runtime_ids.intersection(mapped_ids))
                if task_runtime_ids is not None else None,
            })
    incomplete = sum(item["status"] != "INSTRUMENTED" for item in risks)
    return {"status": "INCOMPLETE" if incomplete or assumptions else "COMPLETE",
            "risk_count": len(risks), "incomplete_count": incomplete,
            "risks": risks, "platform_assumptions": assumptions}
