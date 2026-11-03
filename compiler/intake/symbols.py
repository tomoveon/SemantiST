"""Names defined by ST sources (POUs, types, methods), from the lossless structure."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .structure import split_units

DEFINING_KINDS = {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS", "INTERFACE", "TYPE", "DATA_BLOCK",
                  "ORGANIZATION_BLOCK"}


def defined_in_text(text: str) -> set[str]:
    names: set[str] = set()
    for unit in split_units(text):
        if unit.kind in DEFINING_KINDS and unit.name:
            names.add(unit.name.upper())
            if unit.qualified_name:
                names.add(unit.qualified_name.upper())
    return names


def defined_names(paths: Iterable[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        names |= defined_in_text(Path(path).read_text(encoding="utf-8", errors="replace"))
    return names
