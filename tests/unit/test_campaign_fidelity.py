import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.cli.batch import _snapshot_hit_rate
from fuzzer.pipeline.campaign import Campaign, Candidate, _normalize_ablations
from fuzzer.pipeline.metrics import Metrics
from fuzzer.pipeline.probe import Observation, probe


class _Metrics:
    def event(self, *args, **kwargs):
        pass

    def add(self, *args, **kwargs):
        pass

    def summary(self):
        return {}

    def write_summary(self):
        pass


def test_approximate_contract_follows_candidate_confirmation_and_report(tmp_path: Path) -> None:
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.target_binary = tmp_path / "fuzz_target"
    campaign.config = {"contract_id": "c", "minimize": False}
    campaign.contract = {"support_status": "approximate", "semantic_fidelity": "approximate",
                         "stub_used": ["vendor_time.st"], "model_scope": {"dialect": "vendor"},
                         "execution_status": {"symbolic": "unsupported"}}
    campaign.started = time.monotonic()
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = []
    campaign.metrics = _Metrics()
    campaign.model = SimpleNamespace(runtime_index={7: {"kind": "hazard_violation",
                                                          "modeling": "exact", "hazard": "array_access"}})
    campaign._abi_like = lambda: abi
    campaign._observe_again = lambda data: Observation(0, False, "", hits={7: [0]})
    info = {"oracle": "array_access", "hazard": "array_access", "runtime_id": 7,
            "kind": "hazard_violation", "source": None, "cycle": 0}
    candidate = campaign._register_candidate(b"0.X,INT,1\n", Observation(0, False, "", hits={7: [0]}),
                                             info, provenance_source="solver", query_id="q7",
                                             solver_scope={"imprecise": ["strtod"]})
    finding = campaign.confirm(candidate)
    assert finding is not None
    assert finding["semantic_fidelity"] == "approximate"
    assert finding["stub_used"] == ["vendor_time.st"]
    assert finding["provenance"]["solver_model"] == "approximate"
    assert finding["model_scope"]["imprecise"] == ["strtod"]
    campaign.confirmed.append(finding)
    campaign.symbolic_results = [{"status": "SAT_APPROX", "model_scope": {"imprecise": ["strtod"]}}]
    campaign._write_report()
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["semantic_fidelity"] == "approximate"
    assert report["findings"][0]["confirmation"] == "repeatable"
    assert report["model_result_counts"]["approximate"] == 1
    assert report["confirmed_model_counts"]["approximate"] == 1


def test_confirmation_records_probe_hit_timeout_and_mismatch(tmp_path: Path) -> None:
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.deadline = time.monotonic() + 0.5
    campaign.jobs = 3
    campaign.metrics = _Metrics()
    campaign.model = SimpleNamespace(runtime_index={7: {
        "kind": "hazard_violation", "modeling": "exact", "hazard": "array_access",
    }})
    campaign._abi_like = lambda: abi
    candidate = Candidate(
        "F0001", b"0.X,INT,1\n", Observation(0, False, "", hits={7: [0]}),
        {"oracle": "array_access", "runtime_id": 7, "source": None}, {}, 0.0,
    )
    campaign.candidates = [candidate]
    campaign.confirmed = []
    (tmp_path / "findings" / "candidates").mkdir(parents=True)
    outcomes = iter([Observation(None, True, "probe timeout"),
                     Observation(0, False, ""), Observation(0, False, "", hits={7: [0]})])
    campaign._observe_again = lambda _testcase: next(outcomes)
    campaign._run_confirmation()
    evidence = json.loads((tmp_path / "findings" / "candidates" /
                           "F0001.confirmation.json").read_text(encoding="utf-8"))
    assert evidence["initial_risk_probe_hits"] == [7]
    assert [item["status"] for item in evidence["attempts"]] == [
        "TIMEOUT", "REPLAY_MISMATCH", "MATCH",
    ]
    assert evidence["status"] == "TIMEOUT"
    assert not campaign.confirmed


def test_pointer_width_assumption_prevents_source_vulnerability_claim(tmp_path: Path,
                                                                        monkeypatch) -> None:
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.target_binary = tmp_path / "target"
    campaign.config = {}
    campaign.contract = {"semantic_fidelity": "semantic"}
    campaign.started = time.monotonic()
    campaign.jobs = 1
    campaign.model = SimpleNamespace(runtime_index={7: {
        "kind": "hazard_violation", "modeling": "exact", "hazard": "array_access",
        "source": {"start_line": 2},
    }})
    campaign._abi_like = lambda: abi
    monkeypatch.setattr("fuzzer.pipeline.probe_audit.audit_probe_completeness", lambda _run_dir: {
        "platform_assumptions": [{"kind": "pointer_integer_width", "pointer_bits": 64,
                                  "integer_bits": 32}],
    })
    candidate = Candidate(
        "F0001", b"0.X,INT,1\n", Observation(0, False, "", hits={7: [0]}),
        {"oracle": "array_access", "runtime_id": 7, "source": {"start_line": 2},
         "kind": "hazard_violation"}, {}, 0.0,
    )
    finding = campaign.confirm(candidate, observations=[
        Observation(0, False, "", hits={7: [0]}) for _ in range(3)
    ])
    assert finding is not None
    assert finding["confirmation"] == "repeatable"
    assert finding["semantic_fidelity"] == "approximate"
    assert finding["vulnerability_status"] == "not_established"
    assert finding["platform_evidence"] == "platform_assumption_model_incomplete"


def test_approximate_confirmed_finding_without_solver_scope_is_counted_approximate(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.config = {"contract_id": "c"}
    campaign.contract = {
        "support_status": "approximate",
        "semantic_fidelity": "approximate",
        "stub_used": ["vendor.st"],
    }
    campaign.candidates = []
    campaign.confirmed = [{
        "finding_id": "F0001",
        "oracle": "array_access",
        "goal": {"runtime_id": 7},
        "cause_location": "target.st:1",
        "input_validity": "valid",
        "history_evidence": "repeatable",
        "semantic_fidelity": "approximate",
        "stub_used": ["vendor.st"],
        "model_scope": {},
    }]
    campaign.symbolic_results = []
    campaign.metrics = _Metrics()

    campaign._write_report()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["confirmed_model_counts"] == {
        "faithful": 0,
        "approximate": 1,
        "unknown": 0,
    }


def test_no_symbolic_ablation_is_visible_in_report(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.config = {"contract_id": "c", "ablations": {"no_symbolic": True}}
    campaign.contract = {"support_status": "semantic", "semantic_fidelity": "semantic"}
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = []
    campaign.metrics = _Metrics()

    campaign._write_report()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["execution_status"] == {
        "build": "completed",
        "native": "completed",
        "symbolic": "UNSUPPORTED",
        "symbolic_reason": "disabled_by_no_symbolic_ablation",
    }


def test_symbolic_build_unsupported_is_distinct_from_no_query(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle" / "klee"
    bundle.mkdir(parents=True)
    (bundle / "UNSUPPORTED.json").write_text(json.dumps({
        "status": "unsupported", "reason": "undefined external symbols",
        "detail": "TEST_EXTERNAL"}), encoding="utf-8")
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = tmp_path / "bundle"
    campaign.function = "RENAMED"
    campaign.config = {"contract_id": "c"}
    campaign.contract = {"support_status": "approximate", "semantic_fidelity": "approximate"}
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = []
    campaign.metrics = _Metrics()

    campaign._write_report()
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["execution_status"]["symbolic"] == "UNSUPPORTED"
    assert "TEST_EXTERNAL" in report["execution_status"]["symbolic_reason"]


def test_legacy_v3_switch_aliases_feed_the_single_ablation_map() -> None:
    config = {
        "v3_no_symbolic": True,
        "v3_llm": True,
        "v3_symbolic_start": "replay",
        "ablations": {"no_distance": True},
    }
    normalized = _normalize_ablations(config)
    assert normalized["no_symbolic"] is True
    assert normalized["llm"] is True
    assert normalized["symbolic_start"] == "replay"
    assert normalized["no_distance"] is True
    assert config["ablations"] is normalized


def test_symbolic_error_is_a_failed_campaign_status(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.config = {"contract_id": "c"}
    campaign.contract = {"support_status": "semantic", "semantic_fidelity": "semantic"}
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = [{"status": "ERROR", "model_scope": {}}]
    campaign.metrics = _Metrics()

    campaign._write_report()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["execution_status"]["symbolic"] == "FAILED"
    assert report["status_counts"] == {
        "candidate": 0,
        "confirmed": 0,
        "FAILED": 0,
        "ERROR": 1,
        "UNSUPPORTED": 0,
        "TIMEOUT": 0,
        "REPLAY_MISMATCH": 0,
    }


def test_native_replay_mismatch_is_visible_beside_solver_sat(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.function = "F"
    campaign.config = {"contract_id": "c"}
    campaign.contract = {"support_status": "semantic", "semantic_fidelity": "semantic"}
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = [{
        "query_id": "q1",
        "status": "SAT",
        "native_replay_status": "REPLAY_MISMATCH",
        "model_scope": {},
    }]
    campaign.metrics = _Metrics()

    campaign._write_report()

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["symbolic_queries"][0]["status"] == "SAT"
    assert report["status_counts"]["REPLAY_MISMATCH"] == 1
    assert report["execution_status"]["symbolic"] == "REPLAY_MISMATCH"


def test_default_action_frame_includes_var_in_out_backing_fields() -> None:
    abi = {
        "frame_fields": [{"name": "Trigger", "spec": {"kind": "scalar", "name": "BOOL"}}],
        "shared_fields": [{
            "name": "Counter", "block_kind": "VAR_IN_OUT",
            "spec": {"kind": "scalar", "name": "UINT"},
        }],
    }
    assert Campaign._default_action_frame(abi) == {"Trigger": "0", "Counter": "0"}


def test_planned_prefix_klee_uses_the_native_var_in_out_initial_record() -> None:
    abi = {
        "frame_fields": [{"name": "Trigger", "spec": {"kind": "scalar", "name": "BOOL"}}],
        "shared_fields": [{
            "name": "Counter", "block_kind": "VAR_IN_OUT",
            "spec": {"kind": "scalar", "name": "UINT"},
        }],
    }
    assert Campaign._shared_records_for_frames(
        abi, [{"Trigger": "1", "Counter": "7"}, {"Trigger": "0", "Counter": "8"}]
    ) == "Counter,UINT,7\n"
    assert Campaign._shared_records_for_frames(abi, [{"Trigger": "1"}]) == ""


def test_symbolic_startup_honors_zero_evaluation_budget(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "klee").mkdir(parents=True)
    (tmp_path / "stg").mkdir()
    (bundle / "klee" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (bundle / "abi.json").write_text(json.dumps({"frame_fields": [], "shared_fields": []}), encoding="utf-8")
    (bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": "t1", "symbolic_suitable": True,
        "goal": {"runtime_id": 7, "hazard": "array_access"},
    }]}), encoding="utf-8")
    campaign = object.__new__(Campaign)
    campaign.bundle = bundle
    campaign.run_dir = tmp_path
    campaign.target_binary = tmp_path / "target"
    campaign.config = {"ablations": {}}
    campaign.max_evaluations = 0
    campaign.started = time.monotonic()
    campaign.symbolic_startup_deadline = campaign.started + 10
    campaign.metrics = _Metrics()
    campaign._record_symbolic_result = lambda *args, **kwargs: pytest.fail("zero budget must not query")

    with patch("compiler.symbolic.solver.run_query", side_effect=AssertionError("unexpected query")):
        campaign.run_symbolic_startup()


def test_startup_query_extension_requires_native_guidance_and_reserves_replay_time() -> None:
    assert Campaign._startup_query_timeout(30.0, 2, False) == 5.0
    assert Campaign._startup_query_timeout(30.0, 2, True) == 10.0
    assert Campaign._startup_query_timeout(12.0, 2, True) == 3.0


def test_solver_budget_claim_is_shared_across_campaign_phases() -> None:
    campaign = object.__new__(Campaign)
    campaign.max_evaluations = 2

    assert campaign._claim_solver_budget() is True
    assert campaign._claim_solver_budget() is True
    assert campaign._claim_solver_budget() is False


@pytest.mark.parametrize("trigger", ["task_distance_improved", "native_hazard_reach"])
def test_live_prefix_monitor_submits_cold_prefix_query(tmp_path: Path, trigger: str) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "prefixes" / "frames").mkdir(parents=True)
    (tmp_path / "prefixes" / "klee-queue.jsonl").write_text(
        json.dumps({"prefix_id": "p-00000001", "frames_file": "prefixes/frames/p-00000001.txt",
                    "queue": "klee_suffix", "task_id": "D-live", "trigger": trigger,
                    "task_unresolved": True, "native_execution": 1}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prefixes" / "frames" / "p-00000001.txt").write_text(
        "0.X,INT,1\n0.__DT,TIME,17\n", encoding="utf-8"
    )
    (bundle / "abi.json").write_text(json.dumps({
        "frame_fields": [{"name": "X", "spec": {"kind": "scalar", "name": "INT"}}],
        "shared_fields": [],
    }), encoding="utf-8")
    (bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": "D-live", "goal_class": "defect", "symbolic_suitable": True,
        "goal": {"runtime_id": 7, "hazard": "array_access"},
    }]}), encoding="utf-8")
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = bundle
    campaign.target_binary = tmp_path / "target"
    campaign.config = {"ablations": {}, "scan_period_ns": 99}
    campaign.max_evaluations = 1
    campaign.started = time.monotonic()
    campaign.deadline = campaign.started + 5
    campaign.engine_deadline = campaign.started + 5
    campaign.metrics = _Metrics()
    campaign.source_stats = {"solver": {"attempts": 0, "gains": 0, "weight": 1.0}}
    campaign.symbolic_results = []
    campaign.symbolic_unsupported = []
    campaign.contract = {}
    campaign.model = SimpleNamespace(runtime_index={})
    campaign._abi_like = lambda: TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    stop = __import__("threading").Event()
    observed: dict[str, object] = {}

    def fake_query(**kwargs):
        observed.update(kwargs)
        stop.set()
        return {"status": "UNSUPPORTED", "reason": "test"}

    with patch("compiler.symbolic.solver.run_query", side_effect=fake_query):
        campaign._run_live_prefix_solver(stop)

    assert observed["prefix_frames"] == [{"X": "1"}]
    assert observed["prefix_dt_ns"] == [17]
    assert observed["window"] == 2
    assert campaign.symbolic_results[0]["status"] == "UNSUPPORTED"
    assert "p-00000001" in campaign._live_prefix_ids


def test_live_solver_ignores_unqualified_prefix_index(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    prefixes = tmp_path / "prefixes"
    prefixes.mkdir()
    (prefixes / "prefixes.jsonl").write_text(
        json.dumps({"prefix_id": "p-1", "frames_file": "prefixes/frames/p-1.txt"}) + "\n",
        encoding="utf-8",
    )
    (bundle / "abi.json").write_text(json.dumps({"frame_fields": [], "shared_fields": []}),
                                     encoding="utf-8")
    (bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": "task-a", "goal_class": "defect", "symbolic_suitable": True,
        "goal": {"runtime_id": 7},
    }]}), encoding="utf-8")
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = bundle
    campaign.config = {"ablations": {}}
    campaign.max_evaluations = 1
    campaign.engine_deadline = time.monotonic() + 30
    campaign.metrics = _Metrics()

    class StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.stopped = True

    with patch("compiler.symbolic.solver.run_query", side_effect=AssertionError("unqualified prefix queried")):
        campaign._run_live_prefix_solver(StopAfterWait())


def test_live_prefix_dedup_is_per_task(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    frames = tmp_path / "prefixes" / "frames"
    frames.mkdir(parents=True)
    (frames / "p-1.txt").write_text("0.X,INT,1\n", encoding="utf-8")
    records = [{"prefix_id": "p-1", "frames_file": "prefixes/frames/p-1.txt",
                "queue": "klee_suffix", "task_id": task_id,
                "trigger": "native_hazard_reach", "task_unresolved": True,
                "native_execution": 1}
               for task_id in ("task-a", "task-b")]
    (tmp_path / "prefixes" / "klee-queue.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    (bundle / "abi.json").write_text(json.dumps({
        "frame_fields": [{"name": "X", "spec": {"kind": "scalar", "name": "INT"}}],
        "shared_fields": [],
    }), encoding="utf-8")
    (bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": task_id, "goal_class": "defect", "symbolic_suitable": True,
        "goal": {"runtime_id": runtime_id, "hazard": "array_access"},
    } for runtime_id, task_id in enumerate(("task-a", "task-b"), 1)]}), encoding="utf-8")
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = bundle
    campaign.target_binary = tmp_path / "target"
    campaign.config = {"ablations": {}}
    campaign.max_evaluations = 2
    campaign.engine_deadline = time.monotonic() + 30
    campaign.deadline = campaign.engine_deadline
    campaign.metrics = Metrics(tmp_path)
    campaign.symbolic_results = []
    campaign.symbolic_unsupported = []
    campaign.contract = {}
    campaign.model = SimpleNamespace(runtime_index={})
    campaign._abi_like = lambda: TargetAbi(
        "FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    stop = __import__("threading").Event()
    queried = []

    def fake_query(**kwargs):
        queried.append(kwargs["task_id"])
        if len(queried) == 2:
            stop.set()
        return {"status": "UNSUPPORTED", "reason": "test"}

    with patch("compiler.symbolic.solver.run_query", side_effect=fake_query):
        campaign._run_live_prefix_solver(stop)
    assert queried == ["task-a", "task-b"]


def test_klee_queue_result_preserves_prefix_and_native_replay_requirement(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign._record_klee_queue_result(
        prefix_id="p-17",
        query_id="live-prefix-p-17-7",
        task_id="D-live",
        result={
            "status": "SAT_APPROX",
            "model_scope": {"query_depth": 3, "imprecise": ["strtod"]},
            "candidate_file": "candidates/q.txt",
        },
    )
    record = json.loads(
        (tmp_path / "prefixes" / "klee-results.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["queue"] == "klee_suffix_result"
    assert record["prefix_id"] == "p-17"
    assert record["task_id"] == "D-live"
    assert record["native_replay"] == "required"
    assert record["model_scope"]["query_depth"] == 3


def test_klee_native_replay_outcome_is_recorded_separately(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign._record_klee_native_replay(
        prefix_id="p-17",
        query_id="q-candidate",
        task_id="D-live",
        observation=Observation(0, False, "", wall_s=0.125),
        matched=True,
    )
    campaign._record_klee_native_replay(
        prefix_id="p-17",
        query_id="q-mismatch",
        task_id="D-live",
        observation=Observation(1, False, "", wall_s=0.25),
        matched=False,
    )
    campaign._record_klee_native_replay(
        prefix_id="p-17",
        query_id="q-timeout",
        task_id="D-live",
        observation=Observation(None, True, "deadline", wall_s=1.0),
        matched=False,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "prefixes" / "klee-results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in records] == ["candidate", "REPLAY_MISMATCH", "TIMEOUT"]
    assert all(record["queue"] == "klee_native_replay" for record in records)
    assert records[0]["wall_s"] == 0.125


def test_aborted_engine_invalidates_active_snapshots(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.metrics = _Metrics()
    snapshots = tmp_path / "prefixes" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "node.bin").write_bytes(b"stale")
    (tmp_path / "prefixes" / "snapshots.jsonl").write_text("stale\n", encoding="utf-8")

    campaign._invalidate_snapshots("engine_timeout")

    assert not snapshots.exists()
    assert not (tmp_path / "prefixes" / "snapshots.jsonl").exists()
    marker = json.loads((tmp_path / "prefixes" / "snapshots.invalidated.json").read_text())
    assert marker["status"] == "invalidated"
    assert marker["reason"] == "engine_timeout"


def _engine_campaign(tmp_path: Path) -> Campaign:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = tmp_path / "bundle"
    campaign.bundle.mkdir()
    campaign.target_binary = tmp_path / "fuzz_target"
    campaign.target_binary.write_bytes(b"")
    (tmp_path / "semantist-inprocess").write_bytes(b"")
    campaign.function = "F"
    campaign.seed = 1
    campaign.max_evaluations = 1
    campaign.config = {"ablations": {"no_symbolic": True}}
    campaign.started = time.monotonic()
    campaign.deadline = campaign.started + 10
    campaign.engine_deadline = campaign.started + 5
    campaign.planned_deadline = campaign.started + 5
    campaign.metrics = _Metrics()
    campaign.contract = {"support_status": "semantic", "semantic_fidelity": "semantic"}
    campaign.candidates = []
    campaign.confirmed = []
    campaign.symbolic_results = []
    campaign.run_symbolic_startup = lambda: None
    return campaign


def test_engine_timeout_is_a_failed_campaign(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.symbolic_results = [{"status": "UNSAT", "query_id": "q-start-1"}]
    invalidated: list[str] = []
    campaign._invalidate_snapshots = lambda reason: invalidated.append(reason)

    class TimedOut:
        pid = 999999

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired(["semantist-inprocess"], timeout)

        def wait(self, timeout=None):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: TimedOut())
    with pytest.raises(RuntimeError, match="wall-clock guard"):
        campaign.run_rust_engine()
    assert invalidated == ["engine_timeout"]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["execution_status"]["native"] == "TIMEOUT"
    assert report["symbolic_queries"][0]["query_id"] == "q-start-1"


def test_engine_nonzero_is_a_failed_campaign(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    invalidated: list[str] = []
    campaign._invalidate_snapshots = lambda reason: invalidated.append(reason)

    class Failed:
        pid = 999999
        returncode = 7

        def communicate(self, timeout):
            return b"", b"engine failed"

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Failed())
    with pytest.raises(RuntimeError, match="status 7"):
        campaign.run_rust_engine()
    assert invalidated == ["engine_exit_7"]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["execution_status"]["native"] == "FAILED"


def test_engine_crash_journal_cold_replay_keeps_finding_and_failed_engine_status(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.metrics = Metrics(tmp_path)
    campaign.candidates = []
    campaign.confirmed = []
    campaign.model = type("Model", (), {"runtime_index": {}})()
    campaign._invalidate_snapshots = lambda reason: None
    campaign._probe_with_deadline = lambda testcase, **kwargs: object()
    campaign._abi_like = lambda: object()
    monkeypatch.setattr("fuzzer.pipeline.campaign.parse_testcase", lambda testcase, abi: type("Case", (), {"frames": [1, 2]})())
    monkeypatch.setattr("fuzzer.pipeline.campaign.classify", lambda observation, index: {"oracle": "crash"})
    observed = {}

    def register(testcase, observation, info, **kwargs):
        observed.update(testcase=testcase, provenance=kwargs["provenance_source"], depth=kwargs["depth"])
        campaign.candidates.append(object())

    campaign._register_candidate = register
    campaign._run_confirmation = lambda: campaign.confirmed.append(object())
    campaign._write_report = lambda: observed.update(engine_status=campaign.engine_status)

    class Crashed:
        pid = 999999
        returncode = 134

        def communicate(self, timeout):
            del timeout
            (tmp_path / "metrics" / "engine-pending.txt").write_bytes(b"current testcase")
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Crashed())
    with pytest.raises(RuntimeError, match="status 134"):
        campaign.run_rust_engine()
    assert observed == {"testcase": b"current testcase", "provenance": "engine_crash_recovery",
                        "depth": 2, "engine_status": "FAILED"}


def test_engine_launch_failure_is_visible_and_invalidates_snapshots(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    invalidated: list[str] = []
    campaign._invalidate_snapshots = lambda reason: invalidated.append(reason)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(
        PermissionError("permission denied")
    ))
    with pytest.raises(RuntimeError, match="could not start"):
        campaign.run_rust_engine()
    assert invalidated == ["engine_launch_failed"]


@pytest.mark.parametrize("summary", [
    "{broken\n",
    "{}",
    '{"executions": -1, "scans": 0, "candidates": 0}',
])
def test_engine_invalid_summary_fails_campaign(
    tmp_path: Path, monkeypatch, summary: str,
) -> None:
    campaign = _engine_campaign(tmp_path)
    invalidated: list[str] = []
    campaign._invalidate_snapshots = lambda reason: invalidated.append(reason)
    metrics = tmp_path / "metrics"
    metrics.mkdir()

    class Completed:
        pid = 999999
        returncode = 0

        def communicate(self, timeout):
            del timeout
            (metrics / "summary.json").write_text(summary, encoding="utf-8")
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Completed())
    with pytest.raises(RuntimeError, match="invalid engine summary"):
        campaign.run_rust_engine()
    assert invalidated == ["engine_summary_invalid"]


def test_engine_missing_summary_cannot_reuse_prior_run(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    invalidated: list[str] = []
    campaign._invalidate_snapshots = lambda reason: invalidated.append(reason)
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    old_summary = metrics / "summary.json"
    old_summary.write_text('{"executions": 1, "scans": 1, "candidates": 0}', encoding="utf-8")

    class Completed:
        pid = 999999
        returncode = 0

        def communicate(self, timeout):
            del timeout
            assert not old_summary.exists()
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Completed())
    with pytest.raises(RuntimeError, match="invalid engine summary"):
        campaign.run_rust_engine()
    assert invalidated == ["engine_summary_invalid"]


def test_valid_engine_summary_populates_metrics(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.metrics = Metrics(tmp_path)
    campaign.candidates = []
    campaign.confirmed = []
    campaign.source_stats = {"fuzz": {"attempts": 0, "gains": 0, "weight": 1.0}}
    campaign._run_confirmation = lambda: None
    campaign._write_report = lambda: None
    summary_path = tmp_path / "metrics" / "summary.json"

    class Completed:
        pid = 999999
        returncode = 0

        def communicate(self, timeout):
            del timeout
            summary_path.write_text(json.dumps({
                "executions": 2, "scans": 9, "candidates": 0,
            }), encoding="utf-8")
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Completed())
    result = campaign.run_rust_engine()

    assert result["evaluations"] == 2
    assert json.loads((tmp_path / "metrics" / "engine-summary.json").read_text())["executions"] == 2
    metrics = campaign.metrics.summary()
    assert metrics["executions"] == metrics["engine_executions"] == 2
    assert metrics["scans"] == metrics["engine_scans"] == 9


def test_engine_candidates_get_cold_replay_without_post_engine_solver(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.metrics = Metrics(tmp_path)
    campaign.candidates = []
    campaign.confirmed = []
    campaign.native_replay_results = []
    campaign.model = type("Model", (), {"runtime_index": {}})()
    campaign.source_stats = {"fuzz": {"attempts": 0, "gains": 0, "weight": 1.0}}
    candidate_dir = tmp_path / "findings" / "candidates"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate-000001.txt").write_bytes(b"engine testcase")
    order = []
    campaign._probe_with_deadline = lambda testcase, **kwargs: order.append("probe") or object()
    campaign._abi_like = lambda: object()
    monkeypatch.setattr("fuzzer.pipeline.campaign.parse_testcase", lambda testcase, abi: type("Case", (), {"frames": [1]})())
    monkeypatch.setattr("fuzzer.pipeline.campaign.classify", lambda observation, index: {"oracle": "crash"})

    def register(testcase, observation, info, **kwargs):
        order.append("register")
        campaign.candidates.append(object())

    campaign._register_candidate = register
    campaign._run_confirmation = lambda: order.append("confirm")
    campaign._write_report = lambda: None

    class Completed:
        pid = 999999
        returncode = 0

        def communicate(self, timeout):
            del timeout
            (tmp_path / "metrics" / "summary.json").write_text(
                '{"executions": 1, "scans": 1, "candidates": 1}', encoding="utf-8"
            )
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Completed())
    campaign.run_rust_engine()
    assert order == ["probe", "register", "confirm", "confirm"]
    assert campaign.native_replay_results[0]["status"] == "candidate"


def test_engine_loop_budget_leaves_time_for_normal_summary_exit(tmp_path: Path, monkeypatch) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.engine_deadline = time.monotonic() + 10
    campaign.metrics = Metrics(tmp_path)
    campaign.candidates = []
    campaign.confirmed = []
    campaign.source_stats = {"fuzz": {"attempts": 0, "gains": 0, "weight": 1.0}}
    campaign._run_confirmation = lambda: None
    campaign._write_report = lambda: None
    observed = {}

    class Completed:
        pid = 999999
        returncode = 0

        def communicate(self, timeout):
            observed["guard_s"] = timeout
            summary = tmp_path / "metrics" / "summary.json"
            summary.write_text('{"executions": 1, "scans": 1, "candidates": 0}', encoding="utf-8")
            return "", ""

    def start(command, **_kwargs):
        observed["engine_ms"] = int(command[command.index("--v3-time-budget-ms") + 1])
        observed["asan_options"] = _kwargs["env"]["ASAN_OPTIONS"]
        return Completed()

    monkeypatch.setattr(subprocess, "Popen", start)
    assert campaign.run_rust_engine()["evaluations"] == 1
    assert "halt_on_error=0" in observed["asan_options"]
    assert "abort_on_error=0" in observed["asan_options"]
    assert observed["engine_ms"] / 1000 + 0.25 < observed["guard_s"]


def test_full_hybrid_duration_uses_campaign_remainder_without_iteration_cap(
    tmp_path: Path, monkeypatch,
) -> None:
    campaign = _engine_campaign(tmp_path)
    campaign.time_budget_s = 600
    campaign.engine_deadline = time.monotonic() + 10
    campaign.config["full_hybrid_duration"] = True
    campaign.metrics = Metrics(tmp_path)
    campaign.source_stats = {"fuzz": {"attempts": 0, "gains": 0, "weight": 1.0}}
    campaign._run_confirmation = lambda: None
    campaign._write_report = lambda: None
    observed = {}

    class Completed:
        returncode = 0

        def communicate(self, timeout):
            observed["guard_s"] = timeout
            (tmp_path / "metrics" / "summary.json").write_text(
                '{"executions": 1, "scans": 1, "candidates": 0}', encoding="utf-8"
            )
            return "", ""

    def start(command, **_kwargs):
        observed["command"] = command
        return Completed()

    monkeypatch.setattr(subprocess, "Popen", start)
    campaign.run_rust_engine()
    command = observed["command"]
    engine_s = int(command[command.index("--v3-time-budget-ms") + 1]) / 1000
    assert "--v3-full-duration" in command
    assert "--fuzz-iterations" not in command
    assert 0 < engine_s + 0.25 < observed["guard_s"] < 11


def test_native_probe_launch_failure_is_a_failed_observation(tmp_path: Path) -> None:
    observation = probe(
        tmp_path / "missing-target",
        b"0.X,INT,1\n",
        work_dir=tmp_path / "probe",
    )
    assert observation.returncode == 127
    assert observation.timed_out is False
    assert "could not start" in observation.stderr


def test_solver_finding_depth_uses_actual_query_scope() -> None:
    campaign = object.__new__(Campaign)
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    campaign._abi_like = lambda: abi
    testcase = b"0.X,INT,1\n"
    assert campaign._query_result_depth({"model_scope": {"query_depth": 113}}, testcase, 2) == 113
    assert campaign._query_result_depth({}, testcase, 2) == 1


def test_solver_sat_without_replayable_candidate_is_mismatch(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    missing, testcase = campaign._solver_candidate({"status": "SAT"})
    assert missing["status"] == "REPLAY_MISMATCH"
    assert testcase is None
    candidate = tmp_path / "candidate.txt"
    candidate.write_bytes(b"0.X,INT,1\n")
    result, testcase = campaign._solver_candidate({"status": "SAT", "candidate_file": str(candidate)})
    assert result["status"] == "SAT"
    assert testcase == b"0.X,INT,1\n"


def test_source_weight_drops_after_stagnation_and_recovers_on_gain(tmp_path: Path) -> None:
    campaign = object.__new__(Campaign)
    campaign.metrics = _Metrics()
    campaign.source_stats = {"llm": {"attempts": 0, "gains": 0, "weight": 1.0}}

    campaign._record_source_result("llm", gained=False)
    low = campaign.source_stats["llm"]["weight"]
    campaign._record_source_result("llm", gained=True)

    assert low < 1.0
    assert campaign.source_stats["llm"]["attempts"] == 2
    assert campaign.source_stats["llm"]["gains"] == 1
    assert campaign.source_stats["llm"]["weight"] > low


def test_batch_snapshot_rate_requires_real_engine_attempts() -> None:
    assert _snapshot_hit_rate({"cache_hits": 3, "snapshot_attempts": 5}) == 0.6
    assert _snapshot_hit_rate({"cache_hits": 3}) is None
    assert _snapshot_hit_rate({"cache_hits": 3, "snapshot_attempts": 0}) is None


def test_native_probe_is_capped_by_campaign_deadline(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_probe(*args, **kwargs):
        captured.update(kwargs)
        return Observation(0, False, "")

    monkeypatch.setattr("fuzzer.pipeline.campaign.probe", fake_probe)
    campaign = object.__new__(Campaign)
    campaign.target_binary = tmp_path / "fuzz_target"
    campaign.run_dir = tmp_path
    campaign.deadline = time.monotonic() + 0.25

    result = campaign._probe_with_deadline(b"seed", timeout_s=5.0)
    assert not result.timed_out
    assert 0 < captured["timeout_s"] <= 0.25

    campaign.deadline = time.monotonic() - 1.0
    expired = campaign._probe_with_deadline(b"seed", timeout_s=5.0)
    assert expired.timed_out and "deadline" in expired.stderr
