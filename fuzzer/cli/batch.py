"""semantist-batch: run the defect suite or benchmark set sequentially."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFECT_MANIFEST = ROOT / "benchmarks" / "defects" / "manifest.json"
BENCHMARK_MANIFEST = ROOT / "benchmarks" / "oscat" / "manifest.json"


def _defect_targets(suite: str, variant: str) -> list[dict[str, Any]]:
    manifest = json.loads(DEFECT_MANIFEST.read_text(encoding="utf-8"))
    targets: list[dict[str, Any]] = []
    for case in manifest.get("cases", []):
        if suite not in ("all", "defects"):
            break
        if case.get("set") == "designed":
            variants = ["buggy", "fixed"] if variant == "both" else [variant]
            for item in variants:
                st_file = ROOT / case["dir"] / item / "target.st"
                if st_file.exists():
                    targets.append(
                        {
                            "suite": "defects",
                            "id": case["id"],
                            "variant": item,
                            "function": case["function"],
                            "st_file": st_file,
                            "manifest": case,
                        }
                    )
        else:
            # Known defects have no fixed variant in the repository; a `fixed`
            # request is reported as not applicable rather than silently run.
            if variant == "fixed":
                continue
            targets.append(
                {
                    "suite": "defects",
                    "id": case["id"],
                    "variant": "buggy",
                    "function": case["function"],
                    "st_file": ROOT / case["st_file"],
                    "compatibility_manifest": case.get("compatibility_manifest"),
                    "manifest": case,
                }
            )
    return targets


def _benchmark_targets(manifest_path: Path = BENCHMARK_MANIFEST) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets = []
    for entry in manifest.get("targets", []):
        targets.append(
            {
                "suite": entry.get("suite", "benchmarks"),
                "id": entry["id"],
                "variant": "buggy",
                "function": entry["function"],
                "st_file": ROOT / entry.get("normalized_file", entry["st_file"]),
                "compatibility_manifest": entry.get("compatibility_manifest"),
                "manifest": entry,
            }
        )
    return targets


def _read_report(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "report.json"
    report: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(raw, dict):
            report.update(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    public_path = run_dir / "vulnerability-report" / "report.json"
    try:
        public = json.loads(public_path.read_text(encoding="utf-8")) if public_path.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        public = {}
    if isinstance(public, dict):
        summary = public.get("summary")
        if isinstance(summary, dict):
            # The public report is a downstream view. Keep the raw campaign
            # evidence when that view is truncated or malformed; otherwise a
            # bad exchange JSON could erase FAILED/UNSUPPORTED query records.
            expected_types = {
                "status": str,
                "status_counts": dict,
                "support_status": str,
                "execution_status": dict,
                "semantic_fidelity": str,
                "stub_used": list,
                "model_scope": dict,
                "model_result_counts": dict,
                "confirmed_model_counts": dict,
                "symbolic_queries": list,
                "native_replay_results": list,
            }
            for key, expected_type in expected_types.items():
                value = summary.get(key)
                if key not in summary or not isinstance(value, expected_type):
                    continue
                if key == "execution_status" and isinstance(report.get(key), dict):
                    report[key] = {**report[key], **value}
                else:
                    report[key] = value
        replay = public.get("replay")
        if isinstance(replay, dict):
            report["_public_replay"] = replay
    return report


def _run_one(target: dict[str, Any], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_dir = out_dir / target["id"] / target["variant"]
    if run_dir.exists() and any(run_dir.iterdir()):
        import shutil

        shutil.rmtree(run_dir)
    command = [
        sys.executable,
        "-m",
        "fuzzer.pipeline.core",
        "--mode",
        "hybrid",
        "--st-file",
        str(target["st_file"]),
        "--function",
        target["function"],
        "--run-dir",
        str(run_dir),
        "--fuzz-timeout",
        str(args.time_per_target),
        "--build-timeout",
        str(max(0.01, float(args.build_overhead))),
        "--search-jobs",
        str(args.search_jobs),
        "--seed",
        str(args.seed),
    ]
    if target.get("compatibility_manifest"):
        command += ["--compatibility-manifest", str(ROOT / target["compatibility_manifest"])]
    for switch in args.switches:
        command.append(switch)
    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    error_log = run_dir / "error.log"
    error_text = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        returncode = 127
        error_text = f"pipeline launch failed: {error}"
    else:
        try:
            extra_budget = 240 if any(switch in args.switches for switch in
                                      ("--full-fuzz-duration", "--full-hybrid-duration")) else 0
            stdout, stderr = process.communicate(timeout=args.time_per_target + args.build_overhead + extra_budget)
            returncode = process.returncode
            error_text = (stdout or "") + "\n" + (stderr or "")
        except subprocess.TimeoutExpired as error:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            raw_out = error.stdout or ""
            raw_err = error.stderr or ""
            if isinstance(raw_out, bytes):
                raw_out = raw_out.decode("utf-8", errors="replace")
            if isinstance(raw_err, bytes):
                raw_err = raw_err.decode("utf-8", errors="replace")
            tail_out, tail_err = process.communicate()
            error_text = str(raw_out) + "\n" + str(raw_err) + "\n" + (tail_out or "") + "\n" + (tail_err or "")
    wall = time.monotonic() - started
    if error_text.strip():
        error_log.parent.mkdir(parents=True, exist_ok=True)
        error_log.write_text("\n".join(error_text.splitlines()[-40:]) + "\n", encoding="utf-8")
    report = _read_report(run_dir)
    contract_path = run_dir / "bundle" / "execution-contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        contract = {}
    findings = report.get("findings") or []
    candidate_dir = run_dir / "findings" / "candidates"
    candidate_files = sorted(candidate_dir.glob("*.txt")) if candidate_dir.is_dir() else []
    candidate_count = max(int(report.get("candidates", 0) or 0), len(candidate_files))
    symbolic_queries = report.get("symbolic_queries") if isinstance(report.get("symbolic_queries"), list) else []
    query_status_counts = {
        status: sum(1 for item in symbolic_queries
                    if item.get("status") == status or item.get("native_replay_status") == status)
        for status in ("FAILED", "ERROR", "UNSUPPORTED", "TIMEOUT", "REPLAY_MISMATCH",
                       "SAT", "SAT_APPROX", "UNSAT", "UNKNOWN")
    }
    unsupported_path = run_dir / "bundle" / "klee" / "UNSUPPORTED.json"
    if unsupported_path.is_file() and not symbolic_queries:
        query_status_counts["UNSUPPORTED"] += 1
    first = findings[0] if findings else {}
    bundle_ready = (run_dir / "bundle" / "abi.json").exists()
    engine_summary = {}
    for summary_path in (run_dir / "metrics" / "engine-summary.json", run_dir / "metrics" / "summary.json"):
        if summary_path.exists():
            try:
                engine_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                engine_summary = {}
            if isinstance(engine_summary, dict):
                break
    native_fuzz_elapsed = engine_summary.get("elapsed_s")
    full_duration_required = "--full-fuzz-duration" in args.switches
    full_hybrid_required = "--full-hybrid-duration" in args.switches
    duration_met = (not full_duration_required or
                    isinstance(native_fuzz_elapsed, (int, float)) and
                    native_fuzz_elapsed >= args.time_per_target and
                    isinstance(engine_summary.get("scans"), int) and
                    engine_summary["scans"] > 0)
    if full_hybrid_required:
        native_budget = engine_summary.get("budget_s")
        duration_met = (duration_met and isinstance(native_budget, (int, float))
                        and native_budget > 0
                        and isinstance(native_fuzz_elapsed, (int, float))
                        and native_fuzz_elapsed >= native_budget
                        and isinstance(engine_summary.get("scans"), int)
                        and engine_summary["scans"] > 0)
    recorded_execution = report.get("execution_status")
    native_timed_out = (isinstance(recorded_execution, dict)
                        and str(recorded_execution.get("native", "")).upper() == "TIMEOUT")
    timed_out = timed_out or native_timed_out
    report_status = str(report.get("status", "")).lower()
    report_failed = report_status in {"campaign_failed", "build_failed", "failed"}
    if timed_out:
        status = "campaign_failed" if bundle_ready else "build_failed"
    elif returncode not in (None, 0):
        status = "campaign_failed" if bundle_ready else "build_failed"
    elif report_failed or not duration_met:
        status = "build_failed" if report_status == "build_failed" and not bundle_ready else "campaign_failed"
    else:
        status = "completed" if report else ("campaign_failed" if bundle_ready else "build_failed")
    execution_status = dict(recorded_execution) if isinstance(recorded_execution, dict) else {}
    if unsupported_path.is_file() and not symbolic_queries:
        execution_status["symbolic"] = "UNSUPPORTED"
        try:
            unsupported = json.loads(unsupported_path.read_text(encoding="utf-8"))
            reason = str(unsupported.get("reason") or "symbolic_model_unavailable")
            detail = unsupported.get("detail")
            execution_status["symbolic_reason"] = f"{reason}: {detail}" if detail else reason
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            execution_status["symbolic_reason"] = "symbolic_model_unavailable"
    execution_status["build"] = "completed" if bundle_ready else "FAILED"
    execution_status["campaign"] = ("TIMEOUT" if timed_out else "FAILED" if returncode not in (None, 0) or report_failed or not duration_met
                                     else "completed" if report else "FAILED")
    replay = report.get("_public_replay")
    replay_status_counts = replay.get("status_counts", {}) if isinstance(replay, dict) else {}
    if isinstance(replay, dict) and replay.get("enabled"):
        if replay_status_counts.get("timeout", 0):
            execution_status["replay"] = "TIMEOUT"
        elif any(replay_status_counts.get(status, 0) for status in ("crash", "nonzero", "replay_error")):
            execution_status["replay"] = "FAILED"
        else:
            execution_status["replay"] = "completed"
    else:
        execution_status.setdefault("replay", "not_run")
    recorded_status_counts = report.get("status_counts")
    status_counts = {
        "candidate": candidate_count,
        "confirmed": int(report.get("confirmed", len(findings)) or 0),
        "FAILED": query_status_counts["FAILED"],
        "ERROR": query_status_counts["ERROR"],
        "UNSUPPORTED": query_status_counts["UNSUPPORTED"],
        "TIMEOUT": query_status_counts["TIMEOUT"],
        "REPLAY_MISMATCH": query_status_counts["REPLAY_MISMATCH"],
    }
    if isinstance(recorded_status_counts, dict):
        for key in status_counts:
            if key in recorded_status_counts:
                try:
                    status_counts[key] = int(recorded_status_counts[key] or 0)
                except (TypeError, ValueError):
                    pass
    status_counts["UNSUPPORTED"] = max(status_counts["UNSUPPORTED"],
                                       query_status_counts["UNSUPPORTED"])
    return {
        "id": target["id"],
        "suite": target["suite"],
        "variant": target["variant"],
        "function": target["function"],
        "run_dir": str(run_dir),
        "reached": bool(findings),
        "candidate": candidate_count > 0,
        "candidates": candidate_count,
        "confirmed": report.get("confirmed", len(findings)),
        "source_risk_confirmed_under_model": report.get("source_risk_confirmed_under_model", 0),
        "status_counts": status_counts,
        "oracle": first.get("oracle"),
        "first_candidate_s": first.get("first_candidate_s"),
        "confirmed_s": first.get("confirmed_s"),
        "depth": first.get("depth") or first.get("minimized_depth"),
        "provenance": (first.get("provenance") or {}).get("source"),
        "support_status": report.get("support_status") or contract.get("support_status", "unknown"),
        "semantic_fidelity": report.get("semantic_fidelity") or contract.get("semantic_fidelity", "unknown"),
        "stub_used": report.get("stub_used") or contract.get("stub_used", []),
        "model_scope": report.get("model_scope") or contract.get("model_scope", {}),
        "model_result_counts": report.get("model_result_counts", {}),
        "confirmed_model_counts": report.get("confirmed_model_counts", {}),
        "native_replay_results": report.get("native_replay_results", []),
        "execution_status": execution_status,
        "replay_status_counts": replay_status_counts,
        "replay_mismatches": status_counts["REPLAY_MISMATCH"],
        "symbolic_status_counts": query_status_counts,
        "snapshot_hit_rate": _snapshot_hit_rate(engine_summary),
        "native_fuzz_elapsed_s": native_fuzz_elapsed,
        "native_fuzz_scans": engine_summary.get("scans"),
        "wall_s": round(wall, 2),
        "timed_out": timed_out,
        "returncode": returncode,
        "false_positive": bool(report.get("source_risk_confirmed_under_model", 0))
        and target["variant"] == "fixed",
        "repro_package": (run_dir / "findings" / "confirmed").exists()
        and any((run_dir / "findings" / "confirmed").iterdir()),
        "status": status,
        "error_log": str(error_log) if error_log.exists() else None,
    }


def _snapshot_hit_rate(engine_summary: dict[str, Any]) -> float | None:
    """Compute cache hit rate only from counters emitted by the engine."""
    if "snapshot_attempts" not in engine_summary:
        return None
    try:
        attempts = int(engine_summary["snapshot_attempts"])
        hits = int(engine_summary.get("cache_hits", 0))
    except (TypeError, ValueError):
        return None
    if attempts <= 0:
        return None
    return min(1.0, max(0.0, hits / attempts))


def _batch_options(args: argparse.Namespace) -> dict[str, Any]:
    options = {
        "suite": args.suite,
        "variant": args.variant,
        "time_per_target": args.time_per_target,
        "build_overhead": args.build_overhead,
        "search_jobs": args.search_jobs,
        "seed": args.seed,
        "switches": args.switches,
    }
    if args.suite in ("benchmarks", "all"):
        options["benchmark_manifest"] = str(getattr(args, "benchmark_manifest", BENCHMARK_MANIFEST))
    return options


def _write_summary(results: list[dict[str, Any]], out_dir: Path, args: argparse.Namespace,
                   *, expected_total: int | None = None) -> tuple[Path, Path]:
    summary = {
        **_batch_options(args),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "expected_targets": expected_total if expected_total is not None else len(results),
        "complete": expected_total is None or len(results) == expected_total,
        "targets": results,
        "counts": {
            "total": len(results),
            "candidate_targets": sum(1 for item in results if item.get("candidate")),
            "candidates": sum(int(item.get("candidates", 0) or 0) for item in results),
            "reached": sum(1 for item in results if item["reached"]),
            "confirmed": sum(1 for item in results if item["confirmed"]),
            "false_positives": sum(1 for item in results if item["false_positive"]),
            "timed_out": sum(1 for item in results if item["timed_out"]),
            "built": sum(1 for item in results if item["status"] != "build_failed"),
            "completed": sum(1 for item in results if item["status"] == "completed"),
            "build_failed": sum(1 for item in results if item["status"] == "build_failed"),
            "campaign_failed": sum(1 for item in results if item["status"] == "campaign_failed"),
            "semantic_confirmed": sum(item["confirmed"] for item in results
                                      if item["semantic_fidelity"] == "semantic"),
            "approximate_confirmed": sum(
                item.get("confirmed_model_counts", {}).get("approximate", 0)
                if item.get("confirmed_model_counts")
                else item["confirmed"] if item["semantic_fidelity"] == "approximate" else 0
                for item in results
            ),
            "faithful_model_results": sum(item.get("model_result_counts", {}).get("faithful", 0)
                                          for item in results),
            "approximate_model_results": sum(item.get("model_result_counts", {}).get("approximate", 0)
                                             for item in results),
            "unknown_fidelity": sum(1 for item in results if item["semantic_fidelity"] == "unknown"),
            "symbolic_failed": sum(item.get("symbolic_status_counts", {}).get("FAILED", 0) for item in results),
            "symbolic_error": sum(item.get("symbolic_status_counts", {}).get("ERROR", 0) for item in results),
            "symbolic_unsupported": sum(item.get("symbolic_status_counts", {}).get("UNSUPPORTED", 0) for item in results),
            "symbolic_timeout": sum(item.get("symbolic_status_counts", {}).get("TIMEOUT", 0) for item in results),
            "replay_mismatch": sum(item.get("status_counts", {}).get("REPLAY_MISMATCH", 0) for item in results),
        },
    }
    json_path = out_dir / "batch-summary.json"
    json_tmp = json_path.with_suffix(".json.tmp")
    json_tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(json_tmp, json_path)
    lines = [
        f"# semantist-batch: {args.suite} / {args.variant}",
        "",
        f"Time per target: {args.time_per_target}s",
        "",
        "| id | variant | status | campaign | support | fidelity | symbolic | replay | candidate | confirmed | oracle | first candidate (s) | confirmed (s) | source | repro |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for item in results:
        execution = item.get("execution_status") if isinstance(item.get("execution_status"), dict) else {}
        symbolic_counts = item.get("symbolic_status_counts") if isinstance(item.get("symbolic_status_counts"), dict) else {}
        symbolic = ",".join(
            f"{key}:{symbolic_counts[key]}"
            for key in ("SAT", "SAT_APPROX", "UNSAT", "UNKNOWN", "UNSUPPORTED", "TIMEOUT", "FAILED", "REPLAY_MISMATCH")
            if symbolic_counts.get(key)
        ) or "-"
        lines.append(
            f"| {item['id']} | {item['variant']} | {item.get('status', '-')} | {execution.get('campaign', '-')} | "
            f"{item.get('support_status', '-')} | {item.get('semantic_fidelity', '-')} | {symbolic} | "
            f"{execution.get('replay', '-')} | {'yes' if item.get('candidate') else 'no'} | "
            f"{'yes' if item['reached'] else 'no'} | "
            f"{item['oracle'] or '-'} | {item['first_candidate_s'] if item['first_candidate_s'] is not None else '-'} | "
            f"{item['confirmed_s'] if item['confirmed_s'] is not None else '-'} | "
            f"{item['provenance'] or '-'} | {'yes' if item['repro_package'] else 'no'} |"
        )
    lines += [
        "",
        f"Progress: {len(results)}/{summary['expected_targets']} targets.",
        f"Candidates: {summary['counts']['candidate_targets']}/{len(results)} targets "
        f"({summary['counts']['candidates']} files); confirmed: {summary['counts']['reached']}/{len(results)}; "
        f"false positives: {summary['counts']['false_positives']}.",
        "",
    ]
    md_path = out_dir / "batch-summary.md"
    md_tmp = md_path.with_suffix(".md.tmp")
    md_tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(md_tmp, md_path)
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("defects", "benchmarks", "all"), default="defects")
    parser.add_argument("--variant", choices=("buggy", "fixed", "both"), default="both")
    parser.add_argument("--time-per-target", type=int, default=300,
                        help="seconds forwarded as each target's fuzz-timeout; total campaign budget with --full-hybrid-duration")
    parser.add_argument("--benchmark-manifest", type=Path, default=BENCHMARK_MANIFEST,
                        help="target index for --suite benchmarks (default: curated OSCAT suite)")
    parser.add_argument("--build-overhead", type=int, default=180)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--search-jobs", type=int, default=12)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--switch", dest="switches", action="append", default=[])
    parser.add_argument("--resume", action="store_true", help="reuse completed targets from this batch")
    args = parser.parse_args(argv)
    args.benchmark_manifest = args.benchmark_manifest.resolve()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    targets: list[dict[str, Any]] = []
    if args.suite in ("defects", "all"):
        targets.extend(_defect_targets("defects", args.variant))
    if args.suite in ("benchmarks", "all"):
        targets.extend(_benchmark_targets(args.benchmark_manifest))

    summary_path = out_dir / "batch-summary.json"
    if args.resume and summary_path.is_file():
        try:
            previous = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            parser.error(f"cannot resume unreadable batch summary: {error}")
        if not isinstance(previous, dict) or any(previous.get(key) != value
                                                 for key, value in _batch_options(args).items()):
            parser.error("cannot resume batch with different suite, budget, seed or switches")
        if previous.get("expected_targets") != len(targets):
            parser.error("cannot resume batch with a different target count")
    elif not args.resume and any(out_dir.iterdir()):
        parser.error("batch output directory is not empty; choose a new directory or use --resume")

    results: list[dict[str, Any]] = []
    for index, target in enumerate(targets, 1):
        run_dir = out_dir / target["id"] / target["variant"]
        result_path = run_dir / "batch-result.json"
        if args.resume and result_path.is_file():
            try:
                saved = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                parser.error(f"cannot resume unreadable target result {result_path}: {error}")
            if not isinstance(saved, dict) or any(saved.get(key) != target[key]
                                                     for key in ("id", "suite", "variant", "function")):
                parser.error(f"cannot resume mismatched target result {result_path}")
            print(f"[batch] {index}/{len(targets)} {target['id']} ({target['variant']}) reused", flush=True)
            result = saved
        else:
            if args.resume and run_dir.exists() and any(run_dir.iterdir()):
                suffix = 1
                while run_dir.with_name(f"{run_dir.name}.interrupted-{suffix}").exists():
                    suffix += 1
                preserved = run_dir.with_name(f"{run_dir.name}.interrupted-{suffix}")
                run_dir.rename(preserved)
                print(f"[batch] preserved incomplete run at {preserved}", flush=True)
            print(f"[batch] {index}/{len(targets)} {target['id']} ({target['variant']})", flush=True)
            result = _run_one(target, out_dir, args)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_tmp = result_path.with_suffix(".json.tmp")
            result_tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(result_tmp, result_path)
        results.append(result)
        _write_summary(results, out_dir, args, expected_total=len(targets))
    json_path, md_path = _write_summary(results, out_dir, args, expected_total=len(targets))
    print(f"[batch] wrote {json_path} and {md_path}")
    return 1 if any(item["status"] != "completed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
