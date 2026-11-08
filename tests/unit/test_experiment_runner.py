"""Interface checks for the frozen paper run contract."""

import json
import os
import struct
from argparse import Namespace
from pathlib import Path

import pytest

from fuzzer.cli.experiment import _container_command, _validate_trial_request
from fuzzer.experiments import artifacts, baselines, worker
from fuzzer.experiments.artifacts import build_input_stats, build_path, plan_path, write_json
from fuzzer.experiments.matrix import Job, load_targets, make_matrix
from fuzzer.experiments.summary import summarize


def test_matrix_reuses_runs_across_the_six_comparisons():
    jobs = make_matrix()
    assert len(jobs) == 3500
    assert sum(job.online_seconds == 300 for job in jobs) == 3300
    assert sum(job.online_seconds == 600 for job in jobs) == 200
    assert sum("overall" in job.experiments and "core_ablation" in job.experiments
               for job in jobs) == 500
    _, focus = load_targets()
    assert len(focus) == 20
    assert {item["kind"] for item in focus} == {"FUNCTION_BLOCK"}


def test_frozen_plan_seeds_are_imported_without_importing_prep_findings(tmp_path, monkeypatch):
    target = next(item for item in load_targets()[0] if item["id"] == "oscat_basic_inc2")
    build = build_path(tmp_path, target["id"])
    (build / "build").mkdir(parents=True)
    (build / "build" / "fuzz_target").write_text("binary")
    (build / "source").mkdir()
    (build / "source" / "target.st").write_text("FUNCTION INC2 END_FUNCTION")
    (build / "template" / "bundle").mkdir(parents=True)
    write_json(build / "template" / "bundle" / "execution-contract.json",
               {"contract_id": "frozen-contract"})
    write_json(build / "build.json", {"target": target,
                                       "build_configuration": artifacts.BUILD_CONFIGURATION,
                                       "input_stats": build_input_stats(target)})
    plan = plan_path(tmp_path, target["id"])
    (plan / "unified-corpus" / "seeds").mkdir(parents=True)
    (plan / "unified-corpus" / "seeds" / "llm-plan-0001.seed").write_text("0.X,INT,2\n")
    write_json(plan / "plan.json", {
        "plan_id": "fixed-plan", "summary": {"status": "useful", "native_valid_plans": 1},
        "symbolic_suggestions": {}, "seed_files": ["llm-plan-0001.seed"],
        "planning_seconds": 8.0, "tokens": 200, "calls": 1,
        "planning_confirmed": [{"oracle": "array_access"}],
    })

    class FakeCampaign:
        def __init__(self, run_dir: Path, _bundle: Path, _binary: Path, **kwargs):
            self.run_dir = run_dir
            self.phase_times_s = {"online_search": 5.0, "result_review": 0.1}
            assert kwargs["config"]["prepared_llm"]["plan_id"] == "fixed-plan"
            assert (run_dir / "unified-corpus" / "seeds" / "llm-plan-0001.seed").read_text() == "0.X,INT,2\n"
            assert not (run_dir / "findings" / "confirmed").exists()

        def run(self):
            write_json(self.run_dir / "report.json", {"findings": [
                {"first_candidate_s": 20.0, "oracle": "late_candidate",
                 "goal": {"source": {"file": "target.st", "line": 1}}, "finding_id": "late"}],
                       "online_start_offset_s": 0, "solver_feedback": {}})
            write_json(self.run_dir / "metrics" / "engine-summary.json",
                       {"executions": 4, "scans": 4})

    monkeypatch.setattr(worker, "Campaign", FakeCampaign)
    job = Job(target, "semantist", "full", 0, 5, ["overall"])
    result = worker.run_job(tmp_path, job)
    assert result["status"] == "completed"
    assert result["planning_stage_confirmed"] == 1
    assert result["confirmed"] == 0
    assert result["excluded_outside_online_findings"] == 1
    assert result["llm_tokens"] == 200


def test_semantist_mutation_mode_reaches_campaign_worker(tmp_path, monkeypatch):
    target = next(item for item in load_targets()[0] if item["id"] == "oscat_basic_inc2")
    build = build_path(tmp_path, target["id"])
    (build / "build").mkdir(parents=True)
    (build / "build" / "fuzz_target").write_text("binary")
    (build / "source").mkdir()
    (build / "source" / "target.st").write_text("FUNCTION INC2 END_FUNCTION")
    (build / "template" / "bundle").mkdir(parents=True)
    write_json(build / "template" / "bundle" / "execution-contract.json",
               {"contract_id": "fixed-contract"})
    write_json(build / "build.json", {"target": target,
                                       "build_configuration": artifacts.BUILD_CONFIGURATION,
                                       "input_stats": build_input_stats(target)})

    seen = []

    class FakeCampaign:
        def __init__(self, run_dir: Path, *_args, **_kwargs):
            self.run_dir = run_dir
            self.phase_times_s = {"online_search": 1.0}

        def run(self):
            seen.append(os.environ.get("SEMANTIST_MUTATION_MODE"))
            write_json(self.run_dir / "report.json", {"findings": [], "solver_feedback": {}})
            write_json(self.run_dir / "metrics" / "engine-summary.json", {"executions": 1, "scans": 1})

    monkeypatch.setattr(worker, "Campaign", FakeCampaign)
    job = Job(target, "semantist", "no_llm", 0, 2, ["overall"])
    result = worker.run_job(tmp_path, job, mutation_mode="byte")
    assert result["status"] == "completed"
    assert seen == ["byte"]
    assert os.environ.get("SEMANTIST_MUTATION_MODE") is None


def test_missing_external_adapter_is_not_counted_as_supported(tmp_path):
    target = load_targets()[0][0]
    job = Job(target, "icsquartz", "full", 0, 5, ["overall"])
    result = worker.run_job(tmp_path, job, adapter_config={})
    assert result["status"] == "prerequisite_missing"
    summary = summarize(tmp_path, [job])
    group = summary["groups"][0]
    assert group["support_rate"] == 0
    assert group["target_support_rate"] == 0
    assert group["discovery_rate_supported"] is None
    assert json.loads((tmp_path / "per-target.json").read_text())[0]["supported_runs"] == 0


def test_preparation_costs_are_counted_once_per_target(tmp_path):
    target = load_targets()[0][0]
    build = build_path(tmp_path, target["id"])
    plan = plan_path(tmp_path, target["id"])
    write_json(build / "build.json", {"build_seconds": 12.0})
    write_json(plan / "plan.json", {
        "plan_id": "fixed", "planning_seconds": 8.0,
        "planning_review_seconds": 2.0, "tokens": 200,
        "planning_confirmed": [{"finding_id": "F0001", "oracle": "division_by_zero",
                                "goal": {"source": {"file": "target.st", "line": 1}},
                                "first_candidate_s": 3.0}],
    })
    jobs = [Job(target, "semantist", "full", seed, 300, ["overall"]) for seed in range(5)]
    for job in jobs:
        write_json(worker.result_path(tmp_path, job), {
            "status": "completed", "supported": True, "confirmed": 0,
            "findings": [], "scans": 100, "executions": 50,
            "phase_times_s": {"online_search": 10.0, "result_review": 1.0},
            "feedback": {"enqueued": 1, "executed": 1, "retained": 0},
        })
    group = summarize(tmp_path, jobs)["groups"][0]
    assert group["unique_common_build_seconds"] == 12.0
    assert group["unique_llm_planning_seconds"] == 8.0
    assert group["unique_llm_planning_review_seconds"] == 2.0
    assert group["unique_llm_tokens"] == 200
    assert group["feedback_enqueued"] == 5
    assert group["median_scans_per_second"] == 10
    assert group["planning_stage_confirmed_locations"] == 1
    assert group["runs_with_confirmed"] == 0
    cases = json.loads((tmp_path / "cases.json").read_text())
    assert cases[0]["occurrences"][0]["phase"] == "planning"


def test_changed_build_input_requires_new_root(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ROOT", tmp_path)
    (tmp_path / "Cargo.lock").write_text("lock")
    (tmp_path / "Dockerfile").write_text("FROM test")
    source = tmp_path / "target.st"
    source.write_text("FUNCTION F END_FUNCTION")
    target = {"id": "test", "st_file": "target.st", "function": "F"}
    package = artifacts.build_path(tmp_path, target["id"])
    write_json(package / "build.json", {"target": target,
                                         "build_configuration": artifacts.BUILD_CONFIGURATION,
                                         "input_stats": artifacts.build_input_stats(target)})
    artifacts.validate_build_inputs(package, target)
    source.write_text("FUNCTION F : INT END_FUNCTION")
    try:
        artifacts.validate_build_inputs(package, target)
    except RuntimeError as error:
        assert "new experiment root" in str(error)
    else:
        raise AssertionError("stale build package was accepted")


def test_trial_request_names_its_packages_and_resource_allocation(tmp_path):
    target = load_targets()[0][0]
    job = Job(target, "semantist", "full", 0, 5, ["overall"])
    request = {
        "build_package": str(build_path(tmp_path, target["id"]).relative_to(tmp_path)),
        "common_replay_package": str(build_path(tmp_path, target["id"]).relative_to(tmp_path)),
        "plan_package": str(plan_path(tmp_path, target["id"]).relative_to(tmp_path)),
        "output_dir": str(worker.result_path(tmp_path, job).parent.relative_to(tmp_path)),
        "resources": {"cpu_count": 2, "memory_limit": "8g", "cpu_ids": [0, 1]},
    }
    _validate_trial_request(tmp_path, job, request)
    request["mutation_mode"] = "byte"
    _validate_trial_request(tmp_path, job, request)
    request["mutation_mode"] = "bogus"
    with pytest.raises(ValueError, match="invalid mutation mode"):
        _validate_trial_request(tmp_path, job, request)
    request.pop("mutation_mode")
    request["plan_package"] = None
    with pytest.raises(ValueError, match="inconsistent"):
        _validate_trial_request(tmp_path, job, request)


def test_external_adapter_requires_explicit_equivalence_and_version(tmp_path):
    target = load_targets()[0][0]
    job = Job(target, "icsquartz", "full", 0, 5, ["overall"])
    write_json(tmp_path / "builds" / target["id"] / "icsquartz" / "build-result.json", {
        "status": "supported", "canonical_testcases": "true",
        "oracle_equivalent": True, "version": "",
    })
    result = worker.run_job(tmp_path, job, adapter_config={
        "icsquartz": {"run_command": ["true"]},
    })
    assert result["status"] == "unsupported"
    assert result["supported"] is False


def test_icsquartz_native_sequence_keeps_raw_cycle_boundaries(tmp_path):
    crash = tmp_path / "crash"
    crash.write_bytes(b"last\x00")
    metadata = tmp_path / ".crash.metadata"
    metadata.write_text(json.dumps({"metadata": {"map": {
        "cycle": [1, {"scan_cycles": 2}],
        "sequence": [2, {"raw_sequence_hex": [b"first".hex(), b"last\x00".hex()]}],
    }}}), encoding="utf-8")
    encoded, cycles = baselines._native_sequence(crash, crash.read_bytes())
    assert cycles == 2
    assert encoded == (b"ICSQSEQ1" + struct.pack("<I", 2)
                       + struct.pack("<I", 5) + b"first"
                       + struct.pack("<I", 5) + b"last\x00")
    crash.write_bytes(b"different")
    assert baselines._native_sequence(crash, crash.read_bytes()) == (None, 2)


def test_search_container_isolates_shared_inputs_and_llm_credentials(tmp_path):
    request = Path("requests/trial.json")
    write_json(tmp_path / request, {"job_id": "target/semantist/full/s0/300s",
                                    "output_dir": "runs/target/semantist/full/s0/300s"})
    args = Namespace(engine="podman", memory="8g", root=tmp_path,
                     image="semantist:experiment", llm_env_file=tmp_path / "llm.env")
    search = _container_command(args, "run", cpu_ids=[0, 1],
                                extra=["--request", str(request)])
    planning = _container_command(args, "plan", cpu_ids=[0, 1], extra=[])
    assert "--network=none" in search
    assert any(item.startswith("type=bind,src=") and item.endswith(",dst=/work/SemantiST,readonly")
               for item in search)
    assert f"type=bind,src={tmp_path},dst=/experiments,readonly" in search
    writable = tmp_path / "runs/target/semantist/full/s0"
    assert f"type=bind,src={writable},dst=/experiments/runs/target/semantist/full/s0" in search
    assert "--env-file" not in search
    assert "--network=host" in planning
    assert "--env-file" in planning
