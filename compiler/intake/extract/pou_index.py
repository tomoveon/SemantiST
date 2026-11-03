"""POU-level provenance for an extraction run.

For every program organisation unit in every extracted file this records the
output location, the nearest source marker the extractor wrote in front of it,
the original container chain and, when the original member is text, the line of
the declaration in that member (for binary members: the byte offset). The
result lets a finding in normalized code be traced back to a vendor file.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import OrderedDict
from pathlib import Path

from compiler.intake.structure import split_units

_DECLARATION = (
    r"(FUNCTION_BLOCK|FUNCTIONBLOCK|FUNCTION|PROGRAM|CLASS|INTERFACE|DATA_BLOCK|ORGANIZATION_BLOCK|METHOD|"
    r"PROPERTY|ACTION|TYPE)\b(?:[ \t]+(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|ABSTRACT|FINAL|OVERRIDE))*"
    r"\s+\"?([A-Za-z_][\w.]*)\"?"
)
# Text members: a declaration starts a line (or a TwinCAT CDATA section).
HEADER = re.compile(r"(?im)(?:^|<!\[CDATA\[)[ \t]*(?:\{[^}\n]*\}[ \t]*)*" + _DECLARATION)
# Binary source records (CoDeSys+ .lib/.pro) start a declaration after a length field.
BINARY_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9_])" + _DECLARATION)


def units(text: str) -> list[dict]:
    """Top-level units of one extracted file as JSON-ready records."""
    return [
        {
            "kind": unit.kind,
            "name": unit.qualified_name,
            "line": unit.line,
            "end_line": unit.end_line,
            "marker": unit.marker,
            "members": [{"kind": m.kind, "name": m.name, "line": m.line} for m in unit.members],
        }
        for unit in split_units(text)
    ]


class ContainerReader:
    """Read members of nested archives addressed by provenance chains."""

    def __init__(self, root: Path, cache_size: int = 32):
        self.root = root
        self.cache: OrderedDict[str, zipfile.ZipFile] = OrderedDict()
        self.cache_size = cache_size

    def _zip(self, key: str, opener) -> zipfile.ZipFile | None:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        try:
            archive = opener()
        except (OSError, zipfile.BadZipFile, KeyError, ValueError):
            return None
        self.cache[key] = archive
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return archive

    def read(self, chain: str) -> bytes | None:
        chain = chain.split("#", 1)[0]
        parts = [part.lstrip("/") for part in chain.split("!")]
        first = self.root / parts[0]
        if len(parts) == 1:
            return first.read_bytes() if first.is_file() else None
        archive = self._zip(parts[0], lambda: zipfile.ZipFile(first))
        for depth, member in enumerate(parts[1:], start=1):
            if archive is None:
                return None
            try:
                data = archive.read(member)
            except KeyError:
                return None
            if depth == len(parts) - 1:
                return data
            key = "!".join(parts[: depth + 1])
            archive = self._zip(key, lambda data=data: zipfile.ZipFile(io.BytesIO(data)))
        return None


def _declaration_positions(data: bytes) -> dict[tuple[str, str], dict]:
    binary = b"\0" in data[:4096] or data.startswith(b"CoDeSys+")
    text = data.decode("latin-1") if binary else data.decode("utf-8", errors="replace")
    positions: dict[tuple[str, str], dict] = {}
    for match in (BINARY_HEADER if binary else HEADER).finditer(text):
        kind = match.group(1).upper().replace("FUNCTIONBLOCK", "FUNCTION_BLOCK")
        key = (kind, match.group(2).casefold())
        if key in positions:
            continue
        if binary:
            positions[key] = {"offset": match.start(1)}
        else:
            positions[key] = {"line": text.count("\n", 0, match.start(1)) + 1}
    return positions


def build_index(out: Path, provenance_dir: Path, corpus: Path, fixed_inputs: list[dict],
                curation: list[dict] = ()) -> dict:
    # Records are keyed by "<vendor>/.st文件/<name>" as written by the
    # extractors; curation renames and merges are then replayed onto them.
    records: dict[str, dict] = {}
    for path in sorted(provenance_dir.glob("*.json")):
        for record in json.loads(path.read_text(encoding="utf-8")):
            records["/".join(Path(record["file"]).parts[-3:])] = record
    for step in curation:
        folder = f"{step['vendor']}/.st文件/"
        if step["action"] == "rename" and folder + step["from"] in records:
            records[folder + step["to"]] = records.pop(folder + step["from"])
        elif step["action"] == "merge":
            merged = [records.pop(folder + name, {}) for name in step["members"]]
            records[folder + step["name"]] = {
                "sources": [s for record in merged for s in record.get("sources", [])],
                "pous": [p for record in merged for p in record.get("pous", [])],
            }
    fixed = {f"{item['vendor']}/.st文件/{item['name']}": item for item in fixed_inputs}
    reader = ContainerReader(corpus)
    positions_cache: OrderedDict[str, dict] = OrderedDict()

    def positions(chain: str) -> dict:
        if chain in positions_cache:
            return positions_cache[chain]
        data = reader.read(chain)
        found = _declaration_positions(data) if data is not None else {}
        positions_cache[chain] = found
        if len(positions_cache) > 256:
            positions_cache.popitem(last=False)
        return found

    files: dict[str, dict] = {}
    for path in sorted(out.glob("*/.st文件/*.st")):
        relative = path.relative_to(out).as_posix()
        record = records.get(relative, {})
        sources = record.get("sources", [])
        explicit = {(p["kind"], p["name"].casefold()): p for p in record.get("pous", [])}
        fixed_input = fixed.get(relative)
        entries = []
        for unit in units(path.read_text(encoding="utf-8")):
            entry = dict(unit)
            short = unit["name"].rsplit(".", 1)[-1].casefold()
            origin = explicit.get((unit["kind"], short))
            if origin:
                entry["source"] = origin["source"]
                entry["element"] = origin.get("element")
            elif fixed_input:
                # Copied verbatim: the collected file is the original.
                entry["source"] = fixed_input["origin"]
                entry["source_line"] = unit["line"]
                entry["element"] = "fixed input"
            else:
                marker = (unit["marker"] or "").split(" + ")[0]
                marked = [s for s in sources if marker and marker in s] if marker else []
                key = (unit["kind"], short)
                for chain in marked or sources:
                    found = positions(chain).get(key) if short else None
                    if found:
                        entry["source"] = chain
                        entry.update({"source_" + k: v for k, v in found.items()})
                        break
                else:
                    if marked:
                        entry["source"] = marked[0]
                        entry["element"] = unit["marker"]
                    elif len(sources) == 1:
                        entry["source"] = sources[0]
                    elif sources:
                        entry["candidate_sources"] = sources[:20]
            entries.append(entry)
        files[relative] = {
            "sources": sources,
            "fixed_input": fixed_input,
            "units": entries,
        }
    return {"schema_version": "semantist.pou-index/1.0.0", "files": files}
