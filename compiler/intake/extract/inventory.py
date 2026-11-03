"""Inventory of raw CODESYS ``.library`` containers, including nested ones.

A ``.library`` that is a ZIP container stores its ST in plain text and is read
by :mod:`.whole_xml`. Files starting with ``23 89 ed 33`` and showing about
8 bits/byte of entropy are a compressed or encrypted format without a public
description; they are listed here as raw material that is not ingested.
"""

from __future__ import annotations

import collections
import hashlib
import io
import math
import zipfile
from pathlib import Path

ENCRYPTED_MAGIC = bytes.fromhex("2389ed33")
MAX_NESTED_SIZE = 512 << 20


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    size = len(data)
    return -sum(count / size * math.log2(count / size) for count in counts.values())


def classify(data: bytes) -> str:
    if data.startswith(b"PK"):
        return "zip"
    if data.startswith(ENCRYPTED_MAGIC):
        return "compressed_or_encrypted"
    return "unknown"


def library_inventory(corpus: Path) -> dict:
    occurrences: list[dict] = []

    def visit(chain: str, data: bytes, depth: int) -> None:
        if chain.lower().endswith(".library"):
            kind = classify(data)
            occurrences.append({
                "chain": chain,
                "format": kind,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "entropy_bits_per_byte": round(entropy(data[: 1 << 20]), 3),
                "ingested": kind == "zip",
            })
            return
        if depth >= 3 or not data.startswith(b"PK"):
            return
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    name = info.filename.lower()
                    if info.is_dir() or info.file_size > MAX_NESTED_SIZE:
                        continue
                    if name.endswith((".library", ".zip")):
                        visit(chain + "!" + info.filename, archive.read(info), depth + 1)
        except zipfile.BadZipFile:
            return

    for path in sorted(corpus.glob("[0-9]*/原始库文件/**/*")):
        if path.is_file() and path.suffix.lower() in {".library", ".zip"} and path.stat().st_size < (1 << 30):
            visit(path.relative_to(corpus).as_posix(), path.read_bytes(), 0)
    unique = {}
    for item in occurrences:
        unique.setdefault(item["sha256"], item)
    counts = collections.Counter(item["format"] for item in occurrences)
    unique_counts = collections.Counter(item["format"] for item in unique.values())
    return {
        "schema_version": "semantist.raw-library-inventory/1.0.0",
        "occurrences": dict(sorted(counts.items())),
        "unique_files": dict(sorted(unique_counts.items())),
        "not_ingested": [item for item in occurrences if not item["ingested"]],
        "libraries": occurrences,
    }
