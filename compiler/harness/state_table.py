#!/usr/bin/env python3
"""Record the target's writable globals in its own prepared LLVM IR.

The harness restores these regions before every input, so an execution in a
persistent or in-process loop starts from the same state as a fresh process.
The table lives in the target module because prepared globals are internal:
no other object can name them. Each entry stores the global's address and the
allocation size of its IR type; ASan redzones are outside those bytes.

A table entry makes its global escape, which stops the optimizer from deleting
store-only globals (such as unused FB vtables and the functions they reach) or
folding loads of never-written ones. With ``--optimized``, the IR the target
compile itself produces, only globals still writable after that optimization
are recorded; the others are never written at run time.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

TABLE = "__semantist_state_regions"
COUNT = "__semantist_state_region_count"

_NAME = r'@(?:"(?:[^"\\]|\\.)*"|[-a-zA-Z$._0-9]+)'
_GLOBAL_RE = re.compile(
    rf"^(?P<name>{_NAME})\s*=\s*(?P<attributes>(?:[-\w.()]+\s+)*?)(?P<kind>global|constant)\s+(?P<rest>.*)$"
)
# Linkages that do not define storage in this module.
_DECLARATIONS = {"external", "extern_weak", "available_externally"}
_OPEN = {"{": "}", "[": "]", "<": ">", "(": ")"}


class StateTableError(ValueError):
    pass


@dataclass(frozen=True)
class WritableGlobal:
    name: str
    llvm_type: str


def _split_type(text: str) -> str:
    """Return the leading LLVM type of a global's ``TYPE INITIALIZER`` text."""
    text = text.lstrip()
    if not text:
        raise StateTableError("global without a type")
    if text[0] in _OPEN:
        depth = 0
        for index, char in enumerate(text):
            if char in _OPEN:
                depth += 1
            elif char in _OPEN.values():
                depth -= 1
                if depth == 0:
                    return text[: index + 1]
        raise StateTableError(f"unbalanced type in {text[:60]!r}")
    match = re.match(r'%"(?:[^"\\]|\\.)*"|%[-a-zA-Z$._0-9]+|[a-z][a-z0-9]*(?:\s+addrspace\(\d+\))?', text)
    if match is None:
        raise StateTableError(f"unknown type in {text[:60]!r}")
    return match.group(0)


def writable_globals(source: str) -> list[WritableGlobal]:
    regions: list[WritableGlobal] = []
    for line in source.splitlines():
        if not line.startswith("@"):
            continue
        match = _GLOBAL_RE.match(line)
        if match is None or match.group("kind") != "global":
            continue
        name = match.group("name")
        attributes = match.group("attributes").split()
        if name.startswith(("@llvm.", '@"llvm.')) or name in (f"@{TABLE}", f"@{COUNT}"):
            continue
        if _DECLARATIONS.intersection(attributes) or any(item.startswith("thread_local") for item in attributes):
            continue
        rest = match.group("rest")
        if 'section "llvm.metadata"' in rest:
            continue
        regions.append(WritableGlobal(name, _split_type(rest)))
    return regions


def _bare(name: str) -> str:
    return name[1:].strip('"')


def survives(region: WritableGlobal, optimized: set[str]) -> bool:
    """The global, or the parts scalar replacement split it into, is still writable."""
    bare = _bare(region.name)
    return bare in optimized or any(name.startswith(bare + ".") for name in optimized)


def append_state_table(source: str, optimized: str | None = None) -> tuple[str, int]:
    if re.search(rf"^@{TABLE}\b|^@{COUNT}\b", source, re.M):
        raise StateTableError("IR already contains a SemantiST state table")
    regions = writable_globals(source)
    if optimized is not None:
        writable = {_bare(region.name) for region in writable_globals(optimized)}
        regions = [region for region in regions if survives(region, writable)]
    entries = ", ".join(
        "{ ptr, i64 } { ptr " + region.name
        + f", i64 ptrtoint (ptr getelementptr ({region.llvm_type}, ptr null, i32 1) to i64) }}"
        for region in regions
    )
    table_type = f"[{len(regions)} x {{ ptr, i64 }}]"
    initializer = f"[{entries}]" if regions else "zeroinitializer"
    table = (
        f"\n@{TABLE} = constant {table_type} {initializer}, align 8\n"
        f"@{COUNT} = constant i64 {len(regions)}, align 8\n"
    )
    return source.rstrip("\n") + "\n" + table, len(regions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_ll", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--optimized", type=Path, help="the same IR after the target compile's optimization")
    args = parser.parse_args(argv)
    source = args.input_ll.read_text(encoding="utf-8", errors="replace")
    optimized = args.optimized.read_text(encoding="utf-8", errors="replace") if args.optimized else None
    try:
        text, count = append_state_table(source, optimized)
    except StateTableError as error:
        print(f"[state_table] {args.input_ll}: {error}")
        return 1
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"[state_table] recorded {count} writable globals in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
