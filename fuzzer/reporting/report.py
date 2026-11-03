"""Vulnerability report generation for ST fuzzer run directories.

Findings are deduplicated, their representatives replayed and optionally
minimized; the report records that evidence as JSON with a Markdown summary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fuzzer.reporting.origin import classify as classify_origin
from fuzzer.reporting.findings import FindingDeduplicator
from fuzzer.reporting.minimize import FINDING_STATUSES, minimize_seed
from fuzzer.runtime.paths import ROOT


DEFAULT_REPLAY_LIMIT = int(os.environ.get("SEMANTIST_REPORT_REPLAY_LIMIT", "200"))
DEFAULT_REPLAY_TIMEOUT = float(os.environ.get("SEMANTIST_REPORT_REPLAY_TIMEOUT", "2.0"))
DEFAULT_REPORT_FINDING_LIMIT = int(os.environ.get("SEMANTIST_REPORT_FINDING_LIMIT", "20"))
DEFAULT_REPORT_PREVIEW_LIMIT = int(os.environ.get("SEMANTIST_REPORT_PREVIEW_LIMIT", "1200"))


@dataclass
class EvidenceItem:
    source: str
    kind: str
    severity: str
    confidence: str
    title: str
    path: str
    detail: str
    reproducer: str | None = None


@dataclass
class ReplayResult:
    path: str
    size: int
    status: str
    returncode: int | None
    stderr_preview: str


@dataclass
class Report:
    schema_version: str
    generated_at: str
    run_dir: str
    function: str | None
    st_file: str | None
    summary: dict[str, Any]
    evidence: list[EvidenceItem] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)
    events: dict[str, Any] = field(default_factory=dict)
    corpus: dict[str, Any] = field(default_factory=dict)


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def count_files(path: Path, *, include_metadata: bool = True) -> int:
    if not path.exists():
        return 0
    return sum(
        1
        for item in path.iterdir()
        if item.is_file() and (include_metadata or not item.name.endswith(".metadata"))
    )


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            events.append(raw)
    return events


def read_json_or_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    if text.lstrip().startswith("["):
        raw = read_json(path, [])
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    return iter_jsonl(path)


def read_text_preview(path: Path | None, limit: int = 4000) -> str | None:
    if not path or not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def infer_seed_cycle_metadata(seed_text: str | None) -> dict[str, Any]:
    cycle_ids: set[int] = set()
    for line in (seed_text or "").splitlines():
        if not line.strip():
            continue
        name = line.split(",", 1)[0].strip()
        prefix, dot, rest = name.partition(".")
        if dot and prefix.isdigit() and rest:
            cycle_ids.add(int(prefix))
    ordered = sorted(cycle_ids)
    return {
        "target_kind": "FUNCTION_BLOCK" if ordered else "FUNCTION",
        "cycle_count": len(ordered),
        "cycle_ids": ordered,
    }


def target_inputs(target_dir: Path) -> list[Path]:
    if not target_dir.exists():
        return []
    return sorted(
        item
        for item in target_dir.iterdir()
        if item.is_file() and not item.name.startswith(".") and not item.name.endswith(".metadata")
    )


def replay_one(target: Path, testcase: Path, timeout_seconds: float) -> ReplayResult:
    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "abort_on_error=1:symbolize=1:detect_leaks=0:print_stacktrace=1"
    env["UBSAN_OPTIONS"] = "abort_on_error=1:symbolize=1:print_stacktrace=1"
    try:
        result = subprocess.run(
            [str(target), str(testcase)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode == 0:
            status = "ok"
        elif result.returncode < 0:
            status = "crash"
        else:
            status = "nonzero"
        return ReplayResult(
            path=display_path(testcase),
            size=testcase.stat().st_size,
            status=status,
            returncode=result.returncode,
            stderr_preview=result.stderr.decode("utf-8", errors="replace")[:4000],
        )
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else ""
        return ReplayResult(
            path=display_path(testcase),
            size=testcase.stat().st_size,
            status="timeout",
            returncode=None,
            stderr_preview=stderr[:600],
        )
    except OSError as error:
        return ReplayResult(
            path=display_path(testcase),
            size=testcase.stat().st_size if testcase.exists() else 0,
            status="replay_error",
            returncode=None,
            stderr_preview=str(error),
        )


def collect_replay_evidence(results: list[ReplayResult]) -> list[EvidenceItem]:
    evidence = []
    for result in results:
        if result.status not in {"crash", "timeout", "nonzero"}:
            continue
        severity = "High" if result.status == "crash" else "Medium"
        evidence.append(
            EvidenceItem(
                source="target-corpus-replay",
                kind=result.status,
                severity=severity,
                confidence="confirmed",
                title=f"Target replay produced {result.status}",
                path=result.path,
                detail=result.stderr_preview or f"returncode={result.returncode}",
                reproducer=result.path,
            )
        )
    return evidence


def collect_finding_files(findings_dir: Path) -> list[dict[str, Any]]:
    findings = []
    if not findings_dir.exists():
        return findings
    dedup_dir = findings_dir / "deduplicated"
    candidate_dir = findings_dir / "candidates"
    v3_packages = _v3_package_dirs(findings_dir)
    for item in sorted(findings_dir.rglob("*")):
        if not item.is_file():
            continue
        if candidate_dir in item.parents:
            continue
        if any(package == item or package in item.parents for package in v3_packages):
            continue
        # Skip files inside the deduplicated/ subdirectory
        try:
            if dedup_dir in item.parents:
                continue
        except (ValueError, OSError):
            pass
        if item.suffix == ".seed" and item.with_suffix(".json").exists():
            continue
        if item.suffix == ".json":
            raw = read_json(item, {})
            if isinstance(raw, dict) and (
                "minimized_seed" in raw or "critical_fields" in raw or "original_status" in raw
            ):
                continue
            if _is_structured_finding(raw):
                continue
        findings.append(
            {
                "path": display_path(item),
                "size": item.stat().st_size,
                "preview": item.read_bytes()[:600].decode("utf-8", errors="replace"),
            }
        )
    return findings


def _is_v3_confirmed_finding(raw: Any) -> bool:
    """Recognize the campaign's confirmed finding package schema.

    v3 packages intentionally carry campaign metadata rather than the older
    runtime finding fields.  The confirmation marker keeps candidate JSON
    files and unrelated bundle sidecars out of this path.
    """
    if not isinstance(raw, dict):
        return False
    return (
        isinstance(raw.get("finding_id"), str)
        and bool(raw.get("finding_id"))
        and isinstance(raw.get("function"), str)
        and bool(raw.get("function"))
        and isinstance(raw.get("oracle"), str)
        and bool(raw.get("oracle"))
        and raw.get("confirmation") in {"repeatable", "attributed"}
        and isinstance(raw.get("goal"), dict)
    )


def _is_structured_finding(raw: Any) -> bool:
    """Return whether a JSON document is a finding record.

    Bundle and STG sidecars also carry ``schema_version``.  Requiring the
    finding fields avoids counting those documents as unknown findings.
    """
    if _is_v3_confirmed_finding(raw):
        return True
    return isinstance(raw, dict) and {
        "source", "kind", "reproducer", "content_hash"
    }.issubset(raw.keys())


def _v3_package_dirs(findings_dir: Path) -> set[Path]:
    packages: set[Path] = set()
    for path in findings_dir.rglob("finding.json"):
        raw = read_json(path, {})
        if _is_v3_confirmed_finding(raw):
            packages.add(path.parent)
    return packages


def _v3_reproducer(json_path: Path) -> Path | None:
    """Select the minimized v3 input, falling back to the original testcase."""
    for name in ("minimized.txt", "testcase.txt"):
        candidate = json_path.parent / name
        if candidate.is_file():
            return candidate
    return None


def _v3_source_hint(source: Any) -> str | None:
    if isinstance(source, str) and source:
        return source
    if not isinstance(source, dict):
        return None
    file_name = source.get("file") or source.get("path")
    line = source.get("line") or source.get("start_line")
    if file_name and line:
        return f"{file_name}:{line}"
    if file_name:
        return str(file_name)
    return None


def _normalize_v3_finding(raw: dict[str, Any], json_path: Path) -> dict[str, Any]:
    """Adapt a v3 confirmed package to the reporter's finding schema."""
    normalized = dict(raw)
    goal = raw.get("goal") if isinstance(raw.get("goal"), dict) else {}
    oracle = str(raw.get("oracle") or "unknown")
    semantic_oracles = {
        "array_access", "null_dereference", "division_by_zero", "conversion_out_of_range",
    }
    if oracle == "crash":
        kind = "crash"
    elif oracle == "scan_budget":
        kind = "timeout"
    elif oracle in semantic_oracles:
        kind = "semantic_violation"
    else:
        kind = "semantic_violation"
    source_location = _v3_source_hint(goal.get("source"))
    runtime_id = goal.get("runtime_id")
    stable_id = goal.get("stable_id")
    semantic_target_id = stable_id or (f"runtime:{runtime_id}" if runtime_id is not None else None)
    reproducer = _v3_reproducer(json_path)
    evidence = {
        "v3_confirmation": raw.get("confirmation"),
        "goal": goal,
        "cause_evidence": raw.get("cause_evidence"),
        "history_evidence": raw.get("history_evidence"),
        "provenance": raw.get("provenance"),
    }
    normalized.update(
        {
            "schema_version": "semantist.v3-finding/1.0",
            "source": "v3-campaign",
            "kind": kind,
            "finding_kind": kind,
            "hazard_kind": oracle if kind == "semantic_violation" else None,
            "semantic_target_id": semantic_target_id,
            "source_location": source_location,
            "reproducer": str(reproducer) if reproducer else None,
            "semantic_evidence": evidence,
        }
    )
    return normalized


def resolve_run_path(run_dir: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    direct = ROOT / path
    if direct.exists():
        return direct
    return run_dir / path


def minimization_metadata_candidates(json_path: Path, reproducer: Path | None) -> list[Path]:
    candidates = [
        json_path.with_name(f"{json_path.stem}.minimized.json"),
        json_path.with_name(f"{json_path.stem}.min.json"),
    ]
    if reproducer is not None:
        candidates.extend(
            [
                reproducer.with_name(f"{reproducer.stem}.minimized.json"),
                reproducer.with_name(f"{reproducer.stem}.min.json"),
            ]
        )
    return candidates


def minimization_seed_candidates(json_path: Path, reproducer: Path | None) -> list[Path]:
    candidates = [
        json_path.with_name(f"{json_path.stem}.minimized.seed"),
        json_path.with_name(f"{json_path.stem}.min.seed"),
    ]
    if reproducer is not None:
        candidates.extend(
            [
                reproducer.with_name(f"{reproducer.stem}.minimized.seed"),
                reproducer.with_name(f"{reproducer.stem}.min.seed"),
            ]
        )
    return candidates


def load_minimization_metadata(
    raw: dict[str, Any],
    json_path: Path,
    reproducer: Path | None,
) -> dict[str, Any] | None:
    embedded = raw.get("minimization") or raw.get("minimization_metadata")
    if isinstance(embedded, dict):
        return embedded
    for path in minimization_metadata_candidates(json_path, reproducer):
        metadata = read_json(path, None)
        if isinstance(metadata, dict) and (
            "minimized_seed" in metadata or "critical_fields" in metadata
        ):
            metadata.setdefault("metadata_path", display_path(path))
            return metadata
    for seed_path in minimization_seed_candidates(json_path, reproducer):
        if seed_path.exists():
            return {"minimized_seed": display_path(seed_path)}
    minimized_seed = raw.get("minimized_seed")
    if isinstance(minimized_seed, str) and minimized_seed:
        return {"minimized_seed": minimized_seed}
    return None


def minimization_preview(run_dir: Path, metadata: dict[str, Any]) -> str | None:
    seed_path = resolve_run_path(run_dir, str(metadata.get("minimized_seed") or ""))
    return read_text_preview(seed_path, limit=4000)


def maybe_minimize_finding(
    run_dir: Path,
    raw: dict[str, Any],
    json_path: Path,
    reproducer: Path | None,
    target: Path,
    timeout: float,
) -> dict[str, Any] | None:
    if not target.exists() or not reproducer or not reproducer.exists():
        return None
    output = json_path.with_name(f"{json_path.stem}.minimized.seed")
    metadata_path = json_path.with_name(f"{json_path.stem}.minimized.json")
    if metadata_path.exists():
        metadata = read_json(metadata_path, None)
        return metadata if isinstance(metadata, dict) else None
    kind = str(raw.get("kind") or "")
    baseline_status = kind if kind in FINDING_STATUSES else None
    try:
        result = minimize_seed(
            target=target,
            seed=reproducer,
            output=output,
            timeout=timeout,
            baseline_status=baseline_status,
            metadata=metadata_path,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        error_metadata = {
            "error": str(error),
            "original_seed": display_path(reproducer),
            "metadata_path": display_path(metadata_path),
        }
        metadata_path.write_text(
            json.dumps(error_metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return error_metadata
    metadata = result.to_json()
    metadata["metadata_path"] = display_path(metadata_path)
    return metadata


def collect_finding_records(
    run_dir: Path,
    *,
    target: Path,
    replay_timeout: float,
    minimize_findings: bool = False,
    function: str | None = None,
) -> tuple[list[EvidenceItem], list[dict[str, Any]], dict[str, Any]]:
    """Collect raw finding JSONs, deduplicate, replay representatives, return evidence.

    Returns (evidence, raw_records, dedup_summary).
    """
    findings_dir = run_dir / "findings"
    func_name = function or "unknown"
    evidence: list[EvidenceItem] = []
    records: list[dict[str, Any]] = []
    dedup = FindingDeduplicator()

    dedup_records_dir = findings_dir / "deduplicated"

    if not findings_dir.exists():
        return evidence, records, dedup.summary()

    # Pass 1: collect raw structured findings, feed into deduplicator
    for json_path in sorted(findings_dir.rglob("*.json")):
        # Skip files inside deduplicated/ subdirectory
        try:
            if dedup_records_dir in json_path.parents:
                continue
        except (ValueError, OSError):
            pass

        raw = read_json(json_path, {})
        if not isinstance(raw, dict):
            continue
        is_v3 = _is_v3_confirmed_finding(raw)
        if not _is_structured_finding(raw):
            continue
        if is_v3:
            raw = _normalize_v3_finding(raw, json_path)
        raw.setdefault("schema_version", "legacy")
        raw["path"] = display_path(json_path)
        reproducer = resolve_run_path(run_dir, raw.get("reproducer"))
        raw["structured_input"] = read_text_preview(
            reproducer, limit=DEFAULT_REPORT_PREVIEW_LIMIT
        )
        raw_cycle_metadata = infer_seed_cycle_metadata(raw["structured_input"])
        raw.setdefault("target_kind", raw_cycle_metadata["target_kind"])
        raw.setdefault("cycle_count", raw_cycle_metadata["cycle_count"])
        raw.setdefault("cycle_ids", raw_cycle_metadata["cycle_ids"])
        minimization = load_minimization_metadata(raw, json_path, reproducer)
        if minimization is None and minimize_findings:
            minimization = maybe_minimize_finding(
                run_dir,
                raw,
                json_path,
                reproducer,
                target,
                replay_timeout,
            )
        if isinstance(minimization, dict):
            raw["minimization"] = minimization
            preview = minimization_preview(run_dir, minimization)
            if preview:
                raw["minimized_structured_input"] = preview[
                    :DEFAULT_REPORT_PREVIEW_LIMIT
                ]
                minimized_cycle_metadata = infer_seed_cycle_metadata(preview)
                minimization.setdefault(
                    "minimized_cycle_count", minimized_cycle_metadata["cycle_count"]
                )
                minimization.setdefault(
                    "minimized_cycle_ids", minimized_cycle_metadata["cycle_ids"]
                )
        records.append(raw)

        # Extract signature fields from the raw finding JSON
        raw_function = raw.get("function") or func_name
        kind = str(raw.get("kind") or raw.get("finding_kind") or "unknown")
        dedup.add(
            function=raw_function,
            kind=kind,
            reproducer_path=reproducer,
            semantic_target_id=raw.get("semantic_target_id") or raw.get("guide_target_id"),
            hazard_kind=raw.get("hazard_kind"),
            source_location=raw.get("source_location") or raw.get("location"),
            exit_kind=raw.get("exit_kind") or kind,
            stack_hash=raw.get("stack_hash"),
            target_kind=raw.get("target_kind"),
            cycle_count=raw.get("cycle_count"),
            semantic_evidence=raw.get("semantic_evidence", {}),
            replay_status="unchecked",
        )

    if not records:
        dedup.load_legacy_records(dedup_records_dir)

    # Pass 2: replay representative seeds for each deduplicated finding that hasn't been replayed yet
    for record in dedup.records:
        if record.replay_status != "unchecked":
            continue
        campaign_confirmed = record.semantic_evidence.get("v3_confirmation") in {
            "repeatable", "attributed"
        }
        if not record.representative_seeds or not target.exists():
            if campaign_confirmed:
                record.replay_status = "confirmed"
            continue
        rep_seed_path = resolve_run_path(run_dir, record.representative_seeds[0])
        if rep_seed_path and rep_seed_path.exists():
            replay_result = replay_one(target, rep_seed_path, replay_timeout)
            record.replay_status = _replay_status(replay_result)
            # v3 semantic confirmation already performed three independent
            # native replays.  A zero exit from this generic reporter replay
            # does not disprove a semantic hit because replay_one only sees
            # process status, not semantic runtime telemetry.
            if campaign_confirmed and record.replay_status in {"unstable", "unchecked"}:
                record.replay_status = "confirmed"
            record.replay_returncode = replay_result.returncode
            record.replay_stderr_preview = replay_result.stderr_preview
            if replay_result.status in {"crash", "nonzero", "timeout"}:
                record.crash_origin = classify_origin(replay_result.stderr_preview)

    # Persist deduplicated records
    dedup.persist(dedup_records_dir)

    # Pass 3: build evidence from deduplicated runtime finding records.
    for record in dedup.records:
        evidence.append(_dedup_record_to_evidence(record, run_dir))

    return evidence[:DEFAULT_REPORT_FINDING_LIMIT], records, dedup.summary()


def _replay_status(replay_result: ReplayResult) -> str:
    """Map ReplayResult to a replay status label."""
    if replay_result.status == "ok":
        return "unstable"
    if replay_result.status in ("crash", "timeout", "nonzero", "oom"):
        return "confirmed"
    if replay_result.status == "replay_error":
        return "unstable"
    return "unchecked"


def _dedup_record_to_evidence(record: Any, run_dir: Path) -> EvidenceItem:
    """Convert a FindingRecord into an EvidenceItem for the report."""
    from fuzzer.reporting.findings import FindingRecord
    if not isinstance(record, FindingRecord):
        return EvidenceItem(
            source="semantist",
            kind="unknown",
            severity="Medium",
            confidence="unverified",
            title="Unknown finding record",
            path=str(run_dir),
            detail="",
        )
    sig = record.signature
    severity = "High" if record.error_kind in ("crash", "semantic_violation") else "Medium"
    confidence = record.replay_status if record.replay_status != "unchecked" else "candidate"

    # Build detail text
    parts = [f"Signature: {sig.display()}"]
    if record.source_location:
        parts.append(f"Source location: {record.source_location}")
    parts.append(f"Duplicates: {record.duplicate_count}")
    parts.append(f"First seen: {record.first_seen}, Last seen: {record.last_seen}")
    parts.append(f"Replay status: {record.replay_status}")
    if record.crash_origin:
        parts.append(f"Crash origin: {record.crash_origin.get('origin')} ({record.crash_origin.get('evidence', '')})")
    if record.replay_stderr_preview:
        parts.append("Replay stderr/stack:\n" + record.replay_stderr_preview[:2000])
    if record.semantic_evidence:
        parts.append("Semantic evidence: " + json.dumps(record.semantic_evidence))

    reproducer = record.representative_seeds[0] if record.representative_seeds else None

    return EvidenceItem(
        source="deduplicated-finding",
        kind=record.error_kind,
        severity=severity,
        confidence=confidence,
        title=f"{record.error_kind} in {record.function}: {sig.display()}",
        path=str(run_dir / "findings" / "deduplicated"),
        detail="\n\n".join(part for part in parts if part).strip(),
        reproducer=reproducer,
    )


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(event.get("event", "unknown")) for event in events)
    return {"counts": dict(sorted(counts.items()))}


def summarize_semantic_targets(run_dir: Path) -> dict[str, Any]:
    state = read_json(run_dir / "semantic-task-state.json", {})
    attempts = state.get("attempt_history", [])
    if not isinstance(attempts, list):
        attempts = []
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        target_id = attempt.get("target_id") or attempt.get("guide_target_id")
        if isinstance(target_id, str) and target_id:
            by_target[target_id].append(attempt)
    return {
        "attempt_count": len(attempts),
        "attempted_targets": len(by_target),
        "attempts": attempts[-50:],
        "target_chain": [
            {
                "target_id": target_id,
                "kind": None,
                "side": None,
                "attempts": len(target_attempts),
                "coverage_confirmation": any(
                    bool(attempt.get("covered")) for attempt in target_attempts
                ),
                "finding": False,
            }
            for target_id, target_attempts in sorted(by_target.items())
        ],
    }


def summarize_metadata(run_dir: Path) -> dict[str, Any]:
    raw_by_id = {
        str(item.get("seed_id")): item
        for item in read_json_or_jsonl_records(run_dir / "testcase-metadata.bootstrap.json")
        if isinstance(item, dict) and item.get("seed_id")
    }
    raw = list(raw_by_id.values())
    sources = Counter(str(item.get("source", "unknown")) for item in raw if isinstance(item, dict))
    findings = [
        item
        for item in raw
        if isinstance(item, dict) and item.get("is_finding")
    ]

    def metadata_seed_path(item: dict[str, Any]) -> str:
        artifact = item.get("finding_artifact")
        if isinstance(artifact, str) and artifact:
            path = Path(artifact)
            if path.exists():
                return display_path(path)
        seed_id = item.get("seed_id")
        return display_path(run_dir / "unified-corpus" / "seeds" / f"{seed_id}.seed") if seed_id else ""

    return {
        "records": len(raw),
        "sources": dict(sorted(sources.items())),
        "target_assigned": sum(1 for item in raw if isinstance(item, dict) and item.get("target_id")),
        "target_covered": sum(1 for item in raw if isinstance(item, dict) and item.get("covered_target")),
        "confirmed_finding_seeds": len(findings),
        "finding_seeds": [
            {
                "content_hash": item.get("content_hash"),
                "seed_id": item.get("seed_id"),
                "source": item.get("source"),
                "finding_source": item.get("finding_source"),
                "finding_kind": item.get("finding_kind"),
                "severity": item.get("finding_severity"),
                "confidence": item.get("finding_confidence"),
                "detail": item.get("finding_detail"),
                "artifact": item.get("finding_artifact"),
                "seed_path": metadata_seed_path(item),
            }
            for item in findings[:20]
        ],
    }


def summarize_corpus(run_dir: Path) -> dict[str, Any]:
    target = run_dir / "target-corpus"
    target_case_count = len(target_inputs(target))
    return {
        "unified_seeds": count_files(run_dir / "unified-corpus" / "seeds"),
        "runtime_queue": count_files(run_dir / "unified-corpus" / "runtime"),
        "target_corpus_inputs": target_case_count,
        "target_corpus_metadata": count_files(target, include_metadata=True)
        - target_case_count,
        "findings_files": len(collect_finding_files(run_dir / "findings")),
        "metadata": summarize_metadata(run_dir),
        "semantic_targets": summarize_semantic_targets(run_dir),
    }


def evidence_summary(evidence: list[EvidenceItem]) -> dict[str, Any]:
    by_severity = Counter(item.severity for item in evidence)
    by_source = Counter(item.source for item in evidence)
    confirmed = [item for item in evidence if item.confidence == "confirmed"]
    if any(item.severity == "High" for item in confirmed):
        status = "confirmed_high_risk"
    elif confirmed:
        status = "confirmed_findings"
    else:
        status = "no_confirmed_vulnerability"
    return {
        "status": status,
        "confirmed_findings": len(confirmed),
        "by_severity": dict(sorted(by_severity.items())),
        "by_source": dict(sorted(by_source.items())),
    }


def replay_target_from_events(run_dir: Path, events: list[dict[str, Any]]) -> Path:
    for event in reversed(events):
        if event.get("event") != "build_targets":
            continue
        raw_target = event.get("target")
        if not isinstance(raw_target, str) or not raw_target:
            continue
        target = Path(raw_target)
        if not target.is_absolute():
            target = ROOT / target
        return target
    return run_dir / "build" / "fuzz_target"


def build_report(
    run_dir: Path,
    *,
    function: str | None = None,
    st_file: Path | None = None,
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    replay_timeout: float = DEFAULT_REPLAY_TIMEOUT,
    replay: bool = True,
    minimize_findings: bool = False,
) -> Report:
    run_dir = run_dir.resolve()
    events = iter_jsonl(run_dir / "events.jsonl")
    target = replay_target_from_events(run_dir, events)
    target_cases = target_inputs(run_dir / "target-corpus")
    replay_results: list[ReplayResult] = []
    if replay and target.exists():
        for testcase in target_cases[: max(0, replay_limit)]:
            replay_results.append(replay_one(target, testcase, replay_timeout))
    finding_evidence, finding_records, dedup_summary = collect_finding_records(
        run_dir,
        target=target,
        replay_timeout=replay_timeout,
        minimize_findings=minimize_findings,
        function=function,
    )
    legacy_replay_evidence = [] if finding_records else collect_replay_evidence(replay_results)
    evidence = (finding_evidence + legacy_replay_evidence)[:DEFAULT_REPORT_FINDING_LIMIT]
    findings = collect_finding_files(run_dir / "findings")
    for finding in findings:
        evidence.append(
            EvidenceItem(
                source="findings",
                kind="finding_file",
                severity="Medium",
                confidence="unverified",
                title="Finding file requires manual/replay triage",
                path=finding["path"],
                detail=finding["preview"],
                reproducer=finding["path"],
            )
        )
    evidence = evidence[:DEFAULT_REPORT_FINDING_LIMIT]
    summary = evidence_summary(evidence)
    # A v3 campaign writes execution provenance before the generic report
    # builder runs. Carry those fields forward so report regeneration does not
    # discard support status, approximate model counts, or query outcomes.
    campaign_report = read_json(run_dir / "report.json", {})
    if isinstance(campaign_report, dict):
        for key in (
            "candidates", "confirmed", "source_risk_confirmed_under_model",
            "status_counts",
            "probe_audit", "verification_status", "candidate_confirmation_results",
            "support_status", "execution_status", "semantic_fidelity", "stub_used",
            "model_scope", "model_result_counts", "confirmed_model_counts",
            "symbolic_queries", "native_replay_results",
        ):
            if key in campaign_report:
                summary[key] = campaign_report[key]
        # A campaign with unresolved symbolic work is not a clean negative
        # result.  Keep the detailed per-query status counts while exposing a
        # top-level inconclusive status for consumers that only read summary.
        symbolic_queries = campaign_report.get("symbolic_queries")
        native_replay_results = campaign_report.get("native_replay_results")
        execution_status = campaign_report.get("execution_status")
        symbolic_phase = (str(execution_status.get("symbolic", "")).upper()
                          if isinstance(execution_status, dict) else "")
        unresolved = {"UNKNOWN", "UNSUPPORTED", "TIMEOUT", "FAILED", "ERROR", "REPLAY_MISMATCH"}
        if (summary.get("status") == "no_confirmed_vulnerability"
                and campaign_report.get("verification_status") == "incomplete"):
            summary["status"] = "inconclusive"
        if (summary.get("status") in {"confirmed_high_risk", "confirmed_findings"}
                and campaign_report.get("confirmed", 0) > 0
                and campaign_report.get("source_risk_confirmed_under_model") == 0):
            summary["status"] = ("inconclusive" if campaign_report.get("verification_status") == "incomplete"
                                 else "confirmed_runtime_under_model")
        if (summary.get("status") == "no_confirmed_vulnerability"
                and (symbolic_phase in unresolved
                     or (isinstance(symbolic_queries, list)
                         and any(str(item.get("status", "")).upper() in unresolved
                                 for item in symbolic_queries if isinstance(item, dict)))
                     or (isinstance(native_replay_results, list)
                         and any(str(item.get("status", "")).upper() in unresolved
                                 for item in native_replay_results if isinstance(item, dict))))):
            summary["status"] = "inconclusive"
    campaign_failure = next((event for event in reversed(events)
                             if event.get("event") in {
                                 "v3_campaign_failed", "campaign_failed", "build_failed"
                             }), None)
    if campaign_failure is not None:
        # A failed engine/build must remain visible even when no finding JSON
        # was produced. Do not turn an interrupted campaign into a clean
        # "no vulnerability" result.
        summary["status"] = "build_failed" if campaign_failure.get("event") == "build_failed" else "campaign_failed"
        execution_status = summary.get("execution_status")
        if isinstance(execution_status, dict):
            execution_status = dict(execution_status)
            execution_status["campaign"] = "FAILED"
        else:
            execution_status = {"campaign": "FAILED"}
        summary["execution_status"] = execution_status
        summary["failure"] = {"error": campaign_failure.get("error"),
                               "event": campaign_failure.get("event")}
    summary.update(
        {
            "raw_objective_count": len(target_cases) + len(finding_records),
            "raw_finding_record_count": len(finding_records),
            "deduplicated_finding_count": dedup_summary.get("deduplicated_count", 0),
            "confirmed_finding_count": dedup_summary.get("confirmed_count", 0),
            "unstable_finding_count": dedup_summary.get("unstable_count", 0),
            "candidate_finding_count": dedup_summary.get("candidate_count", 0),
            "duplicate_finding_count": dedup_summary.get("duplicate_count", 0),
            "runtime_finding_count": dedup_summary.get("runtime_finding_count", 0),
            "semantic_finding_count": dedup_summary.get("semantic_finding_count", 0),
        }
    )
    report = Report(
        schema_version="1.0",
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        run_dir=display_path(run_dir),
        function=function,
        st_file=display_path(st_file) if st_file else None,
        summary=summary,
        evidence=evidence,
        replay={
            "enabled": replay,
            "target": display_path(target) if target.exists() else None,
            "target_corpus_total": len(target_cases),
            "target_corpus_replayed": len(replay_results),
            "limit": replay_limit,
            "timeout_seconds": replay_timeout,
            "status_counts": dict(Counter(result.status for result in replay_results)),
            "results": [asdict(result) for result in replay_results[:50]],
        },
        events=summarize_events(events),
        corpus={
            **summarize_corpus(run_dir),
            "finding_records": finding_records[:DEFAULT_REPORT_FINDING_LIMIT],
            "finding_record_total": len(finding_records),
            "finding_record_limit": DEFAULT_REPORT_FINDING_LIMIT,
            "dedup_summary": dedup_summary,
        },
    )
    return report


def write_json(report: Report, out: Path) -> None:
    out.write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_markdown(report: Report, out: Path) -> None:
    lines = [
        f"# Vulnerability Report: {report.function or 'unknown'}",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Run directory: `{report.run_dir}`",
        f"- Status: `{report.summary['status']}`",
        f"- Deduplicated findings: `{report.summary.get('deduplicated_finding_count', 0)}`",
        f"- Confirmed findings: `{report.summary.get('confirmed_finding_count', report.summary['confirmed_findings'])}`",
        f"- Unstable/candidate: `{report.summary.get('unstable_finding_count', 0)}` / `{report.summary.get('candidate_finding_count', 0)}`",
        f"- Duplicates: `{report.summary.get('duplicate_finding_count', 0)}`",
        f"- Raw finding records: `{report.summary.get('raw_finding_record_count', 0)}`",
        f"- Runtime findings: `{report.summary.get('runtime_finding_count', 0)}`",
        f"- Semantic findings: `{report.summary.get('semantic_finding_count', 0)}`",
        "",
    ]
    if not report.evidence:
        lines.append("No confirmed vulnerability evidence was collected.")
    semantic_targets = report.corpus.get("semantic_targets", {})
    target_chain = semantic_targets.get("target_chain", [])
    lines.extend(["## Semantic Target Attempts", ""])
    if not target_chain:
        lines.append("No semantic target attempts were recorded.")
        lines.append("")
    for item in target_chain[:10]:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"- `{item.get('target_id')}`: attempts `{item.get('attempts')}`, "
                f"coverage `{item.get('coverage_confirmation')}`, finding `{item.get('finding')}`",
            ]
        )
    lines.extend(["", "## Evidence", ""])
    for index, item in enumerate(report.evidence, start=1):
        lines.extend(
            [
                f"### {index}. {item.title}",
                "",
                f"- Source: `{item.source}`",
                f"- Kind: `{item.kind}`",
                f"- Severity: `{item.severity}`",
                f"- Confidence: `{item.confidence}`",
                f"- Path: `{item.path}`",
                f"- Reproducer: `{item.reproducer or 'n/a'}`",
                "",
                "```text",
                item.detail.strip()[:1600],
                "```",
                "",
            ]
        )
    minimized = [
        record
        for record in report.corpus.get("finding_records", [])
        if isinstance(record, dict) and isinstance(record.get("minimization"), dict)
    ]
    if minimized:
        lines.extend(["## Minimization", ""])
        for record in minimized:
            metadata = record["minimization"]
            lines.extend(
                [
                    f"### {record.get('content_hash', record.get('path', 'finding'))}",
                    "",
                    f"- Original status: `{metadata.get('original_status', 'n/a')}`",
                    f"- Minimized status: `{metadata.get('minimized_status', 'n/a')}`",
                    f"- Minimized seed: `{metadata.get('minimized_seed', 'n/a')}`",
                    f"- Removed records: `{len(metadata.get('removed_records', []))}`",
                    f"- Changed values: `{len(metadata.get('changed_values', []))}`",
                    f"- Minimized cycle count: `{metadata.get('minimized_cycle_count', 'n/a')}`",
                    f"- Critical fields: `{', '.join(metadata.get('critical_fields', [])) or 'n/a'}`",
                    f"- Maybe fields: `{', '.join(metadata.get('maybe_fields', [])) or 'n/a'}`",
                    f"- Signature match: `{metadata.get('signature_match', 'n/a')}`",
                    f"- Sanitizer: `{(metadata.get('minimized_signature') or {}).get('sanitizer', 'n/a')}`",
                    f"- Stack hash: `{(metadata.get('minimized_signature') or {}).get('stack_hash', 'n/a')}`",
                    "",
                ]
            )
            if record.get("minimized_structured_input"):
                lines.extend(
                    [
                        "```text",
                        str(record["minimized_structured_input"]).strip()[:1600],
                        "```",
                        "",
                    ]
                )
    lines.extend(
        [
            "## Corpus And Events",
            "",
            "```json",
            json.dumps({"corpus": report.corpus, "events": report.events, "replay": report.replay}, indent=2, sort_keys=True),
            "```",
        ]
    )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report_bundle(report: Report, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "report.json",
        "markdown": output_dir / "report.md",
    }
    write_json(report, paths["json"])
    write_markdown(report, paths["markdown"])
    return paths


def generate_report(
    run_dir: Path,
    *,
    function: str | None = None,
    st_file: Path | None = None,
    output_dir: Path | None = None,
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    replay_timeout: float = DEFAULT_REPLAY_TIMEOUT,
    replay: bool = True,
    minimize_findings: bool = False,
) -> tuple[Report, dict[str, Path]]:
    report = build_report(
        run_dir,
        function=function,
        st_file=st_file,
        replay_limit=replay_limit,
        replay_timeout=replay_timeout,
        replay=replay,
        minimize_findings=minimize_findings,
    )
    paths = write_report_bundle(report, output_dir or (run_dir / "vulnerability-report"))
    return report, paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the vulnerability report for a SemantiST run")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--function")
    parser.add_argument("--st-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--replay-limit", type=int, default=DEFAULT_REPLAY_LIMIT)
    parser.add_argument("--replay-timeout", type=float, default=DEFAULT_REPLAY_TIMEOUT)
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument(
        "--minimize-findings",
        action="store_true",
        help="run deterministic minimization for confirmed finding reproducers before writing the report",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report, paths = generate_report(
        args.run_dir,
        function=args.function,
        st_file=args.st_file,
        output_dir=args.output_dir,
        replay_limit=args.replay_limit,
        replay_timeout=args.replay_timeout,
        replay=not args.no_replay,
        minimize_findings=args.minimize_findings,
    )
    print(f"[report] status: {report.summary['status']}")
    print(f"[report] confirmed findings: {report.summary['confirmed_findings']}")
    for kind, path in paths.items():
        print(f"[report] wrote {kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
