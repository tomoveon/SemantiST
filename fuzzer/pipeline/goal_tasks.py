"""Build ``goal-tasks.json`` from the semantic task plan and ABI (spec 7.1)."""

from __future__ import annotations

import itertools
import json
import math
import struct
from pathlib import Path
from typing import Any

from fuzzer.pipeline import analysis
from fuzzer.corpus.frames import default_for_spec
from fuzzer.pipeline.model import type_bounds


MAX_PLANNED_PREFIXES_PER_TASK = 2


def cycle_effects(abi: dict[str, Any], stg_model: dict[str, Any] | None) -> dict[str, Any]:
    """Separate observed cross-frame effects from missing effect information."""
    reasons: set[str] = set()
    unknown: set[str] = set()
    target = abi.get("target") or {}
    if target.get("kind") == "FUNCTION_BLOCK":
        reasons.add("function_block_default")
    def pointer_store(operation: dict[str, Any]) -> bool:
        if operation.get("kind") != "assign":
            return False
        source = operation.get("source") or {}
        path = source.get("file")
        start, end = source.get("start_offset"), source.get("end_offset")
        if (not isinstance(path, str) or not path.lower().endswith(".st")
                or type(start) is not int or type(end) is not int
                or start < 0 or not start < end <= start + 4096):
            return False
        try:
            with Path(path).open("rb") as stream:
                stream.seek(start)
                statement = stream.read(end - start).decode("utf-8", errors="replace")
        except OSError:
            return False
        left, separator, _ = statement.partition(":=")
        return bool(separator and "^" in left)
    for entry in [*abi.get("frame_fields", []), *abi.get("shared_fields", []),
                  *abi.get("state_fields", [])]:
        if entry.get("block_kind") == "VAR_IN_OUT":
            reasons.add("var_in_out")
        if (entry.get("spec") or {}).get("kind") == "pointer":
            unknown.add("pointer_effect_unresolved")
        if entry.get("block_kind") in {"VAR", "VAR_GLOBAL", "VAR_EXTERNAL"}:
            reasons.add("persistent_or_shared_state")
    if any(entry.get("block_kind") == "VAR_OUTPUT" for entry in abi.get("shared_fields", [])):
        reasons.add("by_reference_output")
    if stg_model is None:
        unknown.add("stg_effects_unavailable")
    else:
        pous = stg_model.get("pous") or []
        by_name = {str(value).upper(): item for item in pous
                   for value in ((item.get("pou") or {}).get("name"),
                                 (item.get("pou") or {}).get("qualified_name")) if value}
        name = str(target.get("name") or "").upper()
        def inspect(name: str, visiting: set[str]) -> None:
            pou = by_name.get(name)
            if pou is None:
                unknown.add("callee_effects_unavailable")
                return
            if name in visiting:
                unknown.add("recursive_call_effects_unresolved")
                return
            visiting.add(name)
            if (pou.get("pou") or {}).get("kind") != "function":
                unknown.add("callee_instance_effects_unresolved")
            environment = pou.get("environment") or {}
            for edge in (pou.get("graph") or {}).get("edges") or []:
                for operation in (edge.get("transfer") or {}).get("operations") or []:
                    if pointer_store(operation):
                        reasons.add("pointer_write")
            for symbol in environment.get("symbols") or []:
                role = str(symbol.get("role") or "").lower()
                if role == "in_out" and name != str(target.get("name") or "").upper():
                    unknown.add("callee_inout_alias_unresolved")
                elif role in {"persistent_state", "in_out"} or (
                    role in {"global", "external"} and not symbol.get("constant")
                ):
                    reasons.add(role)
            for call in environment.get("calls") or []:
                domains = {str(value).lower() for value in call.get("effect_domains") or []}
                if domains.intersection({"clock", "io", "persistent_state", "shared_state"}):
                    reasons.add("stateful_or_io_call")
                if call.get("external"):
                    unknown.add("callee_effects_unavailable")
                else:
                    callee_name = str(call.get("callee") or "").upper()
                    if call.get("writes") and callee_name not in by_name:
                        unknown.add("callee_write_targets_unresolved")
                    inspect(callee_name, visiting)
            if (pou.get("pou") or {}).get("stateful"):
                reasons.add("stg_stateful")
            visiting.remove(name)
        if name not in by_name:
            unknown.add("target_effects_unavailable")
        else:
            inspect(name, set())
    mode = "multi_cycle" if reasons else "unknown" if unknown else "single_cycle"
    result = {"mode": mode, "reasons": sorted(reasons),
              "unknown_reasons": sorted(unknown)}
    if stg_model is not None and stg_model.get("effect_resolution"):
        result["effect_resolution"] = stg_model["effect_resolution"]
    return result


def _bounded_integer(spec: dict[str, Any], value: int) -> int:
    bounds = type_bounds(spec)
    if bounds is None:
        return value
    return max(bounds[0], min(bounds[1], value))


def _condition_value(spec: dict[str, Any], op: str, constant: int | float) -> str:
    bounds = type_bounds(spec)
    if op in ("<>", "!="):
        value = constant + 1 if bounds is None or constant + 1 <= bounds[1] else constant - 1
    else:
        value = constant + (1 if op == ">" else -1 if op == "<" else 0)
    if spec.get("name") in ("REAL", "LREAL"):
        return str(float(value))
    return str(_bounded_integer(spec, int(value)))


def _state_slice(abi: dict[str, Any], layout: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    harness = next(
        (region for region in layout.get("regions", []) if region.get("source") == "harness"),
        None,
    )
    offsets = {}
    if harness:
        for field in harness.get("fields", []):
            offsets[field["name"]] = field
    for entry in abi.get("state_fields", []):
        if entry.get("block_kind") not in {"VAR", "VAR_OUTPUT", "VAR_IN_OUT"}:
            continue
        name = entry["name"]
        spec = entry["spec"]
        if spec.get("kind") not in ("scalar", "array"):
            continue
        offset = entry.get("offset")
        region = "instance"
        if offset is None and harness:
            key = f"f{(entry.get('index') or 1) - 1}"
            field = offsets.get(key)
            offset = field["offset"] if field else None
            region = "semantist_harness_state"
        result.append(
            {
                "name": name,
                "block_kind": entry.get("block_kind"),
                "region": region,
                "offset": offset,
                "ctype": spec.get("c_type"),
            }
        )
    return result


def _timers(abi: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    timers = []
    for dependency in task.get("data_dependencies", []):
        type_name = str(dependency.get("type_name") or "").upper()
        if type_name in ("TON", "TOF", "TP", "TON_TIME", "TOF_TIME", "TP_TIME", "TON_LTIME", "TOF_LTIME", "TP_LTIME"):
            timers.append({"name": dependency.get("symbol"), "type": type_name, "pt_const_ns": None})
    return timers


def _conditions(expression: Any, *, negated: bool = False) -> list[dict[str, Any]]:
    if not isinstance(expression, dict):
        return []
    kind = analysis.kind(expression)
    operands = expression.get("operands") or []
    op = str(kind.get("operator") or "").upper()
    if kind.get("kind") == "unary" and op == "NOT" and operands:
        return _conditions(operands[0], negated=not negated)
    if kind.get("kind") == "binary" and op in ("AND", "OR"):
        parts = [_conditions(item, negated=negated) for item in operands]
        if (op == "AND") != negated:
            return [item for part in parts for item in part]
        common = {json.dumps(item, sort_keys=True) for item in parts[0]} if parts else set()
        for part in parts[1:]:
            common.intersection_update(json.dumps(item, sort_keys=True) for item in part)
        return [item for item in parts[0] if json.dumps(item, sort_keys=True) in common] if parts else []
    if kind.get("kind") == "binary" and op in ("=", "==", "<>", "!=", ">", ">=", "<", "<=") and len(operands) >= 2:
        left, right = operands[:2]
        field = analysis.member_name(left)
        constant = analysis.literal(right)
        if field is None and analysis.literal(left) is not None:
            field = analysis.member_name(right)
            constant = analysis.literal(left)
            op = {">": "<", ">=": "<=", "<": ">", "<=": ">="}.get(op, op)
        if negated:
            op = {"=": "<>", "==": "!=", "<>": "=", "!=": "==", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}[op]
        if field and isinstance(constant, (int, float)):
            return [{"lhs": {"field": field}, "op": op, "rhs": {"const": constant}}]
    if kind.get("kind") == "member_access" and expression.get("type_name") == "BOOL":
        field = analysis.member_name(expression)
        return [{"lhs": {"field": field}, "op": "=", "rhs": {"const": 0 if negated else 1}}] if field else []
    if kind.get("kind") == "list" and not negated:
        return [item for operand in operands for item in _conditions(operand)]
    return []


def _array_guard_values(expression: Any, specs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Encode concrete array elements from STG comparison bounds in one guard."""
    bounds: dict[tuple[str, int], dict[str, float]] = {}

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        kind = analysis.kind(node)
        operands = node.get("operands") or []
        if kind.get("kind") == "binary" and kind.get("operator") in ("<", "<=", ">", ">=") and len(operands) >= 2:
            left, right = operands[:2]
            constant = analysis.literal(right)
            op = kind["operator"]
            if constant is None:
                constant = analysis.literal(left)
                left = right
                op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[op]
            indexes = (left.get("operands") or []) if isinstance(left, dict) else []
            if (isinstance(left, dict) and analysis.kind(left).get("kind") == "array_index" and len(indexes) >= 2
                    and isinstance(constant, (int, float)) and math.isfinite(constant)):
                field = analysis.member_name(indexes[0])
                index = analysis.literal(indexes[1])
                spec = specs.get(field or "", {})
                if (spec.get("kind") == "array" and spec.get("element", {}).get("name") in ("REAL", "LREAL")
                        and isinstance(index, int) and 0 <= index < (spec.get("count") or 0)):
                    entry = bounds.setdefault((field, index), {})
                    key = "lower" if op.startswith(">") else "upper"
                    value = float(constant)
                    entry[key] = max(entry.get(key, -math.inf), value) if key == "lower" else min(entry.get(key, math.inf), value)
        for child in operands:
            visit(child)

    visit(expression)
    result: dict[str, str] = {}
    for field in sorted({name for name, _ in bounds}):
        spec = specs[field]
        element = spec["element"]
        width = element.get("byte_size") or element.get("size")
        total = spec.get("byte_size") or spec.get("size")
        if width not in (4, 8) or not isinstance(total, int) or total < spec["count"] * width:
            continue
        payload = bytearray(total)
        for (name, index), interval in bounds.items():
            if name != field:
                continue
            lower, upper = interval.get("lower"), interval.get("upper")
            if lower is not None and upper is not None:
                if lower >= upper:
                    continue
                value = (lower + upper) / 2
            elif lower is not None:
                value = lower + 1
            elif upper is not None:
                value = upper - 1
            else:
                continue
            struct.pack_into("<f" if width == 4 else "<d", payload, index * width, value)
        result[field] = "0x" + payload.hex()
    return result


def _persistent_array_boundary_prefixes(task: dict[str, Any], action_specs: dict[str, dict[str, Any]],
                                        defaults: dict[str, str], guard_frame: dict[str, str],
                                        state_update_reads: dict[str, set[str]] | None = None
                                        ) -> list[list[dict[str, str]]]:
    goal = task.get("terminal_goal_expression") or {}
    operands = goal.get("operands") or []
    if (task.get("terminal_hazard") != "array_access" or analysis.kind(goal).get("kind") != "array_index"
            or len(operands) < 2):
        return []
    index = operands[1]
    index_reads = set(index.get("reads") or [])
    persistent = {symbol for item in task.get("temporal_requirements") or []
                  for symbol in item.get("persistent_symbols") or []}
    if not index_reads.intersection(persistent):
        return []
    update_inputs = set().union(*(state_update_reads.get(symbol, set()) for symbol in index_reads)) \
        if state_update_reads is not None else None
    array_field = analysis.member_name(operands[0])
    array_entry = next((entry for entry in task.get("controllable_seed_fields") or []
                        if entry.get("seed_field") == array_field
                        and entry.get("type", {}).get("kind") == "array"), None)
    if array_entry is None:
        return []
    dimensions = array_entry.get("type", {}).get("dimensions") or []
    upper = dimensions[0].get("upper") if len(dimensions) == 1 else None
    count = action_specs.get(array_field, {}).get("count")
    if not isinstance(upper, int) or not isinstance(count, int) or count <= 0:
        return []
    index_type = index.get("type_name")
    dependencies = {entry.get("symbol") for entry in task.get("data_dependencies") or []}
    controls = [entry["seed_field"] for entry in task.get("controllable_seed_fields") or []
                if entry.get("role") == "input" and entry.get("symbol") in dependencies
                and (update_inputs is None or entry.get("symbol") in update_inputs)
                and entry.get("type_name") == index_type
                and entry.get("seed_field") in action_specs
                and action_specs[entry["seed_field"]].get("kind") == "scalar"
                and _bounded_integer(action_specs[entry["seed_field"]], upper + 1) == upper + 1]
    switches = [entry["seed_field"] for entry in task.get("controllable_seed_fields") or []
                if entry.get("role") == "input" and entry.get("symbol") in dependencies
                and entry.get("type_name") == "BOOL"
                and entry.get("seed_field") in action_specs][:3]
    sequences = []
    for control in controls[:2]:
        for values in itertools.product(("0", "1"), repeat=len(switches)):
            active = {**guard_frame, control: str(upper + 1), **dict(zip(switches, values))}
            sequences.append([dict(defaults)] + [dict(active) for _ in range(min(299, count + 3))])
    return sequences


def _planned_prefixes(abi: dict[str, Any], task: dict[str, Any], conditions_in: list[dict[str, Any]],
                      global_literals: list[int] | None = None,
                      state_update_reads: dict[str, set[str]] | None = None) -> list[list[dict[str, str]]]:
    action_entries = list(abi.get("frame_fields", []))
    seen = {entry.get("name") for entry in action_entries}
    action_entries.extend(entry for entry in abi.get("shared_fields", [])
                          if entry.get("block_kind") == "VAR_IN_OUT" and entry.get("name") not in seen)
    input_names = [entry["name"] for entry in action_entries]
    input_name_set = set(input_names)
    defaults = {entry["name"]: default_for_spec(entry["spec"]) for entry in action_entries}
    action_specs = {entry["name"]: entry["spec"] for entry in action_entries}
    sequences: list[list[dict[str, str]]] = []
    conditions = []
    for condition in conditions_in:
        field = (condition.get("lhs") or {}).get("field")
        constant = (condition.get("rhs") or {}).get("const")
        if (field in input_name_set and action_specs[field].get("kind") == "scalar"
                and isinstance(constant, (int, float))):
            conditions.append((field, condition.get("op"), constant))
    if conditions:
        frame = dict(defaults)
        for field, op, constant in conditions:
            frame[field] = _condition_value(action_specs[field], op, constant)
        sequences.append([dict(defaults), frame, dict(frame), dict(frame), dict(frame)])
    guard_frame = dict(defaults)
    for field, op, constant in conditions:
        guard_frame[field] = _condition_value(action_specs[field], op, constant)
    sequences.extend(_persistent_array_boundary_prefixes(
        task, action_specs, defaults, guard_frame, state_update_reads))
    guard_scalar_fields = [name for name, spec in action_specs.items()
                           if spec.get("kind") == "scalar" and spec.get("name") not in ("REAL", "LREAL")]
    guard_literals = sorted(value for value in analysis.collect_literals(task)
                            if isinstance(value, int) and not isinstance(value, bool))
    for obligation in task.get("control_obligations") or []:
        values = _array_guard_values(obligation.get("goal_expression"), action_specs)
        if values:
            active = {**guard_frame, **values}
            array_lengths = [int(action_specs[field].get("count") or 0)
                             for field in values if action_specs[field].get("kind") == "array"]
            hold_frames = min(299, max(4, max(array_lengths, default=0) + 2))
            sequences.append([dict(defaults)] + [dict(active) for _ in range(hold_frames)])
            for field in guard_scalar_fields[:4]:
                bounds = type_bounds(action_specs[field])
                candidates = (value for value in guard_literals
                              if bounds is None or bounds[0] <= value <= bounds[1])
                for value in itertools.islice((value for value in candidates
                                              if str(value) != active[field]), 4):
                    variant = {**active, field: str(value)}
                    sequences.append([dict(defaults)] + [dict(variant) for _ in range(hold_frames)])
    literal_values = sorted(
        {
            int(value)
            for value in analysis.collect_literals(task)
            if isinstance(value, int)
        }
    )
    for field in input_names:
        spec = next(
            (entry["spec"] for entry in action_entries if entry["name"] == field),
            {},
        )
        if spec.get("kind") != "scalar" or spec.get("name") in ("BOOL", "REAL", "LREAL"):
            continue
        values = []
        for entry in task.get("abstraction") or []:
            if entry.get("field") == field:
                values.extend(entry.get("thresholds") or [])
        values.extend(literal_values)
        bounded = sorted(set(values))
        if (len(bounded) >= 2
                and all(isinstance(value, int) and not isinstance(value, bool) for value in bounded)
                and bounded[-1] - bounded[0] <= 128):
            values.extend(range(bounded[0], bounded[-1] + 1))
        bounds = type_bounds(spec)
        in_domain = (value for value in sorted(set(values))
                     if bounds is None or bounds[0] <= value <= bounds[1])
        for value in itertools.islice(in_domain, 128):
            frame = dict(defaults)
            frame[field] = str(value)
            sequences.append([frame, dict(frame)])
    # Generic write/edge pairs: a two-operation protocol with one integer
    # payload.  Names come only from ABI fields; values from STG literals.
    boolean_fields = [
        entry["name"] for entry in action_entries
        if entry["spec"].get("name") == "BOOL"
    ][:6]
    integer_fields = [
        entry["name"] for entry in action_entries
        if entry["spec"].get("kind") == "scalar"
        and entry["spec"].get("name") not in ("BOOL", "REAL", "LREAL")
    ]
    payload_values = set(global_literals or [])
    for value in list(payload_values):
        payload_values.update((value - 1, value + 1))
    payloads = sorted({int(value) for value in literal_values if isinstance(value, int)} | payload_values)
    for first, second in itertools.combinations(boolean_fields, 2):
        for payload in payloads[:4]:
            low = dict(defaults)
            write = dict(low)
            write[first] = "1"
            if integer_fields:
                write[integer_fields[0]] = str(_bounded_integer(action_specs[integer_fields[0]], payload))
            take = dict(low)
            take[second] = "1"
            sequence = [dict(low), write, dict(low), take] * 3 + [dict(low), write, take]
            sequences.append(sequence)
    # Generic fill/take/drain/ack protocol: assign four distinct boolean roles
    # by combination, not by field name, and use the same integer payload for
    # every integer field.  This covers queue-like state machines without a
    # suite template.
    for gate in boolean_fields:
        remaining = [name for name in boolean_fields if name != gate]
        for roles in itertools.permutations(remaining, 4):
            for payload in payloads[-2:]:
                fill, take, start, ack = roles
                low = dict(defaults)
                low[gate] = "1"
                for name in integer_fields:
                    low[name] = str(_bounded_integer(action_specs[name], payload))
                frames = [dict(low)]
                for _ in range(4):
                    high = dict(low)
                    high[fill] = "1"
                    frames.extend([dict(low), high])
                for _ in range(3):
                    high = dict(low)
                    high[take] = "1"
                    frames.extend([dict(low), high])
                high = dict(low)
                high[fill] = "1"
                frames.extend([dict(low), high])
                high = dict(low)
                high[start] = "1"
                frames.extend([dict(low), high])
                ack_high = dict(low)
                ack_high[ack] = "1"
                frames.extend([dict(low), ack_high])
                frames.extend([dict(low), ack_high])
                sequences.append(frames)
    if task.get("timers") and sequences:
        last = sequences[-1][-1]
        sequences[-1].extend(dict(last) for _ in range(210))
    return sequences


def build_goal_tasks(
    plan: dict[str, Any],
    abi: dict[str, Any],
    layout: dict[str, Any],
    *,
    contract_id: str | None = None,
    runtime_index: dict[int, dict[str, Any]] | None = None,
    stg_model: dict[str, Any] | None = None,
    effect_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effects = cycle_effects(abi, effect_model if effect_model is not None else stg_model)
    if any(task.get("temporal_requirements") for task in plan.get("tasks", [])):
        effects["mode"] = "multi_cycle"
        effects["reasons"] = sorted(set(effects["reasons"]) | {"temporal_requirement"})
    single_cycle = effects["mode"] == "single_cycle"
    one_frame_first = effects["mode"] != "multi_cycle"
    action_entries = list(abi.get("frame_fields", []))
    seen = {entry.get("name") for entry in action_entries}
    action_entries.extend(entry for entry in abi.get("shared_fields", [])
                          if entry.get("block_kind") == "VAR_IN_OUT" and entry.get("name") not in seen)
    inputs = [entry["name"] for entry in action_entries]
    state_slice = _state_slice(abi, layout)
    global_literals = sorted({
        int(value)
        for plan_task in plan.get("tasks", [])
        for value in analysis.collect_literals(plan_task)
        if isinstance(value, int)
    })
    state_update_reads: dict[str, set[str]] | None = None
    if stg_model is not None:
        state_update_reads = {}
        for pou in stg_model.get("pous") or []:
            for edge in (pou.get("graph") or {}).get("edges") or []:
                for operation in (edge.get("transfer") or {}).get("operations") or []:
                    target = operation.get("target_symbol")
                    if operation.get("kind") == "assign" and isinstance(target, str):
                        state_update_reads.setdefault(target, set()).update(operation.get("reads") or [])
    tasks = []
    for task in plan.get("tasks", []):
        kind = task.get("terminal_kind")
        hazard = task.get("terminal_hazard")
        runtime_id = task.get("terminal_runtime_id")
        runtime_modeling = task.get("modeling_status")
        if runtime_index is not None and runtime_id is not None:
            entry = runtime_index.get(int(runtime_id))
            if not isinstance(entry, dict) or not isinstance(entry.get("modeling"), str):
                raise ValueError(f"runtime ID {runtime_id} missing modeling status")
            runtime_modeling = entry["modeling"]
        reach_runtime_ids = []
        if runtime_index is not None and runtime_id is not None:
            goal_entry = runtime_index.get(int(runtime_id), {})
            reach_runtime_ids = sorted(
                int(identifier) for identifier, entry in runtime_index.items()
                if entry.get("kind") == "hazard_reach"
                and entry.get("node_id") == goal_entry.get("node_id")
                and entry.get("hazard") == goal_entry.get("hazard")
            )
        goal_class = {
            "hazard_violation": "defect",
            "branch": "branch",
            "cycle": "cycle",
            "output": "output",
            "exploration": "exploration",
        }.get(str(kind), "exploration")
        if hazard == "arithmetic_boundary":
            goal_class = "exploration"
        objective = hazard in ("array_access", "pointer_access", "division", "modulo", "dangerous_conversion")
        requires_history = not one_frame_first or bool(task.get("temporal_requirements"))
        edge_inputs = []
        for entry in action_entries:
            if entry["spec"].get("name") == "BOOL":
                edge_inputs.append(entry["name"])
        thresholds: list[dict[str, Any]] = []
        for name in inputs:
            values = sorted(
                {
                    int(value)
                    for value in analysis.collect_literals(task)
                    if isinstance(value, int)
                }
            )[:8]
            if values:
                thresholds.append({"field": name, "thresholds": values})
        task_conditions = []
        for obligation in task.get("control_obligations") or []:
            if isinstance(obligation, dict):
                task_conditions.extend(_conditions(obligation.get("goal_expression")))
        task_conditions.extend(_conditions(task.get("terminal_goal_expression")))
        deduped = []
        seen_conditions = set()
        for condition in task_conditions:
            key = (condition["lhs"]["field"], condition["op"], condition["rhs"]["const"])
            if key not in seen_conditions:
                seen_conditions.add(key)
                deduped.append(condition)
        candidate_paths = []
        for path in task.get("candidate_paths", []):
            edges = set(path.get("edge_ids") or [])
            conditions = []
            for obligation in task.get("control_obligations") or []:
                if obligation.get("evaluation_edge_id") in edges:
                    conditions.extend(_conditions(obligation.get("goal_expression")))
            conditions.extend(_conditions(task.get("terminal_goal_expression")))
            path_conditions = list({
                (item["lhs"]["field"], item["op"], item["rhs"]["const"]): item
                for item in conditions
            }.values())
            candidate_paths.append({"path_id": path.get("path_id"),
                                    "conditions": path_conditions if edges else deduped})
        boundary = None
        if hazard == "array_access" and runtime_index is not None and runtime_id is not None:
            predicate = runtime_index.get(int(runtime_id), {}).get("violation_predicate") or {}
            dimensions = predicate.get("dimensions") or []
            operands = (task.get("terminal_goal_expression") or {}).get("operands") or []
            index_field = analysis.member_name(operands[1]) if len(operands) > 1 else None
            if (len(dimensions) == 1 and index_field
                    and isinstance(dimensions[0].get("upper"), int)
                    and isinstance(dimensions[0].get("lower"), int)):
                boundary = {"field": index_field, "lower": dimensions[0]["lower"],
                            "upper": dimensions[0]["upper"],
                            "lower_guarded": any(
                                condition.get("lhs", {}).get("field") == index_field
                                and condition.get("op") in (">=", ">")
                                and condition.get("rhs", {}).get("const") >= dimensions[0]["lower"]
                                for path in candidate_paths
                                for condition in path.get("conditions") or []
                                if isinstance(condition.get("rhs", {}).get("const"), (int, float)))}
        tasks.append(
            {
                "task_id": task.get("task_id"),
                "goal": {
                    "runtime_id": runtime_id,
                    "stable_id": task.get("terminal_target_id"),
                    "kind": kind,
                    "hazard": hazard,
                    "modeling": runtime_modeling,
                    "source": (task.get("terminal_goal_expression") or {}).get("source"),
                },
                "plan_modeling_status": task.get("modeling_status"),
                "goal_class": goal_class,
                "reach_runtime_ids": reach_runtime_ids,
                "polarity": task.get("polarity") or (task.get("terminal_goal_expression") or {}).get("polarity"),
                "requires_history": requires_history,
                "controllable_fields": inputs,
                "state_slice": state_slice,
                "candidate_paths": candidate_paths,
                "array_boundary": boundary,
                "abstraction": thresholds,
                "edge_inputs": edge_inputs,
                "timers": _timers(abi, task),
                "symbolic_suitable": bool(objective and hazard in ("array_access", "pointer_access", "division", "modulo", "dangerous_conversion")),
                "budget": {"attempts": 2000, "solver_queries": 20, "llm_calls": 2},
                "literals": sorted(analysis.collect_literals(task)),
                "seed_prefixes": [sequence[-1:] if one_frame_first else sequence
                                  for sequence in _planned_prefixes(
                                      abi, task, deduped, global_literals, state_update_reads
                                  )[:MAX_PLANNED_PREFIXES_PER_TASK]],
            }
        )
    # A generic combined mode prefix: every frame-input condition from the
    # STG is applied together once, then held.  This is not a suite template;
    # the fields/values come only from goal-tasks conditions.
    defaults = {entry["name"]: default_for_spec(entry["spec"]) for entry in action_entries}
    action_specs = {entry["name"]: entry["spec"] for entry in action_entries}
    combined = dict(defaults)
    for task in tasks:
        for path in task.get("candidate_paths") or []:
            for condition in path.get("conditions") or []:
                field = (condition.get("lhs") or {}).get("field")
                constant = (condition.get("rhs") or {}).get("const")
                if (field in combined and action_specs[field].get("kind") == "scalar"
                        and isinstance(constant, (int, float))):
                    combined[field] = _condition_value(action_specs[field], condition.get("op"), constant)
    global_tasks = [task for task in tasks if (task.get("goal") or {}).get("hazard") == "array_access"] or [
        task for task in tasks if task.get("goal_class") == "defect"
    ][:1]
    boolean_fields = [entry["name"] for entry in action_entries
                      if entry["spec"].get("name") == "BOOL"][:6]
    has_timers = any(task.get("timers") for task in tasks)
    for task in global_tasks:
        prefixes = task["seed_prefixes"]
        added = 0
        while len(prefixes) < MAX_PLANNED_PREFIXES_PER_TASK:
            frame = dict(combined)
            if added == 1 and boolean_fields:
                frame[boolean_fields[0]] = "1"
            else:
                for name in boolean_fields:
                    frame[name] = "0"
            sequence = [dict(frame)] if one_frame_first else [dict(defaults)] + [dict(frame) for _ in range(4)]
            if has_timers and not one_frame_first:
                sequence.extend(dict(frame) for _ in range(210))
            prefixes.append(sequence)
            added += 1
    return {
        "schema_version": "semantist.goal-tasks/1.0.0",
        "contract_id": contract_id,
        "target": abi.get("target"),
        "cycle_effects": effects,
        "single_cycle": single_cycle,
        "unknown_cycle_effects": effects["mode"] == "unknown",
        "tasks": tasks,
    }


def write_goal_tasks(
    plan_path: Path,
    abi_path: Path,
    layout_path: Path,
    out_path: Path,
    *,
    contract_id: str | None = None,
    runtime_ids_path: Path | None = None,
    stg_model_path: Path | None = None,
    stg_effect_model_path: Path | None = None,
) -> dict[str, Any]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    abi = json.loads(Path(abi_path).read_text(encoding="utf-8"))
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    stg_model = json.loads(Path(stg_model_path).read_text(encoding="utf-8")) \
        if stg_model_path is not None else None
    effect_model = json.loads(Path(stg_effect_model_path).read_text(encoding="utf-8")) \
        if stg_effect_model_path is not None and Path(stg_effect_model_path).is_file() else None
    runtime_index = None
    if runtime_ids_path is not None:
        from .probe import load_runtime_index

        runtime_index = load_runtime_index(runtime_ids_path)
    document = build_goal_tasks(plan, abi, layout, contract_id=contract_id,
                                runtime_index=runtime_index, stg_model=stg_model,
                                effect_model=effect_model)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document
