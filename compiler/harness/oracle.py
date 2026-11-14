"""Compile bounded, typed output predicates into a native harness monitor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from compiler.harness.abi import Param, TargetAbi, frame_params


@dataclass(frozen=True)
class HistoryField:
    name: str
    role: str
    state_name: str
    access: str
    is_bool: bool


def history_fields(abi: TargetAbi) -> list[HistoryField]:
    offset = 1 if abi.kind == "FUNCTION_BLOCK" else 0
    fields: list[HistoryField] = []
    for param in frame_params(abi):
        if param.spec.kind == "scalar":
            fields.append(HistoryField(param.name.upper(), "input", f"oracle_prev_i{param.index}",
                                       f"frame->f{param.index - offset}", param.spec.name == "BOOL"))
    if abi.kind == "FUNCTION_BLOCK":
        for param in abi.outputs:
            if param.block_kind == "VAR_OUTPUT" and param.spec.kind == "scalar":
                fields.append(HistoryField(param.name.upper(), "output", f"oracle_prev_o{param.index}",
                                           f"semantist_harness_state.inst.f{param.index - offset}",
                                           param.spec.name == "BOOL"))
    elif abi.ret is not None and abi.ret.spec.kind == "scalar":
        fields.append(HistoryField("__RESULT", "output", "oracle_prev_result",
                                   "semantist_harness_state.result", abi.ret.spec.name == "BOOL"))
    return fields


class OracleRejected(ValueError):
    pass


def _expression(node: Any, fields: dict[tuple[str, str], HistoryField], *,
                depth: int = 0) -> tuple[str, str, bool]:
    if depth > 12 or not isinstance(node, dict):
        raise OracleRejected("oracle expression is too deep or is not an object")
    if set(node) == {"const"}:
        value = node["const"]
        if isinstance(value, bool):
            return ("1" if value else "0", "bool", False)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 10**18:
            return (repr(value) + "L", "number", False)
        raise OracleRejected("oracle constant is outside the supported numeric domain")
    if set(node) == {"field", "at"}:
        at = node["at"]
        if at not in {"input", "output", "previous_input", "previous_output"}:
            raise OracleRejected(f"unsupported oracle observation time: {at}")
        role = at.removeprefix("previous_")
        field = fields.get((role, str(node["field"]).upper()))
        if field is None:
            raise OracleRejected(f"unknown scalar {role} field: {node['field']}")
        previous = at.startswith("previous_")
        access = (f"semantist_harness_state.{field.state_name}" if previous else field.access)
        return (f"({access})" if field.is_bool else f"((long double)({access}))",
                "bool" if field.is_bool else "number", previous)
    if set(node) != {"op", "args"} or not isinstance(node["args"], list):
        raise OracleRejected("oracle node requires op and args")
    op, raw = node["op"], node["args"]
    expected_count = {"not": 1, "abs": 1, "finite": 1, "between": 3}.get(op, 2)
    if op not in {"and", "or", "not", "eq", "ne", "lt", "le", "gt", "ge",
                  "add", "sub", "mul", "min", "max", "abs", "between", "finite"}:
        raise OracleRejected(f"unsupported oracle operation: {op}")
    if len(raw) != expected_count:
        raise OracleRejected(f"oracle operation {op} requires {expected_count} arguments")
    parts = [_expression(item, fields, depth=depth + 1) for item in raw]
    codes, kinds, previous = [item[0] for item in parts], [item[1] for item in parts], any(
        item[2] for item in parts)
    if op in {"and", "or", "not"}:
        if any(kind != "bool" for kind in kinds):
            raise OracleRejected(f"{op} requires boolean arguments")
        if op == "not":
            return f"(!{codes[0]})", "bool", previous
        return f"({codes[0]} {'&&' if op == 'and' else '||'} {codes[1]})", "bool", previous
    if op in {"eq", "ne", "lt", "le", "gt", "ge"}:
        if kinds[0] != kinds[1] or (kinds[0] == "bool" and op not in {"eq", "ne"}):
            raise OracleRejected(f"{op} has incompatible argument types")
        operators = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
        return f"({codes[0]} {operators[op]} {codes[1]})", "bool", previous
    if any(kind != "number" for kind in kinds):
        raise OracleRejected(f"{op} requires numeric arguments")
    if op == "between":
        return f"({codes[1]} <= {codes[0]} && {codes[0]} <= {codes[2]})", "bool", previous
    if op == "finite":
        return f"isfinite({codes[0]})", "bool", previous
    if op == "abs":
        return f"fabsl({codes[0]})", "number", previous
    if op in {"min", "max"}:
        comparison = "<" if op == "min" else ">"
        return f"(({codes[0]} {comparison} {codes[1]}) ? {codes[0]} : {codes[1]})", "number", previous
    operator = {"add": "+", "sub": "-", "mul": "*"}[op]
    return f"({codes[0]} {operator} {codes[1]})", "number", previous


def validate_document(abi: TargetAbi, document: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or document.get("schema_version") != "semantist.generated-oracle/1":
        raise OracleRejected("unsupported generated oracle schema")
    properties = document.get("properties")
    if not isinstance(properties, list) or len(properties) > 3:
        raise OracleRejected("generated oracle requires zero to three properties")
    fields = {(field.role, field.name): field for field in history_fields(abi)}
    seen: set[str] = set()
    for spec in properties:
        if not isinstance(spec, dict) or set(spec) != {"id", "precision", "evidence_refs", "when", "must"}:
            raise OracleRejected("generated property requires id, precision, evidence_refs, when and must")
        identifier = spec["id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identifier):
            raise OracleRejected("invalid generated property id")
        if identifier in seen:
            raise OracleRejected("duplicate generated property id")
        seen.add(identifier)
        if spec["precision"] not in {"exact", "estimated"}:
            raise OracleRejected("generated property precision must be exact or estimated")
        if (not isinstance(spec["evidence_refs"], list)
                or not spec["evidence_refs"]
                or any(not isinstance(item, str) for item in spec["evidence_refs"])):
            raise OracleRejected("generated property requires evidence references")
        for key in ("when", "must"):
            _, kind, _ = _expression(spec[key], fields)
            if kind != "bool":
                raise OracleRejected(f"{key} must be a boolean expression")
    return properties


def render_monitor(abi: TargetAbi, document: dict[str, Any] | None) -> tuple[str, str]:
    if document is None:
        return "", ""
    properties = validate_document(abi, document)
    if not properties:
        return "", ""
    fields = history_fields(abi)
    field_map = {(field.role, field.name): field for field in fields}
    checks = []
    for spec in properties:
        guard, _, guard_previous = _expression(spec["when"], field_map)
        must, _, must_previous = _expression(spec["must"], field_map)
        if guard_previous or must_previous:
            guard = f"(semantist_harness_state.oracle_previous_valid && {guard})"
        checks.append(f'    if ({guard} && !({must})) semantist_oracle_violate("{spec["id"]}");')
    definitions = """
static void semantist_oracle_violate(const char *property_id) {
    fprintf(stderr, "SEMANTIST_PROPERTY_VIOLATION:%s:%llu\\n", property_id,
            (unsigned long long)(semantist_harness_state.frame_index - 1));
    fflush(stderr);
    abort();
}

static void semantist_oracle_check(const semantist_frame_t *frame) {
""" + "\n".join(checks) + "\n}\n"
    update = [f"    semantist_harness_state.{field.state_name} = (long double)({field.access});"
              for field in fields]
    hook = ("    semantist_oracle_check(frame);\n"
            + "\n".join(update) + "\n"
            + "    semantist_harness_state.oracle_previous_valid = 1;")
    return definitions, hook
