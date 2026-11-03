"""Deterministic retrieval of vendor manual passages for stub generation.

The LLM reads only passages of the manuals collected under
``rusty_compatibility/<vendor>/`` (text, Markdown and HTML files outside the
library sources), never its own search results. A passage is found by the
exact name as a whole word; passages of the family's own vendor directories
rank first, then files with more occurrences, then the path. Every passage
carries its file, line and SHA-256, so the generation record shows what the
model was given.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from .. import corpus

ROOT = Path(__file__).resolve().parents[3]
MANUALS = ROOT / "rusty_compatibility"
SUFFIXES = {".txt", ".md", ".html", ".htm"}
EXCLUDED_PARTS = {"原始库文件", ".st文件", "提取脚本"}
CONTEXT_LINES = 10
PASSAGES_PER_NAME = 2
_TAG = re.compile(r"<[^>]+>")


def manual_files(root: Path = MANUALS) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() in SUFFIXES and path.is_file() and not EXCLUDED_PARTS & set(path.parts):
            # An HTML page and its extracted .html.txt are the same text.
            if path.suffix.lower() in {".html", ".htm"} and path.with_name(path.name + ".txt").exists():
                continue
            files.append(path)
    return files


def _lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        text = html.unescape(_TAG.sub(" ", text))
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def vendor_dirs(family: str) -> set[str]:
    return {v.directory for v in corpus.VENDORS if v.family == family}


def find(names_by_family: dict[str, set[str]], root: Path = MANUALS) -> dict[tuple[str, str], list[dict]]:
    """Passages per (family, NAME) for all requested names, in one pass."""
    wanted = {name.upper() for names in names_by_family.values() for name in names}
    if not wanted:
        return {}
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, wanted), key=lambda n: (-len(n), n))) + r")\b",
                         re.IGNORECASE)
    hits: dict[str, list[tuple[Path, list[str], list[int]]]] = {}
    for path in manual_files(root):
        lines = _lines(path)
        per_name: dict[str, list[int]] = {}
        for number, line in enumerate(lines):
            for match in pattern.finditer(line):
                per_name.setdefault(match.group(1).upper(), []).append(number)
        for name, numbers in per_name.items():
            hits.setdefault(name, []).append((path, lines, numbers))
    result: dict[tuple[str, str], list[dict]] = {}
    for family, names in names_by_family.items():
        own = vendor_dirs(family)
        for name in sorted(n.upper() for n in names):
            candidates = sorted(hits.get(name, []), key=lambda item: (
                item[0].relative_to(root).parts[0] not in own, -len(item[2]), str(item[0])))
            passages = []
            for path, lines, numbers in candidates[:PASSAGES_PER_NAME]:
                first = numbers[0]
                start, end = max(0, first - CONTEXT_LINES), min(len(lines), first + CONTEXT_LINES + 1)
                text = "\n".join(lines[start:end])
                shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.relative_to(root)
                passages.append({"file": str(shown), "line": first + 1, "text": text,
                                 "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
            result[(family, name)] = passages
    return result
