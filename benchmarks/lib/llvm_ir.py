"""Minimal LLVM IR type parser and x86_64 layout calculator.

The RuSTy compiler emits LLVM text IR.  This module parses the named
``%Struct = type { ... }`` definitions and computes the byte size, alignment,
and per-field offsets of a target struct using the standard x86_64 System V
layout rules (which match the ``e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-
i128:128-f80:128-n8:16:32:64-S128`` data layout RuSTy emits).
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
        elif ch in "]}>":
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
    pattern = re.compile(r"(%[A-Za-z0-9_.]+)\s*=\s*type\s*(\{.*?\}|<.*?>|\[.*?\])", re.S)
    for match in pattern.finditer(source):
        name = match.group(1)
        body = match.group(2)
        definitions[name] = parse_type(body)
    return definitions


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

_INT_SIZES = {}


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
        # Placeholder to break cycles (structs cannot be directly recursive in
        # LLVM without indirection, so this is safe).
        cache[t.name] = (0, 1)
        if t.name in definitions:
            result = size_align(definitions[t.name], definitions, cache)
        else:
            result = (8, 8)  # opaque named type: treat as pointer-sized
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
    max_align = 1
    for field in fields:
        fsize, falign = size_align(field, definitions, cache)
        max_align = max(max_align, falign)
        if offset % falign:
            offset += falign - (offset % falign)
        result.append((offset, fsize, field))
        offset += fsize
    if max_align > 1 and offset % max_align:
        offset += max_align - (offset % max_align)
    return result


def struct_size(fields: tuple[LLVMType, ...], definitions: dict[str, LLVMType]) -> int:
    cache: dict[str, tuple[int, int]] = {}
    return struct_size_align(fields, definitions, cache)[0]
