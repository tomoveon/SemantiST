"""Vulnerability report generation for ST fuzzer run directories.

The report keeps evidence collection deterministic. Optional LLM text is only
used to summarize already-collected facts, not to decide whether an input is a
vulnerability.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fuzzer.reporting.llm import (
    DEFAULT_OPENAI_COMPATIBLE_API_KEY,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MODEL,
    DEFAULT_LLM_RETRY_DELAYS,
    RETRYABLE_HTTP_STATUS,
    extract_json_payload,
)
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
    source_context: dict[str, Any] = field(default_factory=dict)
    llm_analysis: dict[str, Any] = field(default_factory=dict)


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


def extract_function_source(st_file: Path | None, function: str | None) -> dict[str, Any]:
    if not st_file:
        return {}
    path = st_file.resolve()
    if not path.exists():
        return {"path": display_path(path), "error": "source file does not exist"}
    text = path.read_text(encoding="utf-8", errors="replace")
    if not function:
        return {"path": display_path(path), "excerpt": text[:4000]}
    pattern = re.compile(
        rf"(?is)\bFUNCTION\s+{re.escape(function)}\b.*?\bEND_FUNCTION\b"
    )
    match = pattern.search(text)
    if not match:
        pattern = re.compile(
            rf"(?is)\bFUNCTION_BLOCK\s+{re.escape(function)}\b.*?\bEND_FUNCTION_BLOCK\b"
        )
        match = pattern.search(text)
    excerpt = match.group(0) if match else text[:4000]
    line_start = text[: match.start()].count("\n") + 1 if match else 1
    return {
        "path": display_path(path),
        "function": function,
        "line_start": line_start,
        "excerpt": excerpt[:8000],
    }


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


def cycle_metadata_summary(raw: dict[str, Any]) -> str | None:
    if "cycle_count" not in raw and "cycle_ids" not in raw and "target_kind" not in raw:
        return None
    return (
        f"Cycle metadata: target_kind={raw.get('target_kind', 'n/a')}, "
        f"cycle_count={raw.get('cycle_count', 'n/a')}, "
        f"cycle_ids={raw.get('cycle_ids', [])}, "
        f"stale_cycles={raw.get('stale_cycles', [])}, "
        f"last_changed_cycle={raw.get('last_changed_cycle', 'n/a')}"
    )


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
            stderr_preview=result.stderr.decode("utf-8", errors="replace")[:600],
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
    for item in sorted(findings_dir.rglob("*")):
        if not item.is_file():
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
            if isinstance(raw, dict) and (
                raw.get("schema_version")
                or {"source", "kind", "reproducer", "content_hash"}.issubset(raw.keys())
            ):
                continue
        findings.append(
            {
                "path": display_path(item),
                "size": item.stat().st_size,
                "preview": item.read_bytes()[:600].decode("utf-8", errors="replace"),
            }
        )
    return findings


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
        is_structured = raw.get("schema_version") or {"source", "kind", "reproducer", "content_hash"}.issubset(raw.keys())
        if not is_structured:
            continue
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
        if not record.representative_seeds or not target.exists():
            continue
        rep_seed_path = resolve_run_path(run_dir, record.representative_seeds[0])
        if rep_seed_path and rep_seed_path.exists():
            replay_result = replay_one(target, rep_seed_path, replay_timeout)
            record.replay_status = _replay_status(replay_result)
            record.replay_returncode = replay_result.returncode
            record.replay_stderr_preview = replay_result.stderr_preview

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
    raw_by_hash = {
        str(item.get("content_hash")): item
        for item in read_json_or_jsonl_records(run_dir / "testcase-metadata.bootstrap.json")
        if isinstance(item, dict) and item.get("content_hash")
    }
    raw = list(raw_by_hash.values())
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
        return display_path(run_dir / "unified-corpus" / "seeds" / f"{item.get('content_hash')}.seed")

    return {
        "records": len(raw),
        "sources": dict(sorted(sources.items())),
        "target_assigned": sum(1 for item in raw if isinstance(item, dict) and item.get("target_id")),
        "target_covered": sum(1 for item in raw if isinstance(item, dict) and item.get("covered_target")),
        "confirmed_finding_seeds": len(findings),
        "finding_seeds": [
            {
                "content_hash": item.get("content_hash"),
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


def report_machine_payload(report: Report) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "run_dir": report.run_dir,
        "function": report.function,
        "st_file": report.st_file,
        "summary": report.summary,
        "source_context": report.source_context,
        "evidence": [asdict(item) for item in report.evidence[:30]],
        "finding_records": report.corpus.get("finding_records", [])[:30],
        "replay": {
            key: value
            for key, value in report.replay.items()
            if key != "results"
        },
        "replay_results": report.replay.get("results", [])[:20],
        "events": report.events,
        "corpus_metadata": report.corpus.get("metadata", {}),
    }


def first_structured_input(report: Report) -> str:
    for record in report.corpus.get("finding_records", []):
        seed = record.get("structured_input")
        if seed:
            return str(seed).strip()
    for item in report.evidence:
        if item.reproducer and item.reproducer.endswith(".seed"):
            text = read_text_preview(resolve_run_path(ROOT, item.reproducer), limit=1200)
            if text:
                return text.strip()
    return "n/a"


def first_stack_or_detail(report: Report) -> str:
    for item in report.evidence:
        if "stack" in item.detail.lower() or "ERROR:" in item.detail:
            return item.detail.strip()[:2000]
    return report.evidence[0].detail.strip()[:2000] if report.evidence else "n/a"


def mock_llm_analysis(report: Report) -> dict[str, Any]:
    confirmed = report.summary["confirmed_findings"]
    status = report.summary["status"]
    function = report.function or "unknown function"
    title = f"{function}: {status.replace('_', ' ')}"
    if confirmed:
        headline = f"{confirmed} confirmed vulnerability finding(s) were collected."
    else:
        headline = "No confirmed crash/timeout vulnerability was reproduced from target-corpus replay."
    structured_input = first_structured_input(report)
    stack = first_stack_or_detail(report)
    markdown = f"""# Vulnerability Report: {title}

## Summary
{headline}

## Affected Component
- Function: `{function}`
- Source: `{report.st_file or 'unknown'}`

## Impact
Confirmed findings can terminate or destabilize the compiled ST target under the observed forkserver execution. Severity should be finalized by maintainers after validating deployment reachability.

## Technical Root Cause
The deterministic evidence indicates `{report.evidence[0].kind if report.evidence else 'unknown'}` behavior. Review the affected function around the reported source excerpt and validate boundary handling for the provided structured input.

## Affected Code Review
```iecst
{report.source_context.get("excerpt", "")[:3000]}
```

Review the code above for unchecked arithmetic denominators, pointer/array bounds, and input-derived offsets. This section is generated from machine evidence and should be refined by maintainers during triage.

## Attack Vector
An attacker or upstream caller that can control the function inputs may trigger the issue with structured values equivalent to:

```text
{structured_input}
```

## Reproduction
1. Build the target with the SemantiST harness.
2. Replay the reproducer seed listed in the evidence section.
3. Observe the recorded exit kind, semantic objective, and sanitizer output.

## Evidence
```text
{stack}
```

## Suggested Fix
Add explicit input validation around boundary values, pointer/array capacity, and arithmetic denominators before performing memory writes or arithmetic operations. Add a regression test using the reproducer seed.

## Limitations
This report is generated from runtime semantic, structured finding, and replay evidence. Confirm exploitability, affected versions, and production call paths before publication.
"""
    return {
        "provider": "mock",
        "title": title,
        "summary": headline,
        "assessment": (
            "Use exact semantic objectives and structured findings/*.json records as primary evidence. "
            "Legacy target-corpus entries are replayed for compatibility and counted only if replay confirms a fault."
        ),
        "impact": "Potential denial of service or memory-safety violation, depending on deployment reachability.",
        "root_cause": "Boundary handling should be reviewed at the affected function and input combination.",
        "attack_vector": "Control of the structured ST function input parameters.",
        "affected_code": report.source_context.get("excerpt", "")[:2000],
        "reproduction": {
            "structured_input": structured_input,
            "run_dir": report.run_dir,
            "evidence_paths": [item.path for item in report.evidence],
        },
        "remediation": [
            "Validate boundary values before arithmetic or memory access.",
            "Add regression tests for each confirmed reproducer seed.",
            "Replay with sanitizers and symbols enabled before disclosure.",
        ],
        "recommended_next_steps": [
            "Minimize each confirmed reproducer.",
            "Replay confirmed inputs under ASAN/UBSAN with symbols enabled.",
            "Review pointer, buffer length, and index handling around the reported ST function.",
        ],
        "report_markdown": markdown,
        "status": status,
    }


def openai_report_analysis(report: Report, timeout_seconds: int = 60) -> dict[str, Any]:
    api_key = os.environ.get("LLM_API_KEY", DEFAULT_OPENAI_COMPATIBLE_API_KEY)
    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_OPENAI_COMPATIBLE_BASE_URL).rstrip("/")
    model = os.environ.get("LLM_MODEL", DEFAULT_OPENAI_COMPATIBLE_MODEL)
    if not api_key or not base_url or not model:
        raise RuntimeError("LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL are required")
    endpoint = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
    machine_payload = report_machine_payload(report)
    prompt = {
        "task": "Write a disclosure-ready vulnerability report from deterministic ST fuzzer evidence.",
        "rules": [
            "Treat exact semantic objectives and structured findings/*.json records as primary evidence.",
            "For legacy target-corpus entries without a finding record, claim vulnerability only if replay status is crash, timeout, or nonzero.",
            "Do not invent affected versions, CVSS scores, exploitability, or source lines not present in the evidence.",
            "If root cause or attack vector is an inference, say it is inferred from evidence.",
            "Use the structured seed values verbatim in reproduction steps.",
            "Write for maintainers of the affected ST library/runtime.",
            "Return only JSON matching the output_schema.",
        ],
        "output_schema": {
            "title": "short vulnerability title",
            "summary": "executive summary",
            "impact": "security impact and severity rationale",
            "affected_component": "function/file/component",
            "root_cause": "technical root cause, clearly marking inferences",
            "attack_vector": "how inputs can trigger the issue",
            "reproduction": {
                "steps": ["step 1", "step 2"],
                "structured_input": "NAME,TYPE,VALUE lines",
                "expected_result": "crash/timeout/error",
                "observed_result": "stack or error summary"
            },
            "evidence": ["important evidence bullet"],
            "affected_code_review": "code review notes based on source excerpt",
            "remediation": ["fix recommendation"],
            "limitations": ["what remains unknown"],
            "recommended_next_steps": ["next action"],
            "status": "evidence-derived status",
            "report_markdown": "complete markdown report suitable to send to maintainers"
        },
        "machine_evidence": machine_payload,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a senior vulnerability analyst. Generate disclosure-ready "
                            "security reports from supplied machine evidence only. Return valid JSON only."
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
                ],
            }
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(len(DEFAULT_LLM_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code not in RETRYABLE_HTTP_STATUS or attempt >= len(DEFAULT_LLM_RETRY_DELAYS):
                raise RuntimeError(f"LLM report request failed: HTTP {error.code}: {detail}") from error
        time.sleep(DEFAULT_LLM_RETRY_DELAYS[attempt])
    content = payload["choices"][0]["message"]["content"].strip()
    raw = extract_json_payload(content)
    return {
        "provider": "openai-compatible",
        "title": str(raw.get("title", "")),
        "summary": str(raw.get("summary", "")),
        "impact": str(raw.get("impact", "")),
        "affected_component": str(raw.get("affected_component", "")),
        "root_cause": str(raw.get("root_cause", "")),
        "attack_vector": str(raw.get("attack_vector", "")),
        "reproduction": raw.get("reproduction", {}),
        "evidence": [str(item) for item in raw.get("evidence", [])],
        "affected_code_review": str(raw.get("affected_code_review", "")),
        "remediation": [str(item) for item in raw.get("remediation", [])],
        "limitations": [str(item) for item in raw.get("limitations", [])],
        "assessment": str(raw.get("assessment", "")),
        "recommended_next_steps": [str(item) for item in raw.get("recommended_next_steps", [])],
        "report_markdown": str(raw.get("report_markdown", "")),
        "status": str(raw.get("status", report.summary["status"])),
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
    llm_provider: str = "mock",
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
        source_context=extract_function_source(st_file, function),
    )
    try:
        report.llm_analysis = (
            openai_report_analysis(report)
            if llm_provider == "openai-compatible"
            else mock_llm_analysis(report)
        )
    except (RuntimeError, ValueError, KeyError, TypeError, urllib.error.URLError, socket.timeout, TimeoutError) as error:
        report.llm_analysis = {
            "provider": llm_provider,
            "error": str(error),
            "fallback": mock_llm_analysis(report),
        }
    return report


def severity_class(severity: str) -> str:
    return severity.lower() if severity in {"High", "Medium", "Low"} else "info"


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
        "> **Note:** Findings are evidence output from semantic-guided fuzzing, not the system's primary contribution.",
        "",
        "## LLM Summary",
        "",
        report.llm_analysis.get("summary", ""),
        "",
        report.llm_analysis.get("assessment", ""),
        "",
        "## Disclosure Draft",
        "",
        report.llm_analysis.get("report_markdown", "").strip()
        or "No disclosure-style report was generated.",
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
            "",
            "## Recommended Next Steps",
            "",
        ]
    )
    for step in report.llm_analysis.get("recommended_next_steps", []):
        lines.append(f"- {step}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_official_markdown(report: Report, out: Path) -> None:
    markdown = report.llm_analysis.get("report_markdown", "").strip()
    if not markdown:
        markdown = mock_llm_analysis(report).get("report_markdown", "").strip()
    out.write_text(markdown + "\n", encoding="utf-8")


def stat_card(label: str, value: Any, detail: str = "") -> str:
    return (
        '<div class="card">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(str(value))}</div>'
        f'<div class="detail">{html.escape(detail)}</div>'
        "</div>"
    )


def write_html(report: Report, out: Path) -> None:
    severity_rows = "".join(
        f'<tr><td><span class="pill {severity_class(item.severity)}">{html.escape(item.severity)}</span></td>'
        f"<td>{html.escape(item.source)}</td><td>{html.escape(item.kind)}</td>"
        f"<td>{html.escape(item.title)}</td><td><code>{html.escape(item.path)}</code></td></tr>"
        for item in report.evidence
    ) or '<tr><td colspan="5">No confirmed vulnerability evidence collected.</td></tr>'
    status_counts = report.replay.get("status_counts", {})
    max_count = max([int(value) for value in status_counts.values()] or [1])
    bars = "".join(
        f'<div class="bar-row"><span>{html.escape(key)}</span>'
        f'<div class="bar"><i style="width:{(int(value) / max_count) * 100:.1f}%"></i></div>'
        f'<strong>{html.escape(str(value))}</strong></div>'
        for key, value in sorted(status_counts.items())
    )
    semantic_targets = report.corpus.get("semantic_targets", {})
    finding_seed_count = report.corpus.get("metadata", {}).get("confirmed_finding_seeds", 0)
    chain_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(item.get('target_id'))[:24])}</code></td>"
        f"<td>{html.escape(str(item.get('attempts', 0)))}</td>"
        f"<td>{html.escape(str(item.get('coverage_confirmation', False)))}</td>"
        f"<td>{html.escape(str(item.get('finding', False)))}</td>"
        "</tr>"
        for item in semantic_targets.get("target_chain", [])
        if isinstance(item, dict)
    ) or '<tr><td colspan="4">No target attempts recorded.</td></tr>'
    minimized_records = [
        record
        for record in report.corpus.get("finding_records", [])
        if isinstance(record, dict) and isinstance(record.get("minimization"), dict)
    ]
    cycle_records = [
        record
        for record in report.corpus.get("finding_records", [])
        if isinstance(record, dict)
        and (
            "cycle_count" in record
            or "cycle_ids" in record
            or "stale_cycles" in record
            or "last_changed_cycle" in record
        )
    ]
    cycle_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(record.get('content_hash') or record.get('path') or '')[:24])}</code></td>"
        f"<td>{html.escape(str(record.get('target_kind', 'n/a')))}</td>"
        f"<td>{html.escape(str(record.get('cycle_count', 'n/a')))}</td>"
        f"<td>{html.escape(str(record.get('cycle_ids', [])))}</td>"
        f"<td>{html.escape(str(record.get('stale_cycles', [])))}</td>"
        f"<td>{html.escape(str(record.get('last_changed_cycle', 'n/a')))}</td>"
        f"<td>{html.escape(str(len(record.get('state_hashes', []))))}</td>"
        "</tr>"
        for record in cycle_records
    ) or '<tr><td colspan="7">No cycle metadata available.</td></tr>'
    minimization_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(record.get('content_hash') or record.get('path') or '')[:24])}</code></td>"
        f"<td><code>{html.escape(str(record['minimization'].get('minimized_seed', 'n/a')))}</code></td>"
        f"<td>{html.escape(str(record['minimization'].get('minimized_status', 'n/a')))}</td>"
        f"<td>{html.escape(str(len(record['minimization'].get('removed_records', []))))}</td>"
        f"<td>{html.escape(str(len(record['minimization'].get('changed_values', []))))}</td>"
        f"<td>{html.escape(', '.join(record['minimization'].get('critical_fields', [])) or 'n/a')}</td>"
        f"<td>{html.escape(str(record['minimization'].get('signature_match', 'n/a')))}</td>"
        f"<td>{html.escape(str((record['minimization'].get('minimized_signature') or {}).get('stack_hash', 'n/a')))}</td>"
        "</tr>"
        for record in minimized_records
    ) or '<tr><td colspan="8">No minimized finding metadata available.</td></tr>'
    disclosure_markdown = report.llm_analysis.get("report_markdown", "").strip()
    disclosure_overview = "".join(
        f"<p><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</p>"
        for label, value in (
            ("Title", report.llm_analysis.get("title", "")),
            ("Impact", report.llm_analysis.get("impact", "")),
            ("Root Cause", report.llm_analysis.get("root_cause", "")),
            ("Attack Vector", report.llm_analysis.get("attack_vector", "")),
        )
        if value
    )
    css = """
body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:#f6f7f9;color:#1d2430}
header{background:#ffffff;border-bottom:1px solid #d8dee8;padding:24px 32px}
main{max-width:1180px;margin:0 auto;padding:24px 20px 40px}
h1{font-size:28px;margin:0 0 8px}h2{font-size:18px;margin:28px 0 12px}.muted{color:#5c6778}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:14px}
.label{font-size:12px;text-transform:uppercase;color:#697386}.value{font-size:28px;font-weight:700;margin-top:4px}.detail{font-size:12px;color:#697386;margin-top:4px}
.panel{background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:12px}
.pill{display:inline-block;border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700}.high{background:#ffe1e1;color:#9b1c1c}.medium{background:#fff1c7;color:#765000}.low,.info{background:#e6eefc;color:#244a84}
table{width:100%;border-collapse:collapse;background:#fff}th,td{border-bottom:1px solid #e6ebf2;text-align:left;padding:9px 10px;vertical-align:top}th{font-size:12px;color:#697386;text-transform:uppercase}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all}.bar-row{display:grid;grid-template-columns:110px 1fr 48px;gap:10px;align-items:center;margin:8px 0}.bar{height:10px;background:#e6ebf2;border-radius:999px;overflow:hidden}.bar i{display:block;height:100%;background:#2f6fed}
pre{white-space:pre-wrap;background:#101820;color:#edf2f7;padding:12px;border-radius:8px;overflow:auto}
"""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SemantiST Vulnerability Report</title><style>{css}</style></head>
<body>
<header><h1>Vulnerability Report: {html.escape(report.function or 'unknown')}</h1>
<div class="muted">Generated {html.escape(report.generated_at)} from <code>{html.escape(report.run_dir)}</code></div></header>
<main>
<section class="grid">
{stat_card('Status', report.summary['status'])}
{stat_card('Dedup Findings', report.summary.get('deduplicated_finding_count', 0), f"{report.summary.get('confirmed_finding_count', 0)} confirmed, {report.summary.get('duplicate_finding_count', 0)} duplicates")}
{stat_card('Confirmed Findings', report.summary.get('confirmed_finding_count', report.summary['confirmed_findings']))}
{stat_card('Raw Objectives', report.summary.get('raw_objective_count', 0), f"{report.summary.get('raw_finding_record_count', 0)} finding records + target corpus")}
{stat_card('Unstable/Candidate', f"{report.summary.get('unstable_finding_count', 0)} / {report.summary.get('candidate_finding_count', 0)}", 'replay did not confirm')}
{stat_card('Finding Seeds', finding_seed_count, 'confirmed reproducers in unified corpus')}
{stat_card('Semantic Targets', semantic_targets.get('attempted_targets', 0), f"{semantic_targets.get('attempt_count', 0)} attempt(s)")}
{stat_card('Replayed', report.replay.get('target_corpus_replayed', 0), f"limit {report.replay.get('limit')}")}
</section>
<section class="panel"><h2>Summary</h2><p>{html.escape(report.llm_analysis.get('summary',''))}</p><p class="muted">{html.escape(report.llm_analysis.get('assessment',''))}</p></section>
<section class="panel"><h2>Disclosure Draft</h2>{disclosure_overview or '<p>No LLM disclosure overview generated.</p>'}<pre>{html.escape(disclosure_markdown[:8000])}</pre></section>
<section class="panel"><h2>Replay Status</h2>{bars or '<p>No replay results.</p>'}</section>
<section><h2>Cycle Metadata</h2><table><thead><tr><th>Finding</th><th>Target Kind</th><th>Cycles</th><th>Cycle IDs</th><th>Stale Cycles</th><th>Last Changed</th><th>State Hash Rows</th></tr></thead><tbody>{cycle_rows}</tbody></table></section>
<section><h2>Minimized Findings</h2><table><thead><tr><th>Finding</th><th>Minimized Seed</th><th>Status</th><th>Removed</th><th>Changed</th><th>Critical Fields</th><th>Signature</th><th>Stack</th></tr></thead><tbody>{minimization_rows}</tbody></table></section>
<section><h2>Evidence</h2><table><thead><tr><th>Severity</th><th>Source</th><th>Kind</th><th>Title</th><th>Path</th></tr></thead><tbody>{severity_rows}</tbody></table></section>
<section><h2>Semantic Target Attempts</h2><table><thead><tr><th>Target</th><th>Attempts</th><th>Coverage Confirmed</th><th>Finding</th></tr></thead><tbody>{chain_rows}</tbody></table></section>
<section class="panel"><h2>Raw Summary</h2><pre>{html.escape(json.dumps({'summary': report.summary, 'events': report.events, 'corpus': report.corpus}, indent=2, sort_keys=True))}</pre></section>
</main></body></html>
"""
    out.write_text(body, encoding="utf-8")


def write_report_bundle(report: Report, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "report.json",
        "markdown": output_dir / "report.md",
        "official_markdown": output_dir / "official-report.md",
        "html": output_dir / "report.html",
    }
    write_json(report, paths["json"])
    write_markdown(report, paths["markdown"])
    write_official_markdown(report, paths["official_markdown"])
    write_html(report, paths["html"])
    return paths


def generate_report(
    run_dir: Path,
    *,
    function: str | None = None,
    st_file: Path | None = None,
    llm_provider: str = "mock",
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
        llm_provider=llm_provider,
        replay_limit=replay_limit,
        replay_timeout=replay_timeout,
        replay=replay,
        minimize_findings=minimize_findings,
    )
    paths = write_report_bundle(report, output_dir or (run_dir / "vulnerability-report"))
    return report, paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a visual vulnerability report for an SemantiST run")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--function")
    parser.add_argument("--st-file", type=Path)
    parser.add_argument(
        "--llm-provider",
        choices=("mock", "openai-compatible"),
        default=os.environ.get("SEMANTIST_LLM_PROVIDER", "openai-compatible"),
    )
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
        llm_provider=args.llm_provider,
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
