"""Corpus metadata, seed encoding/decoding, and validation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from compiler.harness.generator import type_token
from compiler.harness.contract import POINTER_STORAGE_CAPACITY

DEFAULT_ST_STRING_LENGTH = int(os.environ.get("STFUZZER_STRING_LENGTH", "250"))
DEFAULT_BOOTSTRAP_MAX_POINTER_BYTES = int(os.environ.get("STFUZZER_BOOTSTRAP_MAX_POINTER_BYTES", "64"))


def read_metadata_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    if text.lstrip().startswith("["):
        raw = json.loads(text)
        return raw if isinstance(raw, list) else []
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if isinstance(raw, dict):
            records.append(raw)
    return records


def compact_metadata_dict(record: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in record.items():
        if value is None or value == [] or value == {} or value is False:
            continue
        if key == "new_afl_edges" and value == 0:
            continue
        compact[key] = value
    return compact

@dataclass
class StSeedMetadata:
    source: str
    content_hash: str
    seed_id: str = ""
    parent_id: str | None = None
    coverage_hash: str | None = None
    constraints: list[str] = field(default_factory=list)
    target_id: str | None = None
    covered_target: str | None = None
    semantic_coverage: dict[str, Any] = field(default_factory=dict)
    covered_target_ids: list[str] = field(default_factory=list)
    covered_targets_by_cycle: dict[str, list[str]] = field(default_factory=dict)
    cycle_ids: list[int] = field(default_factory=list)
    state_signatures: list[str] = field(default_factory=list)
    target_affinity: dict[str, float] = field(default_factory=dict)
    solver_status: str | None = None
    new_afl_edges: int = 0
    new_st_branch_sides: list[str] = field(default_factory=list)
    new_state_transitions: list[str] = field(default_factory=list)
    is_finding: bool = False
    finding_source: str | None = None
    finding_kind: str | None = None
    finding_severity: str | None = None
    finding_confidence: str | None = None
    finding_detail: str | None = None
    finding_artifact: str | None = None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_seed(data: bytes) -> bytes:
    text = data.decode("utf-8")
    records = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split(",", 2)
        if len(parts) != 3 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(f"invalid structured seed line: {raw_line!r}")
        name = parts[0].strip()
        type_name = parts[1].strip().upper()
        value = parts[2].strip()
        if name.upper().endswith(".__DT") or name.upper() == "__DT":
            try:
                int(value, 0)
            except ValueError as error:
                raise ValueError(f"invalid frame time: {value!r}") from error
        elif name.upper() == "__INIT":
            if type_name != "ENUM" or value != "cold":
                raise ValueError("only cold __INIT is supported")
        else:
            if not value.lower().startswith("0x"):
                raise ValueError(f"seed values must use canonical 0x bytes: {value!r}")
            digits = value[2:]
            if len(digits) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", digits):
                raise ValueError(f"invalid canonical hex bytes: {value!r}")
            value = "0x" + digits.lower()
        records.append(",".join((name, type_name, value)))
    if not records:
        raise ValueError("structured seed contains no records")
    return ("\n".join(records) + "\n").encode("utf-8")


def source_constraints(st_source: str) -> list[str]:
    constraints = []
    for line in st_source.splitlines():
        line = line.strip()
        if re.search(r"\bIF\b|\bMOD\b|\bWHILE\b", line, re.I):
            constraints.append(line)
    return constraints[:20]


class CorpusStore:
    def __init__(self, layout: Layout, constraints: list[str] | None = None):
        self.layout = layout
        self.constraints = constraints or []
        self.records: dict[str, StSeedMetadata] = {}
        self.next_seed_number = 1
        if layout.bootstrap.exists():
            valid_fields = {field.name for field in fields(StSeedMetadata)}
            for index, raw in enumerate(read_metadata_records(layout.bootstrap), start=1):
                metadata = StSeedMetadata(**{key: value for key, value in raw.items() if key in valid_fields})
                # Historical metadata had only a content digest. Keep it as
                # evidence, but never use that digest as the seed identity.
                if not metadata.seed_id:
                    metadata.seed_id = f"legacy-{index:08d}"
                self.records[metadata.seed_id] = metadata
                if metadata.seed_id.startswith("seed-"):
                    try:
                        self.next_seed_number = max(self.next_seed_number,
                                                    int(metadata.seed_id.removeprefix("seed-")) + 1)
                    except ValueError:
                        pass
        for path in layout.unified_seeds.glob("seed-*.seed"):
            try:
                self.next_seed_number = max(self.next_seed_number, int(path.stem.removeprefix("seed-")) + 1)
            except ValueError:
                continue

    def persist(self) -> None:
        records = [
            compact_metadata_dict(asdict(record))
            # Seed identity and ordering come from the sequential file ID.
            # Historical content digests remain diagnostic metadata only.
            for record in sorted(self.records.values(), key=lambda x: x.seed_id)
        ]
        self.layout.bootstrap.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def add_seed(
        self,
        data: bytes,
        source: str,
        parent_id: str | None = None,
        constraints: list[str] | None = None,
    ) -> tuple[StSeedMetadata, bool]:
        data = normalize_seed(data)
        digest = content_hash(data)
        seed_id = f"seed-{self.next_seed_number:08d}"
        self.next_seed_number += 1
        metadata = StSeedMetadata(
            source=source,
            parent_id=parent_id,
            content_hash=digest,
            seed_id=seed_id,
            constraints=list(constraints if constraints is not None else self.constraints),
        )
        self.records[seed_id] = metadata
        self.layout.unified_seeds.mkdir(parents=True, exist_ok=True)
        (self.layout.unified_seeds / f"{seed_id}.seed").write_bytes(data)
        return metadata, True

    def discover_runtime_seeds(self) -> int:
        added = 0
        if not self.layout.unified_runtime.exists():
            return added
        for path in sorted(self.layout.unified_runtime.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.stat().st_size == 0:
                continue
            try:
                _, is_new = self.add_seed(path.read_bytes(), "Fuzzing")
            except (UnicodeDecodeError, ValueError):
                continue
            added += int(is_new)
        return added

    def seed_path(self, seed_id: str) -> Path:
        primary = self.layout.unified_seeds / f"{seed_id}.seed"
        if primary.exists():
            return primary
        metadata = self.records.get(seed_id)
        if metadata and metadata.finding_artifact:
            finding = Path(metadata.finding_artifact)
            if finding.exists():
                return finding
        return primary

    def seed_bytes(self, seed_id: str) -> bytes:
        return self.seed_path(seed_id).read_bytes()


def append_event(layout: Layout, event: str, **details: Any) -> None:
    payload = {"event": event, "unix_time_seconds": int(time.time()), **details}
    with layout.events.open("a", encoding="utf-8") as out:
        out.write(json.dumps(payload, sort_keys=True) + "\n")


def parse_records(seed: bytes) -> dict[str, str]:
    values = {}
    for line in seed.decode("utf-8").splitlines():
        if not line.strip():
            continue
        name, _ty, value = line.split(",", 2)
        values[name.strip().upper()] = value.strip()
    if not values:
        raise ValueError("structured seed contains no records")
    return values


def parse_seed_lines(seed: bytes) -> list[tuple[str, str, str]]:
    records = []
    for raw_line in normalize_seed(seed).decode("utf-8").splitlines():
        name, ty, value = raw_line.split(",", 2)
        records.append((name, ty, value))
    return records


def field_name_without_cycle(name: str) -> str:
    prefix, dot, rest = name.partition(".")
    if dot and prefix.isdigit() and rest:
        return rest
    return name


def scalar_integer_bounds(spec: Any) -> tuple[int, int] | None:
    if spec.kind != "scalar" or spec.name in ("REAL", "LREAL"):
        return None
    if spec.name == "BOOL":
        return (0, 1)
    bits = spec.size * 8
    signed = not (spec.c_type or "").startswith("u")
    if signed:
        return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    return (0, 2**bits - 1)


def validate_seed_for_params(seed: bytes, params: Iterable[Any]) -> bytes:
    normalized = normalize_seed(seed)
    expected = {param.name.upper(): param for param in params}
    seen: set[str] = set()
    for name, ty, value in parse_seed_lines(normalized):
        key = field_name_without_cycle(name).upper()
        if key not in expected:
            raise ValueError(f"unknown seed field {name!r}")
        if "." not in name:
            if key in seen:
                raise ValueError(f"duplicate seed field {name!r}")
            seen.add(key)
        spec = expected[key].spec
        expected_type = type_token(spec)
        if ty != expected_type:
            raise ValueError(f"{name} has type {ty}, expected {expected_type}")
        if value.lower().startswith("0x"):
            digits = value[2:]
            raw = bytes.fromhex(digits)
            if spec.kind == "scalar":
                if len(raw) != spec.size:
                    raise ValueError(f"{name} raw scalar requires ABI width {spec.size}")
                # Raw scalar storage deliberately bypasses the normal value
                # domain, including BOOL bytes such as 03, 80, and ff.
                continue
            if spec.kind == "string":
                if len(raw) > spec.byte_size() or len(raw) % spec.size:
                    raise ValueError(f"{name} raw string exceeds ABI storage")
                continue
            if spec.kind in ("array", "opaque", "pointer"):
                capacity = (POINTER_STORAGE_CAPACITY if spec.kind == "pointer"
                            else spec.byte_size())
                if len(raw) > capacity:
                    raise ValueError(f"{name} raw bytes exceed ABI storage")
                continue
        if spec.kind == "scalar":
            if spec.name in ("REAL", "LREAL"):
                float(value)
            else:
                bounds = scalar_integer_bounds(spec)
                parsed = int(value, 0)
                if bounds and not (bounds[0] <= parsed <= bounds[1]):
                    raise ValueError(f"{name}={parsed} is outside {spec.name} range")
        elif spec.kind == "string":
            if len(value.encode("utf-16-le" if spec.size == 2 else "utf-8")) // spec.size > (spec.length or DEFAULT_ST_STRING_LENGTH):
                raise ValueError(f"{name} string exceeds declared length")
        elif spec.kind in ("array", "opaque", "pointer"):
            if value.startswith(("0x", "0X")):
                raw_hex = value[2:]
                if len(raw_hex) % 2 != 0:
                    raise ValueError(f"{name} hex bytes must have an even digit count")
                if raw_hex and not re.fullmatch(r"[0-9a-fA-F]+", raw_hex):
                    raise ValueError(f"{name} has invalid hex bytes")
                byte_len = len(raw_hex) // 2
            else:
                byte_len = len(value.encode("utf-8"))
            if byte_len <= 0:
                raise ValueError(f"{name} must describe at least one byte")
            capacity = (POINTER_STORAGE_CAPACITY if spec.kind == "pointer"
                        else spec.byte_size() if hasattr(spec, "byte_size")
                        else DEFAULT_BOOTSTRAP_MAX_POINTER_BYTES)
            if byte_len > capacity:
                raise ValueError(f"{name} describes {byte_len} bytes, max ABI capacity is {capacity}")
    seen_base = {field_name_without_cycle(name).upper() for name, _ty, _value in parse_seed_lines(normalized)}
    missing = [param.name for param in params if param.name.upper() not in seen_base]
    if missing:
        raise ValueError(f"missing seed fields: {', '.join(missing)}")
    return normalized



# Compatibility alias for the target architecture terminology.
SeedMetadata = StSeedMetadata
