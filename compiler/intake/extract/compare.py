"""Compare an extraction run with the collected ``.st文件`` sources.

The collected files were produced on Windows. Their legacy headers name the
original container by an absolute path below ``D:\\IIOT\\projects\\rusty兼容层``;
this run names it relative to the corpus root. The comparison rewrites only that
path prefix in ``(* Original library/project: ... *)`` lines and then requires
byte equality. Every remaining difference is listed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

WINDOWS_ROOT = "D:\\IIOT\\projects\\rusty兼容层\\"
HEADER = re.compile(r"(?m)^\(\* Original library/project: " + re.escape(WINDOWS_ROOT) + r"([^!\n]*?)(?=!| \*\))")


def normalize_reference(text: str) -> tuple[str, int]:
    """Rewrite Windows corpus paths in legacy headers to relative POSIX paths."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "(* Original library/project: " + match.group(1).replace("\\", "/")

    return HEADER.sub(replace, text), count


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compare(work: Path, reference: Path, output: Path) -> int:
    out = work / "outputs"
    extracted = {p.relative_to(out).as_posix(): p for p in sorted(out.glob("*/.st文件/*.st"))}
    collected = {p.relative_to(reference).as_posix(): p for p in sorted(reference.glob("[0-9]*/.st文件/*.st"))}
    normalized: dict[str, bytes] = {}
    header_rewrites = 0
    for name, path in collected.items():
        text, count = normalize_reference(path.read_bytes().decode("utf-8"))
        header_rewrites += count
        normalized[name] = text.encode("utf-8")
    identical, differing = [], []
    for name in sorted(set(extracted) & set(collected)):
        if extracted[name].read_bytes() == normalized[name]:
            identical.append(name)
        else:
            differing.append(name)
    missing = sorted(set(collected) - set(extracted))
    extra = sorted(set(extracted) - set(collected))
    by_content = {}
    for name in extra:
        by_content.setdefault(digest(extracted[name].read_bytes()), []).append(name)
    renamed = []
    for name in missing:
        matches = by_content.get(digest(normalized[name]), [])
        if matches:
            renamed.append({"collected": name, "extracted": matches})
    report = {
        "schema_version": "semantist.extraction-comparison/1.0.0",
        "collected_files": len(collected),
        "extracted_files": len(extracted),
        "identical_after_header_normalization": len(identical),
        "header_path_rewrites": header_rewrites,
        "normalization": "Windows absolute corpus paths in '(* Original library/project: ...' headers are "
                         "rewritten to corpus-relative POSIX paths; nothing else is changed",
        "differing_content": differing,
        "missing_from_extraction": missing,
        "extra_in_extraction": extra,
        "same_content_different_name": renamed,
        "status": "identical" if not (differing or missing or extra) else "different",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(identical)}/{len(collected)} identical; {len(differing)} differing, "
          f"{len(missing)} missing, {len(extra)} extra -> {output}")
    return 0 if report["status"] == "identical" else 1
