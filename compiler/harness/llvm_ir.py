"""Minimal LLVM IR reader for the RuSTy-compiled target ABI.

The RuSTy compiler emits LLVM text IR.  This module parses the named
``%Struct = type { ... }`` definitions and computes the byte size, alignment,
and per-field offsets of a target struct using the standard x86_64 System V
layout rules (which match the ``e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-
i128:128-f80:128-n8:16:32:64-S128`` data layout RuSTy emits).

It also reads the two facts a harness must not guess: which struct field each
FUNCTION_BLOCK member occupies (RuSTy names one entry-block GEP per member),
and which IR parameter each FUNCTION argument binds to (RuSTy stores or copies
every parameter into an alloca named after the declared variable).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Type representation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLVMType:
    kind: str  # "int", "float", "ptr", "array", "vector", "struct", "named", "void"
    bits: int = 0
    count: int = 0
    element: Optional["LLVMType"] = None
    fields: tuple["LLVMType", ...] = ()
    name: str = ""


_INT_RE = re.compile(r"^i(\d+)$")
_FLOAT_RE = re.compile(r"^(half|float|double|fp128|x86_fp80)$")


def parse_type(text: str) -> LLVMType:
    text = text.strip()
    if text == "ptr":
        return LLVMType("ptr")
    if text == "void":
        return LLVMType("void")
    m = _INT_RE.match(text)
    if m:
        return LLVMType("int", bits=int(m.group(1)))
    if _FLOAT_RE.match(text):
        return LLVMType("float", bits={"half": 16, "float": 32, "double": 64,
                                       "fp128": 128, "x86_fp80": 80}[text])
    if text.startswith("%"):
        return LLVMType("named", name=text)
    if text.startswith("["):
        # [N x T]
        assert text.endswith("]")
        inner = text[1:-1]
        m = re.match(r"^(\d+)\s*x\s*(.*)$", inner, re.S)
        assert m, f"bad array type: {text}"
        return LLVMType("array", count=int(m.group(1)), element=parse_type(m.group(2)))
    if text.startswith("<"):
        assert text.endswith(">")
        inner = text[1:-1]
        m = re.match(r"^(\d+)\s*x\s*(.*)$", inner, re.S)
        assert m, f"bad vector type: {text}"
        return LLVMType("vector", count=int(m.group(1)), element=parse_type(m.group(2)))
    if text.startswith("{"):
        assert text.endswith("}")
        return LLVMType("struct", fields=tuple(parse_type(f) for f in split_fields(text[1:-1])))
    raise ValueError(f"unsupported LLVM type: {text!r}")


def split_fields(body: str) -> list[str]:
    """Split a comma-separated list of LLVM types, respecting nesting."""
    fields: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch in "[{<(":
            depth += 1
        elif ch in "]}>)":
            depth -= 1
        if ch == "," and depth == 0:
            fields.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        fields.append(tail)
    return fields


def parse_named_definitions(source: str) -> dict[str, LLVMType]:
    definitions: dict[str, LLVMType] = {}
    pattern = re.compile(r"(?m)^(%[A-Za-z0-9_.$-]+)\s*=\s*type\s*(\{.*?\}|<.*?>|\[.*?\])\s*$")
    for match in pattern.finditer(source):
        name = match.group(1)
        body = match.group(2)
        definitions[name] = parse_type(body)
    return definitions


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def size_align(t: LLVMType, definitions: dict[str, LLVMType],
               _cache: Optional[dict[str, tuple[int, int]]] = None) -> tuple[int, int]:
    """Return (size, align) in bytes for the given type."""
    cache = _cache if _cache is not None else {}

    if t.kind == "ptr":
        return (8, 8)
    if t.kind == "void":
        return (0, 1)
    if t.kind == "int":
        size = max(1, (t.bits + 7) // 8)
        align = min(size, 8)
        if t.bits > 64:
            align = 16
            size = (t.bits + 7) // 8
        return (size, align)
    if t.kind == "float":
        size = max(1, (t.bits + 7) // 8)
        return (size, size)
    if t.kind == "array" or t.kind == "vector":
        esize, ealign = size_align(t.element, definitions, cache)
        return (t.count * esize, ealign)
    if t.kind == "struct":
        return struct_size_align(t.fields, definitions, cache)
    if t.kind == "named":
        if t.name in cache:
            return cache[t.name]
        if t.name not in definitions:
            raise ValueError(f"LLVM IR does not define named type {t.name}")
        # Placeholder to break cycles (structs cannot be directly recursive in
        # LLVM without indirection, so this is safe).
        cache[t.name] = (0, 1)
        result = size_align(definitions[t.name], definitions, cache)
        cache[t.name] = result
        return result
    raise ValueError(f"unsupported type kind: {t.kind}")


def struct_size_align(fields: tuple[LLVMType, ...], definitions: dict[str, LLVMType],
                      cache: dict[str, tuple[int, int]]) -> tuple[int, int]:
    max_align = 1
    offset = 0
    for field in fields:
        fsize, falign = size_align(field, definitions, cache)
        max_align = max(max_align, falign)
        if offset % falign:
            offset += falign - (offset % falign)
        offset += fsize
    if max_align > 1 and offset % max_align:
        offset += max_align - (offset % max_align)
    return (offset, max_align)


def struct_field_layout(fields: tuple[LLVMType, ...], definitions: dict[str, LLVMType]
                        ) -> list[tuple[int, int, LLVMType]]:
    """Return [(offset, size, type), ...] for each struct field."""
    result: list[tuple[int, int, LLVMType]] = []
    cache: dict[str, tuple[int, int]] = {}
    offset = 0
    for field in fields:
        fsize, falign = size_align(field, definitions, cache)
        if offset % falign:
            offset += falign - (offset % falign)
        result.append((offset, fsize, field))
        offset += fsize
    return result


def struct_size(fields: tuple[LLVMType, ...], definitions: dict[str, LLVMType]) -> int:
    cache: dict[str, tuple[int, int]] = {}
    return struct_size_align(fields, definitions, cache)[0]


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IrParam:
    type: LLVMType
    value: str  # SSA name without the leading "%"


@dataclass(frozen=True)
class IrFunction:
    name: str
    return_type: LLVMType
    params: tuple[IrParam, ...]
    body: str

    def entry_block(self) -> str:
        """Instructions before the first explicit basic-block label."""
        lines = []
        for line in self.body.splitlines():
            if re.match(r"^[A-Za-z0-9_.$\"-]+:", line) and lines:
                break
            if line.strip() and not line.strip().endswith(":"):
                lines.append(line)
        return "\n".join(lines)


_VALUE = r'(?:[A-Za-z0-9_.$-]+|"[^"]+")'


def _leading_type(text: str) -> tuple[str, str]:
    """Split ``<type> rest`` where the type may be bracketed."""
    text = text.strip()
    if text and text[0] in "[{<":
        closer = {"[": "]", "{": "}", "<": ">"}[text[0]]
        depth = 0
        for index, ch in enumerate(text):
            if ch == text[0]:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[: index + 1], text[index + 1 :]
        raise ValueError(f"unbalanced LLVM type: {text!r}")
    head, _, rest = text.partition(" ")
    return head, rest


def find_function(source: str, name: str) -> IrFunction | None:
    pattern = re.compile(
        rf'(?m)^define\s+(?P<head>[^@\n]*?)\s*@"?{re.escape(name)}"?\((?P<params>[^)]*)\)[^{{\n]*\{{\s*$'
    )
    match = pattern.search(source)
    if match is None:
        return None
    head = match.group("head").strip()
    if head.endswith(("]", "}", ">")):
        opener = {"]": "[", "}": "{", ">": "<"}[head[-1]]
        return_text = head[head.rindex(opener):]
    else:
        return_text = head.split()[-1]
    params: list[IrParam] = []
    for raw in split_fields(match.group("params")):
        type_text, rest = _leading_type(raw)
        value = re.search(rf"%({_VALUE})\s*$", rest)
        if value is None:
            raise ValueError(f"unnamed LLVM parameter in @{name}: {raw!r}")
        params.append(IrParam(parse_type(type_text), value.group(1).strip('"')))
    end = source.find("\n}", match.end())
    if end < 0:
        raise ValueError(f"unterminated LLVM function @{name}")
    return IrFunction(name, parse_type(return_text), tuple(params), source[match.end() : end])


def function_block_members(function: IrFunction) -> tuple[str, list[tuple[int, str]]]:
    """Return the instance struct type and its named member GEPs.

    RuSTy emits ``%<member> = getelementptr inbounds %<type>, ptr %0, i32 0,
    i32 <index>`` for every member at FB entry. LLVM may merge isomorphic
    struct types when linking, so the type comes from these GEPs and never
    from the FB name.
    """
    if len(function.params) != 1 or function.params[0].type.kind != "ptr":
        raise ValueError(f"@{function.name} is not a FUNCTION_BLOCK body (expected one ptr parameter)")
    this = re.escape(function.params[0].value)
    pattern = re.compile(
        rf"^\s*%(?P<member>{_VALUE}) = getelementptr inbounds(?: nuw)? (?P<type>%[A-Za-z0-9_.$-]+), "
        rf"ptr %{this}, i32 0, i32 (?P<index>\d+)\s*$",
        re.M,
    )
    struct_type: str | None = None
    members: dict[int, str] = {}
    for match in pattern.finditer(function.entry_block()):
        if struct_type is None:
            struct_type = match.group("type")
        elif struct_type != match.group("type"):
            raise ValueError(f"@{function.name} addresses members of two struct types")
        members.setdefault(int(match.group("index")), match.group("member").strip('"'))
    if struct_type is None:
        raise ValueError(f"@{function.name} has no member GEPs at entry")
    return struct_type, sorted(members.items())


@dataclass(frozen=True)
class ParamBinding:
    position: int
    variable: str
    by_copy_bytes: int | None  # bytes RuSTy copies out of the pointer, if by value


def function_param_bindings(function: IrFunction) -> list[ParamBinding]:
    """Bind every IR parameter to the declared variable that receives it."""
    entry = function.entry_block()
    bindings: list[ParamBinding] = []
    for position, param in enumerate(function.params):
        value = re.escape(param.value)
        store = re.search(rf"^\s*store [^,]+ %{value}, ptr %(?P<var>{_VALUE}),", entry, re.M)
        copy = re.search(
            rf"^\s*call void @llvm\.memcpy\.[^(]+\(ptr align \d+ %(?P<var>{_VALUE}), "
            rf"ptr align \d+ %{value}, i64 (?P<bytes>\d+), i1 false\)",
            entry,
            re.M,
        )
        if copy is not None:
            bindings.append(ParamBinding(position, copy.group("var").strip('"'), int(copy.group("bytes"))))
        elif store is not None:
            bindings.append(ParamBinding(position, store.group("var").strip('"'), None))
        else:
            raise ValueError(f"cannot bind IR parameter %{param.value} of @{function.name} to a variable")
    return bindings
