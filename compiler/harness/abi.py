"""Target ABI for harness generation, taken only from compiler outputs.

Logical types (resolved constants, array bounds, string capacities, VAR_IN_OUT
pointees) come from the STG model, which RuSTy's semantic index produces.
Physical layout (FB member offsets, FUNCTION argument order, by-value copy
sizes) comes from the compiled LLVM IR. Each fact the two sources share is
cross-checked; any disagreement is an error instead of a guessed layout.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from compiler.harness.llvm_ir import (
    IrFunction,
    LLVMType,
    find_function,
    function_block_members,
    function_param_bindings,
    parse_named_definitions,
    parse_type,
    size_align,
    struct_field_layout,
    struct_size_align,
)

SCALAR_TYPES = {
    "BOOL": ("uint8_t", 1),
    "SINT": ("int8_t", 1),
    "USINT": ("uint8_t", 1),
    "BYTE": ("uint8_t", 1),
    "CHAR": ("uint8_t", 1),
    "INT": ("int16_t", 2),
    "UINT": ("uint16_t", 2),
    "WORD": ("uint16_t", 2),
    "WCHAR": ("uint16_t", 2),
    "DINT": ("int32_t", 4),
    "UDINT": ("uint32_t", 4),
    "DWORD": ("uint32_t", 4),
    "LINT": ("int64_t", 8),
    "ULINT": ("uint64_t", 8),
    "LWORD": ("uint64_t", 8),
    "REAL": ("float", 4),
    "LREAL": ("double", 8),
    "TIME": ("int64_t", 8),
    "LTIME": ("int64_t", 8),
    "DATE": ("int64_t", 8),
    "LDATE": ("int64_t", 8),
    "DATE_AND_TIME": ("int64_t", 8),
    "DT": ("int64_t", 8),
    "LDATE_AND_TIME": ("int64_t", 8),
    "LDT": ("int64_t", 8),
    "TIME_OF_DAY": ("int64_t", 8),
    "TOD": ("int64_t", 8),
    "LTIME_OF_DAY": ("int64_t", 8),
    "LTOD": ("int64_t", 8),
}
_INTEGER_BY_WIDTH = {
    (8, True): "SINT", (8, False): "USINT",
    (16, True): "INT", (16, False): "UINT",
    (32, True): "DINT", (32, False): "UDINT",
    (64, True): "LINT", (64, False): "ULINT",
}
# Pointer inputs point at an object of the declared pointee type. Larger
# declared pointees (e.g. OSCAT's ARRAY[0..32767] OF BYTE views) are capped.
MAX_POINTEE_BYTES = 1 << 20

ROLE_BLOCKS = {
    "input": "VAR_INPUT",
    "output": "VAR_OUTPUT",
    "in_out": "VAR_IN_OUT",
    "local": "VAR",
    "persistent_state": "VAR",
    "temporary": "VAR_TEMP",
    "return": "RETURN",
}


class AbiError(ValueError):
    pass


@dataclass
class TypeSpec:
    kind: str  # scalar | string | array | pointer | opaque
    name: str
    c_type: str | None = None
    size: int = 0  # scalar bytes, string code-unit width, or opaque bytes
    length: int | None = None  # string characters, excluding the terminator
    element: "TypeSpec | None" = None
    count: int | None = None
    align: int = 1

    def byte_size(self) -> int:
        if self.kind == "scalar" or self.kind == "opaque":
            return self.size
        if self.kind == "string":
            return (self.length + 1) * self.size
        if self.kind == "array":
            return self.count * self.element.byte_size()
        if self.kind == "pointer":
            return 8
        raise AbiError(f"unknown type kind {self.kind}")

    def flat_c_type(self) -> str:
        if self.kind == "scalar":
            return self.c_type
        if self.kind == "string":
            return self.c_type
        if self.kind == "array":
            return self.element.flat_c_type()
        return "uint8_t"

    def flat_count(self) -> int:
        if self.kind == "scalar":
            return 1
        if self.kind == "string":
            return self.length + 1
        if self.kind == "array":
            return self.count * self.element.flat_count()
        if self.kind == "pointer":
            return 8
        return self.size

    def pointee_bytes(self) -> int:
        """Backing storage for a pointer input: one object of the pointee type."""
        if self.kind != "pointer" or self.element is None:
            return 1
        return max(1, min(self.element.byte_size(), MAX_POINTEE_BYTES))


@dataclass(eq=False)
class Param:
    name: str
    spec: TypeSpec
    block_kind: str
    constant: bool = False
    offset: int | None = None
    index: int | None = None  # FB struct field index or FUNCTION argument position


@dataclass
class CallSlot:
    param: Param
    ir_type: LLVMType


@dataclass
class TargetAbi:
    kind: str
    name: str
    params: list[Param]
    outputs: list[Param] = field(default_factory=list)
    state_fields: list[Param] = field(default_factory=list)
    snapshot_fields: list[Param] = field(default_factory=list)
    instance_size: int = 0
    instance_align: int = 1
    ret: Param | None = None
    ret_by_value: LLVMType | None = None
    call_slots: list[CallSlot] = field(default_factory=list)


def scalar_spec(name: str) -> TypeSpec:
    c_type, size = SCALAR_TYPES[name]
    return TypeSpec("scalar", name, c_type=c_type, size=size, align=size)


class _StgTypes:
    def __init__(self, types: list[dict], definitions: dict[str, LLVMType]):
        self.types = {item["name"].upper(): item for item in types}
        self.definitions = definitions

    def spec(self, type_name: str, depth: int = 0) -> TypeSpec:
        if depth > 32:
            raise AbiError(f"type {type_name} does not resolve")
        ty = self.types.get(type_name.upper())
        if ty is None:
            if type_name.upper() in SCALAR_TYPES:
                return scalar_spec(type_name.upper())
            raise AbiError(f"STG model has no type {type_name}")
        kind = ty["kind"]
        if kind in ("alias", "subrange", "enum"):
            return self.spec(ty["element_type"], depth + 1)
        if kind == "boolean":
            return scalar_spec("BOOL")
        if kind == "integer":
            name = ty["name"].upper()
            if name not in SCALAR_TYPES:
                name = _INTEGER_BY_WIDTH[(ty["bit_width"], bool(ty["signed"]))]
            return scalar_spec(name)
        if kind == "float":
            return scalar_spec("LREAL" if ty["bit_width"] == 64 else "REAL")
        if kind == "string":
            capacity = ty.get("string_capacity")
            if not capacity:
                raise AbiError(f"string type {type_name} has no resolved capacity")
            wide = ty.get("string_encoding") == "utf16"
            return TypeSpec(
                "string",
                "WSTRING" if wide else "STRING",
                c_type="uint16_t" if wide else "uint8_t",
                size=2 if wide else 1,
                length=capacity - 1,
                align=2 if wide else 1,
            )
        if kind == "array":
            count = 1
            for dimension in ty["dimensions"]:
                if dimension.get("lower") is None or dimension.get("upper") is None:
                    raise AbiError(f"array type {type_name} has unresolved bounds")
                count *= max(0, dimension["upper"] - dimension["lower"] + 1)
            element = self.spec(ty["element_type"], depth + 1)
            return TypeSpec(
                "array", f"ARRAY OF {element.name}", element=element, count=count, align=element.align
            )
        if kind == "pointer":
            element = self.spec(ty["pointer_target"], depth + 1)
            return TypeSpec("pointer", f"REF_TO {element.name}", element=element, align=8)
        if kind == "struct":
            # STG struct widths omit ABI padding and the FB vtable slot; only
            # the IR knows the real size.
            definition = self.definitions.get(f"%{ty['name']}")
            if definition is None:
                return TypeSpec("opaque", ty["name"])
            size, align = size_align(definition, self.definitions)
            return TypeSpec("opaque", ty["name"], size=size, align=align)
        raise AbiError(f"unsupported STG type kind {kind} for {type_name}")


def _check_field(param: Param, ir_type: LLVMType, ir_size: int, ir_align: int) -> None:
    spec = param.spec
    if param.block_kind == "VAR_IN_OUT" or spec.kind == "pointer":
        if ir_type.kind != "ptr":
            raise AbiError(f"{param.name}: expected a pointer in IR, found {ir_type.kind}")
        return
    if spec.kind == "opaque":
        if spec.size and spec.size != ir_size:
            raise AbiError(f"{param.name}: STG type {spec.name} is {spec.size} bytes, IR field is {ir_size}")
        spec.size, spec.align = ir_size, ir_align
        return
    if spec.kind == "scalar" and (ir_type.kind == "float") != (spec.name in ("REAL", "LREAL")):
        raise AbiError(f"{param.name}: STG type {spec.name} disagrees with IR {ir_type.kind}")
    if spec.byte_size() != ir_size:
        raise AbiError(f"{param.name}: STG type {spec.name} is {spec.byte_size()} bytes, IR field is {ir_size}")


def _load_pou(model: dict, function: str) -> dict:
    for pou in model.get("pous", []):
        if pou["pou"]["name"].upper() == function.upper():
            return pou
    raise AbiError(f"STG model does not contain POU {function}")


def _sized(spec: TypeSpec) -> bool:
    if spec.kind == "opaque":
        return spec.size > 0
    if spec.kind in ("array", "pointer"):
        return _sized(spec.element)
    return True


def _symbol_param(symbol: dict, types: _StgTypes, *, instance_member: bool = False) -> Param:
    role = symbol["role"]
    block_kind = ROLE_BLOCKS.get(role)
    if block_kind is None:
        raise AbiError(f"{symbol['name']}: unsupported member role {role}")
    type_name = symbol["type_name"]
    by_reference = bool(symbol.get("by_reference"))
    if by_reference:
        pointer = types.types.get(type_name.upper())
        if pointer is None or pointer["kind"] != "pointer":
            raise AbiError(f"{symbol['name']}: by-reference member without pointer type")
        type_name = pointer["pointer_target"]
    if role == "input":
        constant = symbol.get("configuration_source") is not None
    else:
        constant = bool(symbol.get("constant"))
    spec = types.spec(type_name)
    # A direct FB member gets its aggregate size from the IR field. Anything
    # the harness must allocate itself needs a size the IR defines.
    if not _sized(spec):
        if not instance_member or by_reference:
            raise AbiError(f"{symbol['name']}: no IR layout for type {type_name}")
        spec = TypeSpec("opaque", spec.name)
    return Param(symbol["name"], spec, block_kind, constant)


def load_target_abi(stg_model: Path | str, ir: Path | str, function: str) -> TargetAbi:
    model = json.loads(Path(stg_model).read_text(encoding="utf-8"))
    source = Path(ir).read_text(encoding="utf-8", errors="replace")
    return target_abi(model, source, function)


def target_abi(model: dict, ir_source: str, function: str) -> TargetAbi:
    pou = _load_pou(model, function)
    name = pou["pou"]["name"]
    definitions = parse_named_definitions(ir_source)
    types = _StgTypes(pou["environment"]["types"], definitions)
    symbols = sorted(pou["environment"]["symbols"], key=lambda item: item["source"]["start_offset"])
    by_name = {symbol["name"].upper(): symbol for symbol in symbols}
    ir_function = find_function(ir_source, name)
    if ir_function is None:
        raise AbiError(f"LLVM IR does not define @{name}")
    if pou["pou"]["kind"] == "function_block":
        return _function_block_abi(name, ir_function, definitions, types, by_name)
    if pou["pou"]["kind"] == "function":
        return _function_abi(name, ir_function, types, by_name)
    raise AbiError(f"{name}: unsupported POU kind {pou['pou']['kind']}")


def _function_block_abi(name, ir_function: IrFunction, definitions, types, by_name) -> TargetAbi:
    struct_type, members = function_block_members(ir_function)
    definition = definitions.get(struct_type)
    if definition is None or definition.kind != "struct":
        raise AbiError(f"{name}: IR does not define instance type {struct_type}")
    layout = struct_field_layout(definition.fields, definitions)
    if [index for index, _ in members] != list(range(len(layout))):
        raise AbiError(f"{name}: entry GEPs do not cover every field of {struct_type}")
    if members[0][1] != "__vtable" or layout[0][2].kind != "ptr":
        raise AbiError(f"{name}: field 0 of {struct_type} is not the FB vtable")
    instance_size, instance_align = struct_size_align(definition.fields, definitions, {})
    state_fields: list[Param] = []
    for index, member in members[1:]:
        symbol = by_name.get(member.upper())
        if symbol is None:
            raise AbiError(f"{name}: IR member {member} has no STG symbol")
        param = _symbol_param(symbol, types, instance_member=True)
        if param.block_kind in ("VAR_TEMP", "RETURN"):
            raise AbiError(f"{name}: {param.block_kind} member {member} inside the instance")
        offset, size, ir_type = layout[index]
        _check_field(param, ir_type, size, size_align(ir_type, definitions)[1])
        param.offset = offset
        param.index = index
        state_fields.append(param)
    stored = {param.name.upper() for param in state_fields}
    missing = [
        symbol["name"]
        for key, symbol in by_name.items()
        if key not in stored and symbol["role"] not in ("temporary", "global", "external")
    ]
    if missing:
        raise AbiError(f"{name}: STG members missing from the IR instance: {', '.join(missing)}")
    params = [param for param in state_fields if param.block_kind in ("VAR_INPUT", "VAR_IN_OUT")]
    outputs = [param for param in state_fields if param.block_kind == "VAR_OUTPUT"]
    snapshot = [
        param
        for param in state_fields
        if param.block_kind in ("VAR_OUTPUT", "VAR_IN_OUT") or (param.block_kind == "VAR" and not param.constant)
    ]
    return TargetAbi(
        "FUNCTION_BLOCK",
        name,
        params,
        outputs=outputs,
        state_fields=state_fields,
        snapshot_fields=snapshot,
        instance_size=instance_size,
        instance_align=instance_align,
    )


def _alloca_type(ir_function: IrFunction, variable: str) -> LLVMType | None:
    match = re.search(
        rf"^\s*%\"?{re.escape(variable)}\"? = alloca (?P<type>.+?), align \d+\s*$",
        ir_function.entry_block(),
        re.M,
    )
    return parse_type(match.group("type")) if match else None


def _function_abi(name, ir_function: IrFunction, types, by_name) -> TargetAbi:
    slots: list[CallSlot] = []
    ret: Param | None = None
    for binding in function_param_bindings(ir_function):
        symbol = by_name.get(binding.variable.upper())
        if symbol is None:
            raise AbiError(f"{name}: IR argument {binding.position} binds unknown variable {binding.variable}")
        param = _symbol_param(symbol, types)
        param.index = binding.position
        ir_type = ir_function.params[binding.position].type
        spec = param.spec
        if param.block_kind == "RETURN":
            if ir_type.kind != "ptr" or spec.kind not in ("string", "array", "opaque"):
                raise AbiError(f"{name}: unexpected by-reference return of {spec.name}")
            ret = param
        elif param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT") or spec.kind == "pointer":
            if ir_type.kind != "ptr":
                raise AbiError(f"{name}: {param.name} must be passed as a pointer")
        elif binding.by_copy_bytes is not None:
            local = _alloca_type(ir_function, binding.variable)
            local_size = size_align(local, {})[0] if local is not None and local.kind != "named" else None
            if spec.kind == "opaque":
                raise AbiError(f"{name}: by-value aggregate {param.name} of type {spec.name} is unsupported")
            if local_size is not None and local_size != spec.byte_size():
                raise AbiError(f"{name}: {param.name} is {spec.byte_size()} bytes, IR local is {local_size}")
            if binding.by_copy_bytes > spec.byte_size():
                raise AbiError(f"{name}: IR copies {binding.by_copy_bytes} bytes into {param.name}")
        else:
            _check_field(param, ir_type, size_align(ir_type, {})[0], size_align(ir_type, {})[1])
        slots.append(CallSlot(param, ir_type))
    ret_by_value = None
    if ir_function.return_type.kind != "void":
        if ret is not None:
            raise AbiError(f"{name}: both a return value and a return pointer")
        symbol = by_name.get(name.upper())
        if symbol is None or symbol["role"] != "return":
            raise AbiError(f"{name}: returns a value but STG has no return symbol")
        ret = _symbol_param(symbol, types)
        _check_field(ret, ir_function.return_type, size_align(ir_function.return_type, {})[0], 1)
        ret_by_value = ir_function.return_type
    params = [
        slot.param
        for slot in slots
        if slot.param.block_kind in ("VAR_INPUT", "VAR_IN_OUT")
    ]
    outputs = [slot.param for slot in slots if slot.param.block_kind == "VAR_OUTPUT"]
    return TargetAbi(
        "FUNCTION",
        name,
        params,
        outputs=outputs,
        ret=ret,
        ret_by_value=ret_by_value,
        call_slots=slots,
    )
