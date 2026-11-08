#!/usr/bin/env python3
"""Insert deterministic loop-backedge accounting into prepared target IR.

The harness implements ``__semantist_loop_tick``; this pass adds one call at
the start of every loop header (after phi nodes), so a scan that never exits is
reported by the harness as a scan-budget violation instead of a host timeout.
The pass is deliberately textual: the prepared IR is already the same module
that the native and KLEE builds share, and a table-driven statement walk is
easier to audit than another compiler plugin.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

TICK = "__semantist_loop_tick"
LABEL_RE = re.compile(r"^(?P<name>[-a-zA-Z$._0-9]+|\"(?:[^\"\\]|\\.)*\"):")
DEFINE_RE = re.compile(r"^\s*define\b.*@(?P<name>\"(?:[^\"\\]|\\.)*\"|[-a-zA-Z$._0-9]+)\s*\(")
SUCC_RE = re.compile(r"label\s+%(?P<label>[-a-zA-Z$._0-9]+|\"(?:[^\"\\]|\\.)*\")")
TERMINATOR_PREFIXES = (
    "br ",
    "switch ",
    "indirectbr ",
    "invoke ",
    "callbr ",
    "ret ",
    "unreachable",
    "resume ",
    "cleanupret ",
    "catchswitch ",
    "catchret ",
    "catchpad ",
    "cleanuppad ",
)


@dataclass
class Block:
    label: str
    lines: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)
    phis: int = 0


def _parse_function(lines: list[str]) -> tuple[list[str], list[Block]]:
    blocks: list[Block] = []
    prefix: list[str] = []
    current: Block | None = None
    for line in lines:
        if current is None and not LABEL_RE.match(line):
            prefix.append(line)
            continue
        match = LABEL_RE.match(line)
        if match:
            current = Block(_bare(match.group("name")))
            blocks.append(current)
            current.lines.append(line)
            continue
        if current is None:
            continue
        current.lines.append(line)
        stripped = line.strip()
        if stripped.startswith("phi "):
            current.phis += 1
        if any(stripped.startswith(term) for term in TERMINATOR_PREFIXES):
            current.successors = [_bare(m.group("label")) for m in SUCC_RE.finditer(stripped)]
    return prefix, blocks


def _bare(label: str) -> str:
    return label.strip().strip('"')


def loop_headers(blocks: list[Block]) -> set[str]:
    by_label = {block.label: block for block in blocks}
    headers: set[str] = set()
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(label: str) -> None:
        state[label] = 1
        stack.append(label)
        block = by_label.get(label)
        if block is not None:
            for successor in block.successors:
                if successor not in by_label:
                    continue
                status = state.get(successor, 0)
                if status == 1:
                    headers.add(successor)
                elif status == 0:
                    visit(successor)
        stack.pop()
        state[label] = 2

    for block in blocks:
        if state.get(block.label, 0) == 0:
            visit(block.label)
    return headers


def instrument(source: str, tick: str = TICK) -> tuple[str, int]:
    if f"@{tick}" in source:
        return source, 0
    lines = source.splitlines()
    output: list[str] = []
    count = 0
    index = 0
    declaration_added = False
    while index < len(lines):
        line = lines[index]
        match = DEFINE_RE.match(line)
        if not match:
            output.append(line)
            index += 1
            continue
        # Collect the complete function body.
        start = index
        body: list[str] = []
        depth = 0
        while index < len(lines):
            current = lines[index]
            body.append(current)
            depth += current.count("{") - current.count("}")
            index += 1
            if depth == 0 and "}" in current:
                break
        if not declaration_added:
            output.append(f"declare void @{tick}()")
            declaration_added = True
        prefix, blocks = _parse_function(body)
        output.extend(prefix)
        headers = loop_headers(blocks)
        if not headers:
            for block in blocks:
                output.extend(block.lines)
            continue
        for block in blocks:
            output.append(block.lines[0])
            inserted = False
            for line in block.lines[1:]:
                if block.label in headers and not inserted and not line.strip().startswith("phi "):
                    output.append(f"  call void @{tick}()")
                    count += 1
                    inserted = True
                output.append(line)
            if block.label in headers and not inserted:
                output.append(f"  call void @{tick}()")
                count += 1
    return "\n".join(output) + ("\n" if source.endswith("\n") else ""), count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_ll", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbol", default=TICK,
                        help="loop tick symbol (the default is the SemantiST ABI)")
    args = parser.parse_args(argv)
    source = args.input_ll.read_text(encoding="utf-8", errors="replace")
    text, count = instrument(source, args.symbol)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"[loop_budget] inserted {count} loop ticks in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
