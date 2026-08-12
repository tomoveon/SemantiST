"""Lightweight finding deduplication for runtime error records.

Groups raw runtime/semantic findings by signature so that repeated
manifestations of the same error are counted as duplicates rather than
separate findings.  Each deduplicated record keeps a few representative
seeds and a duplicate count.

Signature priority (best-effort, no complex sanitizer parsing):
1. semantic:  function + semantic_target_id + hazard_kind + source_location
2. runtime:   function + exit_kind + stack_hash / source_location
3. fallback:  function + exit_kind + seed_content_hash

The module is intentionally simple — no root-cause analysis, no multi-round
stability testing, no external dependencies beyond the stdlib.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingSignature:
    """Hashable, order-independent key for deduplication."""

    function: str
    kind: str  # crash | timeout | oom | nonzero | semantic_violation
    tier: str  # "semantic" | "runtime" | "fallback"

    # semantic tier fields
    semantic_target_id: str | None = None
    hazard_kind: str | None = None
    source_location: str | None = None

    # runtime / fallback tier fields
    exit_kind: str | None = None
    stack_hash: str | None = None
    target_kind: str | None = None
    cycle_count: int | None = None
    seed_content_hash: str | None = None

    def key(self) -> str:
        """Stable string key for grouping / display."""
        if self.tier == "semantic":
            return (
                f"semantic::{self.function}::{self.semantic_target_id or '?'}"
                f"::{self.hazard_kind or '?'}::{self.source_location or '?'}"
            )
        if self.tier == "runtime":
            return (
                f"runtime::{self.function}::{self.exit_kind or self.kind}"
                f"::{self.stack_hash or self.source_location or '?'}"
            )
        # fallback
        return (
            f"fallback::{self.function}::{self.exit_kind or self.kind}"
            f"::{self.target_kind or '?'}::{self.cycle_count if self.cycle_count is not None else '?'}"
        )

    def display(self) -> str:
        """Human-readable one-line summary."""
        if self.tier == "semantic":
            return (
                f"[semantic] {self.function} "
                f"target={self.semantic_target_id or '?'} "
                f"hazard={self.hazard_kind or '?'} "
                f"loc={self.source_location or '?'}"
            )
        if self.tier == "runtime":
            return (
                f"[runtime] {self.function} "
                f"exit={self.exit_kind or self.kind} "
                f"stack={self.stack_hash or '?'}"
            )
        return (
            f"[fallback] {self.function} "
            f"exit={self.exit_kind or self.kind} "
            f"target_kind={self.target_kind or '?'} "
            f"cycles={self.cycle_count if self.cycle_count is not None else '?'}"
        )


# ---------------------------------------------------------------------------
# Finding record
# ---------------------------------------------------------------------------


@dataclass
class FindingRecord:
    """One deduplicated runtime error record."""

    # Identity
    signature: FindingSignature
    error_kind: str  # crash | timeout | oom | nonzero | semantic_violation
    function: str

    # Representative seeds (up to MAX_REPRESENTATIVE_SEEDS)
    representative_seeds: list[str] = field(default_factory=list)
    duplicate_count: int = 0

    # Timestamps
    first_seen: str = ""  # ISO 8601
    last_seen: str = ""

    # Replay status for the first representative seed
    replay_status: str = "unchecked"  # confirmed | unstable | candidate | unchecked
    replay_returncode: int | None = None
    replay_stderr_preview: str = ""

    # Semantic evidence (best-effort)
    semantic_evidence: dict[str, Any] = field(default_factory=dict)

    # Source location hint
    source_location: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "finding-record-1.0",
            "error_kind": self.error_kind,
            "function": self.function,
            "signature": self.signature.key(),
            "signature_detail": {
                "tier": self.signature.tier,
                "kind": self.signature.kind,
                "semantic_target_id": self.signature.semantic_target_id,
                "hazard_kind": self.signature.hazard_kind,
                "source_location": self.signature.source_location,
                "exit_kind": self.signature.exit_kind,
                "stack_hash": self.signature.stack_hash,
                "target_kind": self.signature.target_kind,
                "cycle_count": self.signature.cycle_count,
                "seed_content_hash": self.signature.seed_content_hash,
            },
            "representative_seeds": self.representative_seeds,
            "duplicate_count": self.duplicate_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "replay_status": self.replay_status,
            "replay_returncode": self.replay_returncode,
            "replay_stderr_preview": self.replay_stderr_preview[:2000],
            "semantic_evidence": self.semantic_evidence,
            "source_location": self.source_location,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FindingRecord:
        sig_detail = data.get("signature_detail", {})
        sig = FindingSignature(
            function=data.get("function", "unknown"),
            kind=data.get("error_kind", sig_detail.get("kind", "unknown")),
            tier=sig_detail.get("tier", "fallback"),
            semantic_target_id=sig_detail.get("semantic_target_id"),
            hazard_kind=sig_detail.get("hazard_kind"),
            source_location=sig_detail.get("source_location") or data.get("source_location"),
            exit_kind=sig_detail.get("exit_kind"),
            stack_hash=sig_detail.get("stack_hash"),
            target_kind=sig_detail.get("target_kind"),
            cycle_count=sig_detail.get("cycle_count"),
            seed_content_hash=sig_detail.get("seed_content_hash"),
        )
        return cls(
            signature=sig,
            error_kind=data.get("error_kind", "unknown"),
            function=data.get("function", "unknown"),
            representative_seeds=data.get("representative_seeds", []),
            duplicate_count=data.get("duplicate_count", 0),
            first_seen=data.get("first_seen", ""),
            last_seen=data.get("last_seen", ""),
            replay_status=data.get("replay_status", "unchecked"),
            replay_returncode=data.get("replay_returncode"),
            replay_stderr_preview=data.get("replay_stderr_preview", ""),
            semantic_evidence=data.get("semantic_evidence", {}),
            source_location=data.get("source_location"),
        )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

MAX_REPRESENTATIVE_SEEDS = 3


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed_content_hash(reproducer_path: Path | None) -> str | None:
    """Return SHA-256 of the seed file content, or None if unavailable."""
    if reproducer_path is None or not reproducer_path.is_file():
        return None
    try:
        return _sha256_hex(reproducer_path.read_bytes())
    except OSError:
        return None


def build_signature(
    *,
    function: str,
    kind: str,
    # semantic tier
    semantic_target_id: str | None = None,
    hazard_kind: str | None = None,
    source_location: str | None = None,
    # runtime tier
    exit_kind: str | None = None,
    stack_hash: str | None = None,
    # fallback
    target_kind: str | None = None,
    cycle_count: int | None = None,
    seed_content_hash: str | None = None,
) -> FindingSignature:
    """Build the best-available signature for a raw finding.

    Priority: semantic > runtime > fallback.
    """
    # Tier 1: semantic
    if semantic_target_id or hazard_kind:
        return FindingSignature(
            function=function,
            kind=kind,
            tier="semantic",
            semantic_target_id=semantic_target_id,
            hazard_kind=hazard_kind,
            source_location=source_location,
        )

    # Tier 2: runtime (stack hash or source location available)
    if stack_hash or source_location:
        return FindingSignature(
            function=function,
            kind=kind,
            tier="runtime",
            exit_kind=exit_kind or kind,
            stack_hash=stack_hash,
            source_location=source_location,
            target_kind=target_kind,
            cycle_count=cycle_count,
        )

    # Tier 3: fallback.  Keep this intentionally coarse so ordinary runtime
        # crashes in the same function collapse to a stable representative
    # error record instead of one record per input hash.
    return FindingSignature(
        function=function,
        kind=kind,
        tier="fallback",
        exit_kind=exit_kind or kind,
        target_kind=target_kind,
        cycle_count=cycle_count,
        seed_content_hash=seed_content_hash,
    )


class FindingDeduplicator:
    """Collects raw findings and groups them into deduplicated records."""

    def __init__(self) -> None:
        self._records: dict[str, FindingRecord] = {}
        self._raw_count: int = 0

    @property
    def raw_count(self) -> int:
        """Total number of raw findings seen (before dedup)."""
        return self._raw_count

    @property
    def dedup_count(self) -> int:
        """Number of deduplicated finding groups."""
        return len(self._records)

    @property
    def duplicate_total(self) -> int:
        """Sum of duplicate counts across all records."""
        return sum(r.duplicate_count for r in self._records.values())

    @property
    def records(self) -> list[FindingRecord]:
        return sorted(self._records.values(), key=lambda r: r.signature.key())

    def add(
        self,
        *,
        function: str,
        kind: str,
        reproducer_path: Path | None = None,
        # semantic fields
        semantic_target_id: str | None = None,
        hazard_kind: str | None = None,
        source_location: str | None = None,
        # runtime fields
        exit_kind: str | None = None,
        stack_hash: str | None = None,
        target_kind: str | None = None,
        cycle_count: int | None = None,
        # extra
        semantic_evidence: dict[str, Any] | None = None,
        replay_status: str = "unchecked",
        replay_returncode: int | None = None,
        replay_stderr_preview: str = "",
    ) -> FindingRecord:
        """Register a raw finding and return the (possibly existing) group record."""
        self._raw_count += 1
        now = _iso_now()

        seed_hash = _seed_content_hash(reproducer_path)
        sig = build_signature(
            function=function,
            kind=kind,
            semantic_target_id=semantic_target_id,
            hazard_kind=hazard_kind,
            source_location=source_location,
            exit_kind=exit_kind,
            stack_hash=stack_hash,
            target_kind=target_kind,
            cycle_count=cycle_count,
            seed_content_hash=seed_hash,
        )

        key = sig.key()
        reproducer_display = str(reproducer_path) if reproducer_path else ""

        if key in self._records:
            record = self._records[key]
            record.duplicate_count += 1
            record.last_seen = now
            if reproducer_display and len(record.representative_seeds) < MAX_REPRESENTATIVE_SEEDS:
                if reproducer_display not in record.representative_seeds:
                    record.representative_seeds.append(reproducer_display)
            # Keep the "best" replay status: confirmed > unstable > candidate > unchecked
            if _replay_rank(replay_status) > _replay_rank(record.replay_status):
                record.replay_status = replay_status
                record.replay_returncode = replay_returncode
                record.replay_stderr_preview = replay_stderr_preview
            return record

        record = FindingRecord(
            signature=sig,
            error_kind=kind,
            function=function,
            representative_seeds=[reproducer_display] if reproducer_display else [],
            duplicate_count=0,
            first_seen=now,
            last_seen=now,
            replay_status=replay_status,
            replay_returncode=replay_returncode,
            replay_stderr_preview=replay_stderr_preview,
            semantic_evidence=semantic_evidence or {},
            source_location=source_location,
        )
        self._records[key] = record
        return record

    def add_from_finding_json(self, raw: dict[str, Any], json_path: Path, reproducer_path: Path | None = None) -> FindingRecord | None:
        """Register a finding from a legacy structured finding JSON."""
        function = raw.get("function", "unknown")
        kind = str(raw.get("kind") or raw.get("finding_kind") or "unknown")
        semantic_target_id = raw.get("semantic_target_id") or raw.get("guide_target_id")
        hazard_kind = raw.get("hazard_kind")
        source_location = raw.get("source_location") or raw.get("location")
        exit_kind = raw.get("exit_kind") or kind
        stack_hash = raw.get("stack_hash")
        target_kind = raw.get("target_kind")
        cycle_count = raw.get("cycle_count")
        semantic_evidence = raw.get("semantic_evidence", {})

        # Try to extract replay info if embedded
        replay_data = raw.get("replay", {})
        replay_status = "unchecked"
        replay_returncode = None
        replay_stderr_preview = ""
        if isinstance(replay_data, dict):
            replay_status = _replay_status_from_replay_result(replay_data)
            replay_returncode = replay_data.get("returncode")
            replay_stderr_preview = replay_data.get("stderr_preview", "")

        return self.add(
            function=function,
            kind=kind,
            reproducer_path=reproducer_path,
            semantic_target_id=semantic_target_id,
            hazard_kind=hazard_kind,
            source_location=source_location,
            exit_kind=exit_kind,
            stack_hash=stack_hash,
            target_kind=target_kind,
            cycle_count=cycle_count,
            semantic_evidence=semantic_evidence,
            replay_status=replay_status,
            replay_returncode=replay_returncode,
            replay_stderr_preview=replay_stderr_preview,
        )

    def load_legacy_records(self, records_dir: Path) -> int:
        """Load previously persisted FindingRecord JSON files for idempotent runs."""
        loaded = 0
        if not records_dir.is_dir():
            return loaded
        for path in sorted(records_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("schema_version") != "finding-record-1.0":
                continue
            try:
                record = FindingRecord.from_json(data)
            except (KeyError, TypeError, ValueError):
                continue
            key = record.signature.key()
            if key not in self._records:
                self._records[key] = record
                self._raw_count += record.duplicate_count + 1
                loaded += 1
        return loaded

    def persist(self, output_dir: Path) -> list[Path]:
        """Write deduplicated records as individual JSON files."""
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for record in self._records.values():
            safe_key = record.signature.key().replace("::", "__").replace("/", "_")[:160]
            path = output_dir / f"{safe_key}.json"
            path.write_text(
                json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append(path)
        return written

    def summary(self) -> dict[str, Any]:
        """Return a summary dict suitable for report/benchmark aggregation."""
        confirmed = sum(
            1 for r in self._records.values() if r.replay_status == "confirmed"
        )
        unstable = sum(
            1 for r in self._records.values() if r.replay_status == "unstable"
        )
        candidate = sum(
            1 for r in self._records.values() if r.replay_status == "candidate"
        )
        unchecked = sum(
            1 for r in self._records.values() if r.replay_status == "unchecked"
        )
        runtime = sum(
            1 for r in self._records.values() if r.signature.tier != "semantic"
        )
        semantic = sum(
            1 for r in self._records.values() if r.signature.tier == "semantic"
        )
        return {
            "raw_count": self.raw_count,
            "deduplicated_count": self.dedup_count,
            "confirmed_count": confirmed,
            "unstable_count": unstable,
            "candidate_count": candidate,
            "unchecked_count": unchecked,
            "duplicate_count": self.duplicate_total,
            "runtime_finding_count": runtime,
            "semantic_finding_count": semantic,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPLAY_RANK = {"confirmed": 3, "unstable": 2, "candidate": 1, "unchecked": 0}


def _replay_rank(status: str) -> int:
    return _REPLAY_RANK.get(status, 0)


def _replay_status_from_replay_result(replay: dict[str, Any]) -> str:
    """Map a replay result dict to a replay status label."""
    status = replay.get("status", "")
    if status == "ok":
        return "unstable"
    if status in ("crash", "timeout", "nonzero", "oom"):
        return "confirmed"
    if status == "replay_error":
        return "unstable"
    return "unchecked"
