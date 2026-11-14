#!/usr/bin/env python3
"""Independently recheck native candidates from an improvement experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fuzzer.corpus.frames import build_testcase, parse_testcase, validate
from fuzzer.pipeline.model import TargetModel, _AbiLike
from fuzzer.pipeline.probe import classify, is_finding_candidate, probe


def candidate_key(info: dict) -> tuple:
    return (info.get("oracle"), info.get("runtime_id"),
            json.dumps(info.get("source"), sort_keys=True))


def review_target(root: Path, target_id: str) -> list[dict]:
    run = root / "runs" / target_id / "semantist" / "improved" / "s0" / "100s"
    target = root / "builds" / target_id / "semantist" / "build" / "fuzz_target"
    model = TargetModel.load(run, run / "bundle")
    abi = _AbiLike(model)
    probe_dir = root / "review-probe" / target_id
    records = []
    for metadata_path in sorted((run / "findings" / "candidates").glob("candidate-*.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        runtime_id = metadata.get("runtime_id")
        expected_runtime_id = (runtime_id if metadata.get("kind") == "semantic_goal"
                               and type(runtime_id) is int else None)
        testcase_path = metadata_path.with_suffix(".txt")
        testcase = testcase_path.read_bytes()
        record = {
            "target": target_id,
            "candidate": str(testcase_path.relative_to(root)),
            "engine_kind": metadata.get("kind"),
            "created_epoch_ns": metadata.get("created_epoch_ns"),
        }
        try:
            parsed = parse_testcase(testcase, abi)
            validate(parsed, abi)
            record["input_validity"] = "satisfies_confirmed"
            record["frames"] = len(parsed.frames)
        except Exception as error:
            record["input_validity"] = "violates_contract"
            record["input_error"] = str(error)

        plain = probe(target, testcase, work_dir=probe_dir, timeout_s=5, trace=False)
        record["plain_returncode"] = plain.returncode
        record["plain_timeout"] = plain.timed_out
        if plain.timed_out:
            record["status"] = "TIMEOUT"
            records.append(record)
            continue
        plain_info = classify(plain, model.runtime_index,
                              expected_runtime_id=expected_runtime_id)
        semantic = not is_finding_candidate(plain_info) and plain.returncode == 0
        first = (probe(target, testcase, work_dir=probe_dir, timeout_s=5, trace=True)
                 if semantic else plain)
        first_info = classify(first, model.runtime_index,
                              expected_runtime_id=expected_runtime_id)
        boundary_ids = sorted(runtime_id for runtime_id in first.hits
                              if (model.runtime_index.get(runtime_id) or {}).get("hazard")
                              == "arithmetic_boundary")
        record["numeric_boundary_runtime_ids"] = boundary_ids
        if not is_finding_candidate(first_info):
            if boundary_ids and not first.timed_out:
                second = probe(target, testcase, work_dir=probe_dir, timeout_s=5, trace=True)
                repeat_ids = {runtime_id for runtime_id in second.hits
                              if (model.runtime_index.get(runtime_id) or {}).get("hazard")
                              == "arithmetic_boundary"}
                record["status"] = ("numeric_boundary_observation" if
                                    set(boundary_ids) & repeat_ids else "REPLAY_MISMATCH")
            else:
                record["status"] = "REPLAY_MISMATCH"
            records.append(record)
            continue
        second = probe(target, testcase, work_dir=probe_dir, timeout_s=5, trace=semantic)
        second_info = classify(second, model.runtime_index,
                               expected_runtime_id=expected_runtime_id)
        if not is_finding_candidate(second_info) or candidate_key(first_info) != candidate_key(second_info):
            record["status"] = "REPLAY_MISMATCH"
            records.append(record)
            continue
        record.update({
            "status": "confirmed",
            "oracle": first_info["oracle"],
            "hazard": first_info["hazard"],
            "kind": first_info["kind"],
            "modeling": first_info.get("modeling"),
            "runtime_id": first_info["runtime_id"],
            "source": first_info["source"],
            "cycles": first.hits.get(first_info["runtime_id"], [])
                if first_info["runtime_id"] is not None else [],
            "confirmation": "two_independent_native_replays",
            "semantic_trace": semantic,
        })
        records.append(record)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_root", type=Path)
    args = parser.parse_args()
    root = args.experiment_root.resolve()
    matrix = json.loads((root / "matrix.json").read_text(encoding="utf-8"))
    target_ids = sorted({job["target"]["id"] for job in matrix["jobs"]
                         if job["tool"] == "semantist" and job["configuration"] == "improved"})
    records = [record for target_id in target_ids for record in review_target(root, target_id)]
    unique = {}
    for record in records:
        if record["status"] != "confirmed" or record["input_validity"] != "satisfies_confirmed":
            continue
        key = (record["target"], record["oracle"], record["runtime_id"],
               json.dumps(record["source"], sort_keys=True))
        if key not in unique or record["created_epoch_ns"] < unique[key]["created_epoch_ns"]:
            unique[key] = record
    for record in unique.values():
        cycles = record["cycles"]
        if not cycles or min(cycles) == 0:
            continue
        target_id = record["target"]
        run = root / "runs" / target_id / "semantist" / "improved" / "s0" / "100s"
        target = root / "builds" / target_id / "semantist" / "build" / "fuzz_target"
        model = TargetModel.load(run, run / "bundle")
        abi = _AbiLike(model)
        testcase = (root / record["candidate"]).read_bytes()
        parsed = parse_testcase(testcase, abi)
        frame_index = min(cycles)
        frame = parsed.frames[frame_index]
        cold_case = build_testcase(
            abi, [frame.values], shared=parsed.shared,
            init_profile=parsed.init_profile, frame_dt_ns=[frame.dt_ns],
            include_dt=frame.dt_ns is not None,
        )
        cold = probe(target, cold_case, work_dir=root / "review-probe" / target_id,
                     timeout_s=5, trace=True)
        record["cold_single_frame_at_first_hit"] = {
            "original_frame_index": frame_index,
            "same_runtime_id_hit": record["runtime_id"] in cold.hits,
            "returncode": cold.returncode,
            "timed_out": cold.timed_out,
        }
    output = {
        "schema_version": "semantist.improvement-candidate-review/1.0.0",
        "experiment_root": str(root),
        "target_count": len(target_ids),
        "raw_candidate_count": len(records),
        "confirmed_candidate_count": sum(record["status"] == "confirmed" for record in records),
        "confirmed_valid_candidate_count": sum(
            record["status"] == "confirmed" and record["input_validity"] == "satisfies_confirmed"
            for record in records),
        "numeric_boundary_candidate_count": sum(
            record["status"] == "numeric_boundary_observation" for record in records),
        "confirmed_target_count": len({record["target"] for record in unique.values()}),
        "unique_target_oracle_locations": sorted(
            unique.values(), key=lambda record: (record["target"], record["runtime_id"] or -1)),
        "candidates": records,
    }
    destination = root / "candidate_recheck.json"
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{destination}: {output['confirmed_target_count']} targets, "
          f"{len(unique)} target/oracle/locations, "
          f"{output['confirmed_valid_candidate_count']}/{len(records)} valid confirmed candidates")


if __name__ == "__main__":
    main()
