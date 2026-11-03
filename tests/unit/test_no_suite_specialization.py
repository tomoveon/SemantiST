"""R1 guard: the tool must not encode defect-suite constants or names."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN = [
    ROOT / "fuzzer" / "v3",
    ROOT / "fuzzer" / "symbolic",
    ROOT / "compiler" / "symbolic",
    ROOT / "fuzzer" / "llm",
]
SUITE_NAMES = re.compile(r"\b(D0[1-9]|K[1-6])\b")
VARIABLE_NAMES = re.compile(r"\b(Head|Draining|Locked|Ready|AckBatch|BatchId|overTimer|holdTimer)\b")
TRIGGER_CONSTANTS = re.compile(
    r"(?<![0-9A-Za-z_])(101|232|233|700|800|801|1450|1500|1550|4700|4711|4718|4719|3\.14|3\.68|4\.13)(?![0-9A-Za-z_])"
)


def _sources():
    for root in SCAN:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".rs", ".c", ".h"} or not path.is_file():
                continue
            yield path


def test_no_defect_case_names_or_trigger_constants():
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in (
            (SUITE_NAMES, "case id"),
            (VARIABLE_NAMES, "defect variable name"),
            (TRIGGER_CONSTANTS, "defect trigger constant"),
        ):
            match = pattern.search(text)
            if match:
                offenders.append(f"{path.relative_to(ROOT)}: {label} {match.group(0)!r}")
    assert not offenders, "\n".join(offenders)
