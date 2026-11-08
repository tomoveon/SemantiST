"""Plan schema validation and expansion (spec sections 9.2-9.3)."""

from __future__ import annotations

import json
import re
import struct
from typing import Any

from compiler.harness.abi import TargetAbi, frame_action_params
from compiler.harness.contract import POINTER_STORAGE_CAPACITY
from fuzzer.corpus.frames import default_for_spec, structured_default_for_spec

OPS = {"set", "pulse", "hold", "wait", "repeat"}
INT_TYPES = {"SINT", "USINT", "BYTE", "CHAR", "INT", "UINT", "WORD", "WCHAR", "DINT", "UDINT", "DWORD",
             "LINT", "ULINT", "LWORD", "TIME", "LTIME", "DATE", "DT", "TOD"}
MAX_FRAMES = 512
MAX_REPEAT_DEPTH = 8
PLAN_KEYS = {
    "plan", "steps", "sequence", "actions", "plan_id", "task_id", "contract_id", "intent",
    "evidence_refs", "start_from", "suggested_symbolic_fields", "priority", "prompt_version",
    "solver_triggers",
}
SOLVER_TRIGGERS = {"task_distance_improved", "native_hazard_reach", "state_frontier", "task_stagnation"}


class PlanRejected(ValueError):
    pass


def _normalize_value(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def normalize_plan(plan: dict[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Accept the common `action/field/value` spelling as well as `op/inputs`."""
    if not isinstance(plan, dict):
        return plan
    if _depth > MAX_REPEAT_DEPTH:
        raise PlanRejected(f"repeat nesting exceeds {MAX_REPEAT_DEPTH}")
    for key in ("plan", "steps", "sequence"):
        if key in plan and (isinstance(plan[key], list) or isinstance(plan[key], dict)):
            nested = plan[key]
            if isinstance(nested, dict) and "actions" in nested:
                # Preserve request metadata while accepting a wrapper such as
                # {"plan": {"actions": [...]}}. Otherwise task/contract
                # checks would run against the inner object only.
                outer_metadata = {
                    name: plan[name]
                    for name in ("plan_id", "task_id", "contract_id", "prompt_version")
                    if name in plan
                }
                plan = {**plan, **nested, **outer_metadata}
            elif isinstance(nested, list):
                plan = {**plan, "actions": nested}
            break
    actions = []
    for raw in plan.get("actions") or []:
        if not isinstance(raw, dict):
            actions.append(raw)
            continue
        action = dict(raw)
        op = action.get("op") or action.get("action")
        action["op"] = op
        action.pop("action", None)
        if op in ("set", "hold"):
            inputs = action.get("inputs")
            if not isinstance(inputs, dict):
                inputs = {}
            if "field" in action and "value" in action:
                inputs[str(action["field"])] = _normalize_value(action["value"])
            action["inputs"] = {name: _normalize_value(value) for name, value in inputs.items()}
            action.pop("field", None)
            action.pop("value", None)
            frames = action.get("frames")
            if frames is None:
                frames = action.get("duration")
            if isinstance(frames, dict):
                frames = frames.get("cycles", 1)
            action["frames"] = frames if frames is not None else 1
            action.pop("duration", None)
        elif op == "pulse":
            if "input" not in action and "field" in action:
                action["input"] = action["field"]
            if "input" not in action and isinstance(action.get("inputs"), dict) and action["inputs"]:
                action["input"] = next(iter(action["inputs"]))
            action.pop("field", None)
            action.pop("value", None)
            action.pop("inputs", None)
        elif op == "wait":
            frames = action.get("frames")
            if frames is None:
                frames = action.get("duration")
            if isinstance(frames, dict):
                frames = frames.get("cycles", 1)
            if frames is None and isinstance(action.get("inputs"), dict):
                frames = action["inputs"].get("cycles", 1)
            action["frames"] = frames if frames is not None else 1
            action.pop("duration", None)
            action.pop("inputs", None)
        elif op == "repeat":
            if "times" not in action and "count" in action:
                action["times"] = action["count"]
            action.pop("count", None)
            action["actions"] = normalize_plan(
                {"actions": action.get("actions") or []}, _depth=_depth + 1
            ).get("actions", [])
        actions.append(action)
    normalized = dict(plan)
    normalized["actions"] = actions
    return normalized


def _field_map(abi: dict[str, Any] | TargetAbi) -> dict[str, dict[str, Any]]:
    """Return the fields that a plan may change on each frame.

    ``VAR_IN_OUT`` backing storage is a frame action even though it is also
    represented in ``shared_fields``. Constant shared inputs remain excluded.
    Keep this conversion local so old exchange JSON continues to work without
    introducing a second schema validator.
    """
    if isinstance(abi, TargetAbi):
        return {
            param.name: {"name": param.name, "spec": {
                "kind": param.spec.kind, "name": param.spec.name,
                "size": param.spec.size, "c_type": param.spec.c_type,
                "length": param.spec.length, "count": param.spec.count,
                "byte_size": param.spec.byte_size(),
            }}
            for param in frame_action_params(abi)
        }
    entries = list(abi.get("frame_fields", []))
    seen = {entry.get("name") for entry in entries}
    entries.extend(entry for entry in abi.get("shared_fields", [])
                   if entry.get("block_kind") == "VAR_IN_OUT" and entry.get("name") not in seen)
    result = {entry["name"]: entry for entry in entries
              if isinstance(entry, dict) and entry.get("name")}
    for entry in list(result.values()):
        spec = entry.get("spec") or {}
        _add_member_fields(result, entry["name"], spec)
    return result


def _add_member_fields(result: dict[str, dict[str, Any]], prefix: str,
                       spec: dict[str, Any], base_offset: int = 0) -> None:
    """Expose opaque members as plan fields while retaining the parent ABI field."""
    for member in spec.get("members") or []:
        name = str(member.get("name") or "")
        child = member.get("spec") or {}
        if not name or not isinstance(child, dict):
            continue
        path = f"{prefix}.{name}"
        offset = base_offset + int(member.get("offset") or 0)
        result[path] = {"name": path, "spec": child,
                        "_parent": prefix.split(".", 1)[0],
                        "_offset": offset}
        _add_member_fields(result, path, child, offset)


def _aggregate_member_layout(abi: dict[str, Any]) -> dict[str, tuple[str, int, dict[str, Any]]]:
    fields = _field_map(abi)
    return {
        name: (entry["_parent"], int(entry["_offset"]), entry["spec"])
        for name, entry in fields.items() if "_parent" in entry
    }


def _scalar_signed(spec: dict[str, Any]) -> bool:
    name = str(spec.get("name") or "")
    c_type = str(spec.get("c_type") or "")
    return not (c_type.startswith("u") or name in {
        "USINT", "BYTE", "CHAR", "UINT", "WORD", "WCHAR",
        "UDINT", "DWORD", "ULINT", "LWORD",
    })


def _member_bytes(spec: dict[str, Any], value: Any) -> bytes:
    kind = spec.get("kind")
    size = int(spec.get("byte_size") or spec.get("size") or 0)
    if isinstance(value, str) and value.lower().startswith("0x"):
        digits = value[2:]
        if len(digits) % 2 or re.fullmatch(r"[0-9a-fA-F]*", digits) is None:
            raise PlanRejected("aggregate member has invalid hexadecimal bytes")
        data = bytes.fromhex(digits)
    elif kind == "scalar":
        if spec.get("name") in {"REAL", "LREAL"}:
            data = struct.pack("<f" if spec.get("name") == "REAL" else "<d", float(value))
        else:
            number = int(value)
            data = number.to_bytes(size, "little", signed=_scalar_signed(spec))
    elif kind == "string":
        text = str(value)
        data = (text.encode("utf-16-le") if spec.get("name") == "WSTRING"
                else text.encode("utf-8"))
    else:
        raise PlanRejected(f"aggregate member {spec.get('name') or kind!r} requires 0x bytes")
    if size and len(data) > size:
        raise PlanRejected(f"aggregate member bytes exceed {size}")
    return data + b"\x00" * max(0, size - len(data))


def _expand_aggregate_inputs(inputs: dict[str, Any], abi: dict[str, Any]) -> dict[str, Any]:
    """Merge member assignments into fixed-size parent raw-byte fields."""
    layout = _aggregate_member_layout(abi)
    if not layout:
        return dict(inputs)
    top_specs = {entry["name"]: entry.get("spec") or {}
                 for entry in list(abi.get("frame_fields", [])) + list(abi.get("shared_fields", []))
                 if isinstance(entry, dict) and entry.get("name")}
    expanded = dict(inputs)
    buffers: dict[str, bytearray] = {}
    for parent, spec in top_specs.items():
        if not (spec.get("kind") in {"opaque", "array"} and spec.get("byte_size")):
            continue
        raw = inputs.get(parent)
        if isinstance(raw, str) and raw.lower().startswith("0x"):
            digits = raw[2:]
            if len(digits) % 2 or re.fullmatch(r"[0-9a-fA-F]*", digits) is None:
                raise PlanRejected(f"{parent} has invalid hexadecimal bytes")
            initial = bytes.fromhex(digits)
        else:
            # A member-only plan gets a structural starting state so
            # protocol guards remain reachable. An explicit parent value,
            # including 0x00, still has exact precedence.
            seeded = structured_default_for_spec(parent, spec)
            initial = bytes.fromhex(seeded[2:]) if seeded.lower().startswith("0x") else b""
        capacity = int(spec.get("byte_size") or spec.get("size") or 0)
        if len(initial) > capacity:
            raise PlanRejected(f"{parent} bytes exceed {capacity}")
        buffers[parent] = bytearray(initial + b"\x00" * (capacity - len(initial)))
    for name, value in inputs.items():
        if name not in layout:
            continue
        parent, offset, spec = layout[name]
        # XML_CONTROL starts a parse cycle only when COMMAND bit 15 is set.
        # Preserve the planner's command bits while carrying that structural
        # enable bit into the aggregate bytes; later raw mutation may clear or
        # flip it deliberately.
        if (name.upper().startswith("CTRL.") and name.upper().endswith(".COMMAND")
                and spec.get("kind") == "scalar" and not (
                    isinstance(value, str) and value.lower().startswith("0x"))):
            value = int(value) | 0x8000
        data = _member_bytes(spec, value)
        buffer = buffers.get(parent)
        if buffer is None or offset + len(data) > len(buffer):
            raise PlanRejected(f"aggregate member {name!r} exceeds parent storage")
        buffer[offset:offset + len(data)] = data
    for parent, buffer in buffers.items():
        if any(name == parent or name.startswith(parent + ".") for name in inputs):
            expanded[parent] = "0x" + bytes(buffer).hex()
    for name in list(expanded):
        if name in layout:
            expanded.pop(name, None)
    return expanded


def _value_ok(spec: dict[str, Any], value: Any) -> bool:
    kind = spec.get("kind")
    if isinstance(value, str) and value.startswith(("0x", "0X")):
        digits = value[2:]
        if len(digits) % 2 or re.fullmatch(r"[0-9a-fA-F]*", digits) is None:
            return False
        count = len(digits) // 2
        if kind == "string":
            unit = 2 if spec.get("name") == "WSTRING" else 1
            capacity = spec.get("length") or spec.get("capacity")
            return count % unit == 0 and (capacity is None or count <= int(capacity) * unit)
        if kind == "scalar" and count == int(spec.get("size") or 1):
            return True
        if kind in {"array", "pointer", "opaque"}:
            capacity = POINTER_STORAGE_CAPACITY if kind == "pointer" else int(spec.get("byte_size") or spec.get("size") or 0)
            return not capacity or count <= capacity
    if kind == "string":
        if not isinstance(value, str):
            return False
        capacity = spec.get("length") or spec.get("capacity")
        units = len(value.encode("utf-16le")) // 2 if spec.get("name") == "WSTRING" else len(value.encode("utf-8"))
        return capacity is None or units <= int(capacity)
    if kind == "pointer":
        return isinstance(value, str) and (value == "" or value.startswith(("0x", "0X")))
    if kind == "array":
        if not isinstance(value, str) or not value.lower().startswith("0x"):
            return False
        digits = value[2:]
        byte_size = int(spec.get("byte_size") or spec.get("size") or 0)
        return (len(digits) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]*", digits) is not None
                and (not byte_size or len(digits) // 2 <= byte_size))
    if kind != "scalar":
        return isinstance(value, (str, int, float)) and not isinstance(value, bool)
    name = str(spec.get("name"))
    if name == "BOOL":
        return value in (0, 1, True, False)
    if name in ("REAL", "LREAL"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    bits = int(spec.get("size") or 1) * 8
    c_type = str(spec.get("c_type") or "")
    unsigned = c_type.startswith("u") or name in {
        "USINT", "BYTE", "UINT", "WORD", "UDINT", "DWORD", "ULINT", "LWORD",
    }
    if unsigned:
        return 0 <= value <= 2**bits - 1
    return -(2 ** (bits - 1)) <= value <= 2 ** (bits - 1) - 1


def validate_plan(
    plan: dict[str, Any],
    *,
    abi: dict[str, Any],
    contract_id: str | None,
    task_id: str | None = None,
    abstraction_fields: set[str] | None = None,
    allowed_evidence_refs: set[str] | None = None,
    reachable_prefix_ids: set[str] | None = None,
    max_frames: int = MAX_FRAMES,
) -> dict[str, Any]:
    plan = normalize_plan(plan)
    reasons: list[str] = []
    if not isinstance(plan, dict):
        raise PlanRejected("plan is not a JSON object")
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        reasons.append("plan_id is required")
    # The contract id is provenance metadata.  It is deliberately not treated
    # as a hash or identity gate: native cold replay and the normalized
    # execution bundle establish whether a candidate is usable.
    if contract_id and (not isinstance(plan.get("contract_id"), str)
                        or not plan.get("contract_id", "").strip()):
        reasons.append("contract_id is required")
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        reasons.append("actions must be a non-empty list")
        actions = []
    unknown_plan = set(plan) - PLAN_KEYS
    if unknown_plan:
        reasons.append(f"unknown plan fields {sorted(unknown_plan)}")
    if task_id is not None and plan.get("task_id") != task_id:
        reasons.append("task_id is required and must match")
    priority = plan.get("priority")
    if priority is not None and (type(priority) is not int or not 1 <= priority <= 100):
        reasons.append("priority must be an integer in 1..100")
    triggers = plan.get("solver_triggers", [])
    if (not isinstance(triggers, list) or len(triggers) > len(SOLVER_TRIGGERS)
            or any(not isinstance(item, str) or item not in SOLVER_TRIGGERS
                   for item in triggers)
            or any(triggers.count(item) > 1 for item in triggers if isinstance(item, str))):
        reasons.append("solver_triggers must be distinct supported native events")
    fields = _field_map(abi)
    output_evidence_refs = plan.get("evidence_refs", [])
    if output_evidence_refs is not None and (not isinstance(output_evidence_refs, list)
                                             or any(not isinstance(item, str) for item in output_evidence_refs)):
        reasons.append("evidence_refs must be a list of strings")
    elif allowed_evidence_refs is not None and any(item not in allowed_evidence_refs
                                                   for item in output_evidence_refs):
        reasons.append("evidence_refs contains an id not supplied by the request")
    suggested = plan.get("suggested_symbolic_fields", [])
    if suggested is not None and (not isinstance(suggested, list)
                                  or any(not isinstance(item, str) for item in suggested)):
        reasons.append("suggested_symbolic_fields must be a list of strings")
    elif isinstance(suggested, list):
        for name in suggested:
            if name not in fields:
                reasons.append(f"suggested symbolic field {name!r} is not controllable")
    total = 0

    def integer(value: Any, label: str, minimum: int = 1) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            reasons.append(f"{label} must be an integer >= {minimum}")
            return None
        return value

    def check_action(action: Any) -> int:
        if not isinstance(action, dict):
            reasons.append("action is not an object")
            return 0
        op = action.get("op")
        if op not in OPS:
            reasons.append(f"unknown op {op!r}")
            return 0
        unknown = set(action) - {"op", "inputs", "input", "frames", "times", "actions"}
        if unknown:
            reasons.append(f"unknown fields {sorted(unknown)}")
        cost = 0
        if op in ("set", "hold"):
            inputs = action.get("inputs", {})
            if not isinstance(inputs, dict):
                reasons.append(f"{op} inputs must be an object")
                return 0
            for name, value in inputs.items():
                entry = fields.get(name)
                if entry is None:
                    reasons.append(f"unknown input {name!r}")
                elif not _value_ok(entry["spec"], value):
                    reasons.append(f"{name} value outside its type domain")
            try:
                _expand_aggregate_inputs(inputs, abi)
            except PlanRejected as error:
                reasons.append(str(error))
            if op == "set":
                if action.get("frames", 1) != 1:
                    reasons.append("set frames must be 1")
                cost = 1
            else:
                frames = integer(action.get("frames"), "hold frames")
                cost = frames or 0
        elif op == "pulse":
            name = action.get("input")
            entry = fields.get(name)
            if entry is None:
                reasons.append(f"unknown pulse input {name!r}")
            elif entry["spec"].get("name") != "BOOL":
                reasons.append(f"pulse input {name!r} must be BOOL")
            cost = 2
        elif op == "wait":
            frames = integer(action.get("frames"), "wait frames")
            cost = frames or 0
        elif op == "repeat":
            times = integer(action.get("times"), "repeat times")
            if times is not None and times > 64:
                reasons.append("repeat times outside 1..64")
            nested = action.get("actions")
            if not isinstance(nested, list) or not nested:
                reasons.append("repeat actions must be a non-empty list")
                nested = []
            nested_cost = 0
            for item in nested:
                nested_cost += check_action(item)
            cost = (times or 0) * nested_cost
        return cost

    for action in actions:
        total += check_action(action)
        if total > max_frames:
            raise PlanRejected(f"expanded frame count exceeds {max_frames}")
    start_from = plan.get("start_from")
    if start_from is None:
        start_from = {}
    elif not isinstance(start_from, dict):
        reasons.append("start_from must be an object")
        start_from = {}
    if start_from and abstraction_fields is not None:
        for field in (start_from.get("prefer_abstraction") or {}):
            if field not in abstraction_fields:
                reasons.append(f"start_from field {field!r} is not abstracted")
    if "prefix_id" in start_from:
        prefix_id = start_from.get("prefix_id")
        if not isinstance(prefix_id, str) or not prefix_id.strip():
            reasons.append("start_from.prefix_id must be a non-empty string")
        elif reachable_prefix_ids is not None and prefix_id not in reachable_prefix_ids:
            reasons.append(f"start_from prefix {prefix_id!r} is not reachable")
    if reasons:
        raise PlanRejected("; ".join(reasons))
    return plan


def validate_plan_batch(
    batch: Any, *, abi: dict[str, Any], contract_id: str | None,
    tasks: dict[str, dict[str, Any]], seen_actions: set[str], max_plans: int,
    max_frames: int = 128,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate a planner response without discarding valid peers in a batch."""
    if not isinstance(batch, dict) or set(batch) != {"contract_id", "plans"}:
        raise PlanRejected("PlanBatch requires exactly contract_id and plans")
    if batch["contract_id"] != contract_id:
        raise PlanRejected("PlanBatch contract_id does not match")
    plans = batch["plans"]
    if not isinstance(plans, list) or not 1 <= len(plans) <= max_plans:
        raise PlanRejected(f"plans must contain 1..{max_plans} items")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(plans):
        try:
            if not isinstance(raw, dict):
                raise PlanRejected("plan is not a JSON object")
            required = {"plan_id", "task_id", "contract_id", "evidence_refs",
                        "suggested_symbolic_fields", "actions"}
            missing = required - set(raw)
            if missing:
                raise PlanRejected(f"missing required plan fields {sorted(missing)}")
            if raw["contract_id"] != contract_id:
                raise PlanRejected("plan contract_id does not match PlanBatch")
            task_id = raw.get("task_id")
            task = tasks.get(task_id) if isinstance(task_id, str) else None
            if task is None:
                raise PlanRejected("task_id is not an available target")
            plan = validate_plan(
                raw, abi=abi, contract_id=contract_id, task_id=task_id,
                abstraction_fields=set(task.get("abstraction_fields") or []),
                allowed_evidence_refs=set(task.get("evidence_refs") or []),
                reachable_prefix_ids=set(task.get("reachable_prefix_ids") or []),
                max_frames=max_frames,
            )
            key = json.dumps({"task_id": task_id, "start_from": plan.get("start_from"),
                              "actions": plan["actions"]}, sort_keys=True)
            if key in seen_actions:
                raise PlanRejected("duplicate plan actions for this task and prefix")
            seen_actions.add(key)
            accepted.append(plan)
        except PlanRejected as error:
            rejected.append({"index": index, "plan": raw, "reason": str(error)})
    return accepted, rejected


def expand_plan(
    plan: dict[str, Any],
    base_frame: dict[str, str],
    abi: dict[str, Any],
    *,
    max_frames: int = MAX_FRAMES,
) -> list[dict[str, str]]:
    plan = normalize_plan(plan)
    fields = _field_map(abi)
    defaults = {name: default_for_spec(entry["spec"]) for name, entry in fields.items()
                if "." not in name}
    frames: list[dict[str, str]] = []
    current = {**defaults, **base_frame}

    def apply(inputs: dict[str, Any]) -> None:
        inputs = _expand_aggregate_inputs(inputs, abi)
        for name, value in inputs.items():
            if isinstance(value, bool):
                value = 1 if value else 0
            current[name] = str(value)

    def run(actions: list[dict[str, Any]]) -> None:
        for action in actions:
            op = action["op"]
            if op == "set":
                apply(action.get("inputs") or {})
                frames.append(dict(current))
            elif op == "pulse":
                name = action["input"]
                low = dict(current)
                low[name] = "0"
                frames.append(low)
                high = dict(current)
                high[name] = "1"
                frames.append(high)
                current.update(high)
            elif op == "hold":
                apply(action.get("inputs") or {})
                for _ in range(int(action.get("frames") or 1)):
                    frames.append(dict(current))
            elif op == "wait":
                for _ in range(int(action.get("frames") or 1)):
                    frames.append(dict(current))
            elif op == "repeat":
                for _ in range(int(action.get("times") or 1)):
                    run(action.get("actions") or [])

    run(plan.get("actions") or [])
    if len(frames) > max_frames:
        raise PlanRejected(f"expanded frame count exceeds {max_frames}")
    return frames
