"""Manifest loading and validation for the 90-target benchmark set."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .hashing import sha256_file

MANIFEST_SCHEMA = "semantist.external-benchmarks/1.0.0"
EXPECTED_TARGET_COUNT = 90
EXPECTED_SUITE_COUNTS = {
    "oscat_basic": 61,
    "icsquartz_icsfuzz": 17,
    "icsquartz_scan_cycle": 12,
}
REQUIRED_TARGET_FIELDS = {
    "suite",
    "id",
    "kind",
    "function",
    "st_file",
    "upstream_repo",
    "upstream_commit",
    "upstream_path",
    "upstream_sha256",
    "lineage",
    "transformations",
}


@dataclass(frozen=True)
class Target:
    suite: str
    target_id: str
    kind: str
    function: str
    st_file: Path
    st_file_rel: str
    st_file_sha256: str
    compatibility_manifest: Path | None
    compatibility_manifest_rel: str | None
    compatibility_manifest_sha256: str | None
    raw: dict[str, object]

    @property
    def ground_truth_status(self) -> str:
        if self.suite in {"icsquartz_icsfuzz", "icsquartz_scan_cycle"}:
            return "known_fault"
        return "unknown_ground_truth"


@dataclass(frozen=True)
class BenchmarkManifest:
    path: Path
    sha256: str
    raw: dict[str, object]
    targets: list[Target]


def _resolve_under_root(semantist_root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = semantist_root / path
    return path.resolve()


def load_manifest(path: Path, semantist_root: Path, *, strict: bool = True) -> BenchmarkManifest:
    path = path.resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if raw.get("schema_version") != MANIFEST_SCHEMA:
        errors.append(f"schema_version must be {MANIFEST_SCHEMA!r}")
    if raw.get("target_count") != EXPECTED_TARGET_COUNT:
        errors.append(f"target_count must be {EXPECTED_TARGET_COUNT}")
    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list):
        errors.append("targets must be an array")
        raw_targets = []
    if len(raw_targets) != raw.get("target_count"):
        errors.append("len(targets) must match target_count")
    suite_counts = Counter(t.get("suite") for t in raw_targets if isinstance(t, dict))
    if suite_counts != EXPECTED_SUITE_COUNTS:
        errors.append(f"suite counts must be {EXPECTED_SUITE_COUNTS}, got {dict(suite_counts)}")

    targets: list[Target] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            errors.append(f"targets[{index}] must be an object")
            continue
        missing = sorted(REQUIRED_TARGET_FIELDS - item.keys())
        if missing:
            errors.append(f"target {item.get('id', index)!r} missing fields: {missing}")
            continue
        target_id = str(item["id"])
        if target_id in seen_ids:
            errors.append(f"duplicate target id: {target_id}")
        seen_ids.add(target_id)
        st_file_rel = str(item["st_file"])
        st_file = _resolve_under_root(semantist_root, st_file_rel)
        if not st_file.is_file():
            errors.append(f"target {target_id}: st_file does not exist: {st_file}")
            st_hash = ""
        else:
            st_hash = sha256_file(st_file)
        compat_rel = item.get("compatibility_manifest")
        compat_path: Path | None = None
        compat_hash: str | None = None
        if compat_rel is not None:
            compat_path = _resolve_under_root(semantist_root, str(compat_rel))
            if not compat_path.is_file():
                errors.append(
                    f"target {target_id}: compatibility_manifest does not exist: {compat_path}"
                )
            else:
                compat_hash = sha256_file(compat_path)
        targets.append(
            Target(
                suite=str(item["suite"]),
                target_id=target_id,
                kind=str(item["kind"]),
                function=str(item["function"]),
                st_file=st_file,
                st_file_rel=st_file_rel,
                st_file_sha256=st_hash,
                compatibility_manifest=compat_path,
                compatibility_manifest_rel=str(compat_rel) if compat_rel is not None else None,
                compatibility_manifest_sha256=compat_hash,
                raw=item,
            )
        )
    if strict and errors:
        raise ValueError("invalid benchmark manifest:\n" + "\n".join(f"- {e}" for e in errors))
    return BenchmarkManifest(path=path, sha256=sha256_file(path), raw=raw, targets=targets)


def select_targets(
    targets: Iterable[Target], selected_ids: Iterable[str] | None, limit: int | None
) -> list[Target]:
    selected = list(targets)
    if selected_ids:
        requested = list(dict.fromkeys(selected_ids))
        by_id = {target.target_id: target for target in selected}
        missing = [target_id for target_id in requested if target_id not in by_id]
        if missing:
            raise ValueError("unknown target id(s): " + ", ".join(missing))
        selected = [by_id[target_id] for target_id in requested]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit-targets must be positive")
        selected = selected[:limit]
    return selected


def experiment_manifest_entry(target: Target, tools: Iterable[str]) -> dict[str, object]:
    return {
        "target_id": target.target_id,
        "suite": target.suite,
        "kind": target.kind,
        "function": target.function,
        "st_file": str(target.st_file),
        "st_file_sha256": target.st_file_sha256,
        "compatibility_manifest": str(target.compatibility_manifest)
        if target.compatibility_manifest is not None
        else None,
        "compatibility_manifest_sha256": target.compatibility_manifest_sha256,
        "upstream_repo": target.raw["upstream_repo"],
        "upstream_commit": target.raw["upstream_commit"],
        "upstream_path": target.raw["upstream_path"],
        "upstream_sha256": target.raw["upstream_sha256"],
        "ground_truth_status": target.ground_truth_status,
        "tool_support": {tool: "pending" for tool in tools},
    }
