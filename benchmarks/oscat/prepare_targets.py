#!/usr/bin/env python3
"""Extract the selected OSCAT POUs without modifying their bodies."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from compiler.scripts.genfunction import extract_pou
from compiler.parser.st import significant, tokenize_lossless

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("manifest.json")


def normalize_cross_library_source(source: str) -> str:
    """Flatten Basic qualifiers in extracted Network/Building POUs."""
    tokens = significant(tokenize_lossless(source))
    edits: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if (token.upper == "OSCAT_BASIC" and index + 2 < len(tokens)
                and tokens[index + 1].text == "." and tokens[index + 2].is_word):
            dot = tokens[index + 1]
            if source[token.end:dot.start].strip() or source[dot.end:tokens[index + 2].start].strip():
                raise ValueError("comment inside OSCAT_BASIC qualifier")
            edits.append((token.start, dot.end, ""))
        if (token.upper in {"VAR_INPUT", "VAR_IN_OUT"} and index + 1 < len(tokens)
                and tokens[index + 1].upper == "CONSTANT"):
            qualifier = tokens[index + 1]
            edits.append((qualifier.start, qualifier.end, "(* CONSTANT *)"))
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    return source


def prepare(*, check: bool) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets = manifest["targets"]
    if len(targets) != manifest["target_count"] or len({item["id"] for item in targets}) != len(targets):
        raise SystemExit("OSCAT target count or IDs are inconsistent")
    actual_counts = Counter(item["suite"] for item in targets)
    if dict(actual_counts) != manifest["suite_counts"]:
        raise SystemExit(f"OSCAT suite counts differ from manifest: {dict(actual_counts)}")
    for item in targets:
        source = ROOT / item["source_file"]
        destination = ROOT / item["st_file"]
        actual, kind, body = extract_pou(source.read_text(encoding="utf-8"), item["function"])
        if actual != item["function"] or kind != item["kind"]:
            raise SystemExit(f"{item['id']}: source POU differs from manifest")
        if check:
            if not destination.is_file() or destination.read_text(encoding="utf-8") != body:
                raise SystemExit(f"{item['id']}: extracted target is missing or differs from source")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(body, encoding="utf-8")
        normalized_name = item.get("normalized_file")
        if normalized_name:
            normalized_path = ROOT / normalized_name
            normalization = item.get("normalization")
            if normalization == "oscat_basic_namespace":
                normalized_body = normalize_cross_library_source(body)
                if check:
                    if not normalized_path.is_file() or normalized_path.read_text(encoding="utf-8") != normalized_body:
                        raise SystemExit(f"{item['id']}: normalized target is missing or stale")
                else:
                    normalized_path.write_text(normalized_body, encoding="utf-8")
            elif normalization is None:
                if not normalized_path.is_file():
                    raise SystemExit(f"{item['id']}: normalized target is missing")
            else:
                raise SystemExit(f"{item['id']}: unknown normalization {normalization}")
    return len(targets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify generated targets without writing")
    args = parser.parse_args()
    count = prepare(check=args.check)
    print(f"[oscat] {'verified' if args.check else 'extracted'} {count} targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
