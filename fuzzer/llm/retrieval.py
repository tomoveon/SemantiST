"""Frozen OSCAT manual retrieval for target-scoped LLM requests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from pathlib import Path


MANUALS = {
    "oscat_basic": (
        "oscat_basic333_en.pdf",
        "https://raw.githubusercontent.com/eclipse-oscat/oscat-libs-archive/main/"
        "OSCAT_BASIC_LIB/ARCHIVE/rel333/oscat_basic333_en.pdf",
    ),
    "oscat_network": (
        "oscat_netlib135_de.pdf",
        "https://raw.githubusercontent.com/eclipse-oscat/oscat-libs-archive/main/"
        "OSCAT_NETWORK_LIB/Archive/net135/oscat_netlib135_de.pdf",
    ),
    "oscat_building": (
        "oscat_building100_en.pdf",
        "https://raw.githubusercontent.com/eclipse-oscat/oscat-libs-archive/main/"
        "OSCAT_BUILDING_LIB/Archive/rel100/oscat_building100_en.pdf",
    ),
}


def _chunks(text: str, maximum: int = 1900) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines()]
    result: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > maximum and current:
            result.append("\n".join(current).strip())
            current = current[-2:]
            length = sum(len(item) + 1 for item in current)
        current.append(line)
        length += len(line) + 1
    if current:
        result.append("\n".join(current).strip())
    return [part for part in result if part]


def prepare_knowledge(root: Path) -> Path:
    """Download manuals once and persist page-addressed SQLite FTS5 chunks."""
    directory = root / "knowledge"
    cache = Path(__file__).resolve().parents[2] / "artifacts" / "oscat-manual-cache"
    database = directory / "manuals.sqlite3"
    metadata = directory / "manuals.json"
    if database.is_file() and metadata.is_file():
        return database
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    temporary = directory / "manuals.sqlite3.tmp"
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("CREATE VIRTUAL TABLE chunks USING fts5("
                           "suite UNINDEXED, document UNINDEXED, page UNINDEXED, text)")
        for suite, (filename, url) in MANUALS.items():
            pdf = directory / filename
            if not pdf.is_file():
                cache.mkdir(parents=True, exist_ok=True)
                cached = cache / filename
                if not cached.is_file():
                    incoming = cached.with_suffix(".pdf.tmp")
                    try:
                        with urllib.request.urlopen(url, timeout=90) as response, incoming.open("wb") as output:
                            while block := response.read(1024 * 1024):
                                output.write(block)
                        if not incoming.read_bytes().startswith(b"%PDF-"):
                            raise ValueError(f"download is not a PDF: {url}")
                        incoming.replace(cached)
                    finally:
                        incoming.unlink(missing_ok=True)
                shutil.copy2(cached, pdf)
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            extracted = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
                check=True, capture_output=True, text=True).stdout
            pages = extracted.split("\f")
            for page_number, page in enumerate(pages, start=1):
                for chunk in _chunks(page):
                    connection.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)",
                                       (suite, filename, page_number, chunk))
            records.append({"suite": suite, "file": filename, "url": url,
                            "sha256": digest, "pages": len(pages)})
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    temporary.replace(database)
    metadata.write_text(json.dumps({"schema_version": "semantist.manual-index/1",
                                    "manuals": records}, indent=2) + "\n", encoding="utf-8")
    return database


def retrieve_manual(root: Path, suite: str, function: str, *, limit: int = 4) -> list[dict]:
    database = root / "knowledge" / "manuals.sqlite3"
    if not database.is_file() or suite not in MANUALS:
        return []
    symbol = re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", function)
    if symbol is None:
        raise ValueError(f"invalid target function name: {function!r}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT document, page, text, bm25(chunks) FROM chunks "
            "WHERE chunks MATCH ? AND suite = ? AND CAST(page AS INTEGER) > 4 "
            "ORDER BY bm25(chunks) LIMIT ?",
            (f'"{function}"', suite, max(1, limit * 16)),
        ).fetchall()
    finally:
        connection.close()
    body_rows = [row for row in rows if not (
        row[2].count("....") > 3 and not re.search(r"\b(?:Input|Output|Eingang|Ausgang)\b",
                                                      row[2], re.IGNORECASE))]
    ranked = sorted(body_rows or rows, key=lambda row: (
        -int(bool(re.search(rf"(?m)^\s*(?:\d+(?:\.\d+)*\s+)?{re.escape(function)}\b",
                            row[2], re.IGNORECASE))),
        row[3],
    ))
    return [{"source": row[0], "page": int(row[1]), "text": row[2]}
            for row in ranked[:limit]]
