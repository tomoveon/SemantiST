import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.corpus.frames import parse_testcase
from fuzzer.pipeline.campaign import (Campaign, Candidate, solver_feedback_cutoff,
                                solver_prefix_priority, solver_prefix_skip_reason)
from fuzzer.pipeline.goal_tasks import _conditions, build_goal_tasks, cycle_effects
from fuzzer.pipeline.metrics import Metrics
from fuzzer.pipeline.probe import Observation, classify


def test_negated_branch_obligations_keep_required_polarity():
    def field(name):
        return {"kind": {"kind": "member_access", "member": name},
                "type_name": "BOOL" if name in ("init", "RST") else "UINT",
                "operands": []}

    def negated(node):
        return {"kind": {"kind": "unary", "operator": "NOT"}, "operands": [node]}

    def either(left, right):
        return {"kind": {"kind": "binary", "operator": "OR"},
                "operands": [left, right]}

    zero = {"kind": {"kind": "literal", "value": {"kind": "integer", "value": "0"}}}
    n_is_zero = {"kind": {"kind": "binary", "operator": "="},
                 "operands": [field("N"), zero]}
    expression = negated(either(negated(field("init")), either(field("RST"), n_is_zero)))
    assert _conditions(expression) == [
        {"lhs": {"field": "init"}, "op": "=", "rhs": {"const": 1}},
        {"lhs": {"field": "RST"}, "op": "=", "rhs": {"const": 0}},
        {"lhs": {"field": "N"}, "op": "<>", "rhs": {"const": 0}},
    ]


@pytest.mark.parametrize("name,width,fields", [
    ("CONVERT_BUFFER", 2, ["INDEX", "PAYLOAD"]),
    ("RENAMED_TRANSFORM", 4, ["PAYLOAD", "INDEX"]),
])
def test_pure_function_uses_one_frame_across_layout_variants(name, width, fields):
    abi = {"target": {"name": name, "kind": "FUNCTION"},
           "frame_fields": [{"name": field, "block_kind": "VAR_INPUT",
                             "spec": {"kind": "scalar", "name": "INT", "size": width}}
                            for field in fields],
           "shared_fields": [], "state_fields": []}
    stg = {"pous": [{"pou": {"name": name, "kind": "function", "stateful": False},
                      "environment": {"symbols": [], "calls": []}}]}
    plan = {"tasks": [{"task_id": "renamed-risk", "terminal_kind": "hazard_violation",
                       "terminal_hazard": "division", "terminal_runtime_id": 4,
                       "candidate_paths": [], "control_obligations": []}]}
    result = build_goal_tasks(plan, abi, {"regions": []}, stg_model=stg)
    assert result["single_cycle"]
    assert result["cycle_effects"]["reasons"] == []
    assert all(len(seed) == 1 for seed in result["tasks"][0]["seed_prefixes"])


@pytest.mark.parametrize("change", ["global", "inout", "pointer", "clock",
                                    "missing_call", "external_unknown"])
def test_cross_frame_effects_distinguish_known_from_unknown(change):
    name = "STATEFUL_VARIANT"
    abi = {"target": {"name": name, "kind": "FUNCTION"}, "frame_fields": [],
           "shared_fields": [], "state_fields": []}
    pou = {"pou": {"name": name, "kind": "function", "stateful": False},
           "environment": {"symbols": [], "calls": []}}
    if change == "global":
        pou["environment"]["symbols"] = [{"name": "RENAMED_SHARED", "role": "global"}]
    elif change == "inout":
        abi["shared_fields"] = [{"name": "M", "block_kind": "VAR_IN_OUT",
                                 "spec": {"kind": "scalar", "name": "DINT", "size": 4}}]
    elif change == "pointer":
        abi["frame_fields"] = [{"name": "P", "block_kind": "VAR_INPUT",
                                "spec": {"kind": "pointer", "name": "REF_TO BYTE"}}]
    elif change == "clock":
        pou["environment"]["calls"] = [{"callee": "RENAMED_CLOCK", "kind": "external",
                                         "external": True, "effect_domains": ["clock"],
                                         "writes": []}]
    elif change == "external_unknown":
        pou["environment"]["calls"] = [{"callee": "UNMODELED_LIBRARY", "kind": "external",
                                         "external": True, "writes": []}]
    else:
        pou["environment"]["calls"] = [{"callee": "UNKNOWN_HELPER", "kind": "function",
                                         "external": False, "writes": []}]
    result = cycle_effects(abi, {"pous": [pou]})
    assert result["mode"] == ("unknown" if change in {"pointer", "missing_call",
                                                       "external_unknown"}
                              else "multi_cycle")


def test_unknown_call_starts_with_one_frame_and_keeps_bounded_fallback():
    name = "RENAMED_FUNCTION"
    abi = {"target": {"name": name, "kind": "FUNCTION"},
           "frame_fields": [{"name": "VALUE", "block_kind": "VAR_INPUT",
                             "spec": {"kind": "scalar", "name": "DINT", "size": 4}}],
           "shared_fields": [], "state_fields": []}
    stg = {"pous": [{"pou": {"name": name, "kind": "function"},
                     "environment": {"symbols": [], "calls": [
                         {"callee": "RENAMED_HELPER", "kind": "function",
                          "external": False, "writes": []}]}}]}
    plan = {"tasks": [{"task_id": "risk", "terminal_kind": "hazard_violation",
                       "terminal_hazard": "division", "terminal_runtime_id": 1,
                       "candidate_paths": [], "control_obligations": []}]}
    result = build_goal_tasks(plan, abi, {"regions": []}, stg_model=stg)
    assert result["cycle_effects"]["mode"] == "unknown"
    assert result["unknown_cycle_effects"]
    assert not result["single_cycle"]
    assert all(len(seed) == 1 for seed in result["tasks"][0]["seed_prefixes"])


def test_unresolved_callee_formal_write_does_not_prove_pointer_alias():
    name = "RENAMED_COPY"
    abi = {"target": {"name": name, "kind": "FUNCTION"},
           "frame_fields": [{"name": "DESTINATION", "block_kind": "VAR_INPUT",
                             "spec": {"kind": "pointer", "name": "REF_TO BYTE"}}],
           "shared_fields": [], "state_fields": []}
    stg = {"pous": [{"pou": {"name": name, "kind": "function"},
                     "environment": {"symbols": [], "calls": [
                         {"callee": "COPY_HELPER", "kind": "function",
                          "external": False, "writes": [f"{name}.DESTINATION"]}]}}]}
    effects = cycle_effects(abi, stg)
    assert effects["mode"] == "unknown"
    assert "pointer_write" not in effects["reasons"]


@pytest.mark.parametrize("name,pointer", [("RENAMED_STORE", "DEST"),
                                           ("ANOTHER_STORE", "BUFFER")])
def test_stg_assign_source_proves_pointer_store(tmp_path, name, pointer):
    statement = f"{pointer}^ := 7;".encode()
    source = tmp_path / "renamed.st"
    source.write_bytes(statement)
    abi = {"target": {"name": name, "kind": "FUNCTION"},
           "frame_fields": [{"name": pointer, "block_kind": "VAR_INPUT",
                             "spec": {"kind": "pointer", "name": "REF_TO BYTE"}}],
           "shared_fields": [], "state_fields": []}
    stg = {"pous": [{"pou": {"name": name, "kind": "function"},
                     "environment": {"symbols": [], "calls": []},
                     "graph": {"edges": [{"transfer": {"operations": [{
                         "kind": "assign", "target_symbol": "BYTE",
                         "source": {"file": str(source), "start_offset": 0,
                                    "end_offset": len(statement)}}]}}]}}]}
    assert cycle_effects(abi, stg)["mode"] == "multi_cycle"


def test_function_block_defaults_to_multicycle():
    abi = {"target": {"name": "RENAMED_FB", "kind": "FUNCTION_BLOCK"},
           "frame_fields": [], "shared_fields": [], "state_fields": []}
    stg = {"pous": [{"pou": {"name": "RENAMED_FB", "kind": "function_block",
                             "stateful": False},
                     "environment": {"symbols": [], "calls": []}}]}
    effects = cycle_effects(abi, stg)
    assert effects["mode"] == "multi_cycle"
    assert "function_block_default" in effects["reasons"]


@pytest.mark.parametrize("count,order", [(5, ["HISTORY", "COUNT"]),
                                          (11, ["COUNT", "HISTORY"])])
def test_state_layout_and_array_boundary_variants_keep_history(count, order):
    fields = {
        "HISTORY": {"name": "HISTORY", "block_kind": "VAR",
                    "spec": {"kind": "array", "count": count,
                             "element": {"kind": "scalar", "name": "DINT", "size": 4}}},
        "COUNT": {"name": "COUNT", "block_kind": "VAR",
                  "spec": {"kind": "scalar", "name": "DINT", "size": 4}},
    }
    abi = {"target": {"name": "RENAMED_DELAY", "kind": "FUNCTION_BLOCK"},
           "frame_fields": [], "shared_fields": [], "state_fields": [fields[name] for name in order]}
    stg = {"pous": [{"pou": {"name": "RENAMED_DELAY", "kind": "function_block",
                             "stateful": True},
                      "environment": {"symbols": [{"name": name, "role": "persistent_state"}
                                                  for name in order], "calls": []}}]}
    assert cycle_effects(abi, stg)["mode"] == "multi_cycle"


def test_goal_task_matches_reach_probe_by_node_and_hazard():
    abi = {"target": {"name": "RENAMED", "kind": "FUNCTION_BLOCK"},
           "frame_fields": [], "shared_fields": [], "state_fields": []}
    plan = {"tasks": [{"task_id": "target", "terminal_kind": "hazard_violation",
                       "terminal_hazard": "array_access", "terminal_runtime_id": 8,
                       "candidate_paths": [], "control_obligations": []}]}
    runtime = {
        8: {"node_id": "node-a", "kind": "hazard_violation", "hazard": "array_access",
            "modeling": "exact"},
        9: {"node_id": "node-a", "kind": "hazard_reach", "hazard": "array_access"},
        10: {"node_id": "node-b", "kind": "hazard_reach", "hazard": "array_access"},
        11: {"node_id": "node-a", "kind": "hazard_reach", "hazard": "division"},
    }
    result = build_goal_tasks(plan, abi, {"regions": []}, runtime_index=runtime)
    assert result["tasks"][0]["reach_runtime_ids"] == [9]


def _campaign(tmp_path: Path) -> Campaign:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.bundle = tmp_path / "bundle"
    campaign.bundle.mkdir()
    campaign.metrics = Metrics(tmp_path)
    campaign.config = {"ablations": {}}
    campaign.model = SimpleNamespace(runtime_index={})
    campaign.max_evaluations = 2
    campaign._abi_like = lambda: TargetAbi(
        "FUNCTION", "RENAMED", [Param("N", scalar_spec("INT"), "VAR_INPUT")])
    (campaign.bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": "task", "state_slice": [], "candidate_paths": [{"conditions": [
            {"lhs": {"field": "N"}, "op": "<", "rhs": {"const": 10}}]}],
    }]}))
    return campaign


def test_array_boundary_distance_keeps_pre_access_frontier_one_step_away(tmp_path):
    campaign = _campaign(tmp_path)
    (campaign.bundle / "goal-tasks.json").write_text(json.dumps({"tasks": [{
        "task_id": "array", "goal": {"runtime_id": 2},
        "array_boundary": {"field": "STATE", "lower": 0, "upper": 63,
                           "lower_guarded": True},
        "candidate_paths": [{"conditions": [{"lhs": {"field": "STATE"},
                                              "op": ">=", "rhs": {"const": 0}}]}],
    }]}))
    case = parse_testcase(b"0.N,INT,1\n", campaign._abi_like())
    assert campaign._solver_task_distance("array", case, Observation(0, False, "", states=[{"STATE": -1}])) == 65
    assert campaign._solver_task_distance("array", case, Observation(0, False, "", states=[{"STATE": 64}])) == 1
    assert campaign._solver_task_distance("array", case, Observation(0, False, "", hits={2: [0]},
                                                                      states=[{"STATE": 64}])) == 0


def test_solver_queue_defers_distance_feedback_to_native_engine(tmp_path):
    campaign = _campaign(tmp_path)
    assert campaign._enqueue_solver_seed(b"0.N,INT,20\n", query_id="q0", task_id="task")
    assert campaign._enqueue_solver_seed(b"0.N,INT,21\n", query_id="q1", task_id="task")
    assert campaign._enqueue_solver_seed(b"0.N,INT,9\n", query_id="q2", task_id="task")
    assert len(list((tmp_path / "solver" / "reinject").glob("*.seed"))) == 3
    campaign._engine_stop_event = threading.Event()
    campaign._engine_stop_event.set()
    assert not campaign._enqueue_solver_seed(b"0.N,INT,8\n", query_id="q3", task_id="task")


def test_solver_budget_shared_and_prefix_requires_native_task_progress(tmp_path):
    campaign = _campaign(tmp_path)
    campaign._solver_task_limits = {"task": 1}
    assert campaign._claim_solver_budget("task")
    assert not campaign._claim_solver_budget("task")
    assert campaign._claim_solver_budget("other")
    assert not campaign._claim_solver_budget("other")
    task = {"task_id": "task", "goal_class": "defect", "symbolic_suitable": True}
    record = {"task_id": "task", "queue": "klee_suffix", "trigger": "task_distance_improved",
              "task_unresolved": True, "native_execution": 12}
    assert solver_prefix_skip_reason(record, task) is None
    assert solver_prefix_skip_reason({**record, "trigger": "native_hazard_reach"}, task) is None
    assert solver_prefix_skip_reason({**record, "trigger": "state_frontier"}, task) is None
    assert solver_prefix_skip_reason({**record, "trigger": "task_stagnation"}, task) is None
    assert solver_prefix_skip_reason({**record, "task_id": "other"}, task) == "unrelated_task"
    assert solver_prefix_skip_reason({**record, "native_execution": None}, task) == "missing_native_task_progress"
    assert solver_prefix_skip_reason(record, task, single_cycle=True) == "single_cycle_uses_cold_queries"


def test_solver_prefix_priority_prefers_reached_frontiers_and_stagnation():
    ordinary = {"trigger": "task_distance_improved", "distance": 5.0,
                "consecutive_no_progress": 2}
    stalled = {**ordinary, "consecutive_no_progress": 9}
    frontier = {**ordinary, "distance": 0.0}
    state_frontier = {**ordinary, "trigger": "state_frontier"}
    reach = {**ordinary, "trigger": "native_hazard_reach", "distance": None}
    stagnation = {**stalled, "trigger": "task_stagnation"}
    assert sorted([ordinary, stalled, frontier, state_frontier, reach, stagnation],
                  key=solver_prefix_priority, reverse=True) == [
                      reach, state_frontier, frontier, stalled, ordinary, stagnation]


def test_feedback_cutoff_uses_recent_query_and_replay_cost_without_losing_minimum():
    assert solver_feedback_cutoff(100.0, 15.0, []) == 85.0
    assert solver_feedback_cutoff(100.0, 15.0, [4.0, 10.0]) == 78.0
    assert solver_feedback_cutoff(100.0, 15.0, [100.0] + [1.0] * 5) == 85.0


def test_native_goal_reached_after_queueing_blocks_stale_query(tmp_path):
    campaign = _campaign(tmp_path)
    assert not campaign._native_task_reached("task")
    campaign.metrics.event("goal_reached", task_id="task", source="fuzz")
    assert campaign._native_task_reached("task")
    assert not campaign._native_task_reached("unrelated")


def test_engine_candidate_replay_recovers_exact_semantic_hit(tmp_path):
    campaign = _campaign(tmp_path)
    campaign.model.runtime_index = {
        7: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact",
            "source": {"file": "target.st", "start_line": 3}},
        12: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact",
             "source": {"file": "target.st", "start_line": 4}},
    }
    plain = Observation(returncode=0, timed_out=False, stderr="")
    traced = Observation(returncode=0, timed_out=False, stderr="", hits={7: [0], 12: [0]})
    modes = []

    def observe(_testcase, *, semantic=False):
        modes.append(semantic)
        return traced if semantic else plain

    campaign._observe_again = observe
    observation, info = campaign._replay_engine_candidate(
        b"0.N,INT,1\n", expected_runtime_id=12)
    assert modes == [False, True]
    assert observation is traced
    assert info["kind"] == "hazard_violation"
    assert info["runtime_id"] == 12


def test_numeric_boundary_observation_does_not_mask_declared_oracle():
    index = {
        2: {"kind": "hazard_violation", "hazard": "arithmetic_boundary", "modeling": "exact"},
        7: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact"},
        12: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact"},
    }
    observation = Observation(returncode=0, timed_out=False, stderr="", hits={2: [0]})
    assert classify(observation, index) is None
    assert classify(observation, index, expected_runtime_id=2) is None
    observation.hits[7] = [1]
    assert classify(observation, index)["runtime_id"] == 7
    observation.hits[12] = [1]
    assert classify(observation, index, expected_runtime_id=12)["runtime_id"] == 12
    assert classify(observation, index, expected_runtime_id=99) is None
    budget_abort = Observation(returncode=1, timed_out=False,
                               stderr="semantist-scan-budget-exceeded", scan_budget=True)
    assert classify(budget_abort, index, expected_runtime_id=99)["kind"] == "scan_budget"


def test_numeric_boundary_task_is_exploration_not_defect():
    abi = {"target": {"name": "NUMERIC", "kind": "FUNCTION_BLOCK"},
           "frame_fields": [], "shared_fields": [], "state_fields": []}
    plan = {"tasks": [{"task_id": "boundary", "terminal_kind": "hazard_violation",
                       "terminal_hazard": "arithmetic_boundary", "terminal_runtime_id": 3,
                       "candidate_paths": [], "control_obligations": []}]}
    result = build_goal_tasks(plan, abi, {"regions": []})
    assert result["tasks"][0]["goal_class"] == "exploration"


def test_exact_semantic_confirmation_uses_traced_cold_replay(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    campaign.model.runtime_index = {
        7: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact",
            "source": {"file": "target.st", "start_line": 3}},
        12: {"kind": "hazard_violation", "hazard": "division", "modeling": "exact",
             "source": {"file": "target.st", "start_line": 4}},
    }
    traced = Observation(returncode=0, timed_out=False, stderr="", hits={7: [0], 12: [0]})
    candidate = Candidate("finding-1", b"0.N,INT,1\n", traced,
                          {"kind": "hazard_violation", "oracle": "division_by_zero",
                           "runtime_id": 12, "source": {"file": "target.st", "start_line": 4}},
                          {}, 1.0)
    campaign.candidates = [candidate]
    campaign.confirmed = []
    campaign.jobs = 1
    campaign.started = time.monotonic()
    campaign.contract = {"semantic_fidelity": "exact"}
    campaign.function = "RENAMED"
    campaign.target_binary = tmp_path / "fuzz_target"
    (tmp_path / "findings/candidates").mkdir(parents=True)
    modes = []

    def observe(_testcase, *, semantic=False):
        modes.append(semantic)
        return traced if semantic else Observation(returncode=0, timed_out=False, stderr="")

    campaign._observe_again = observe
    campaign._write_repro = lambda *_args: None
    monkeypatch.setattr("fuzzer.pipeline.probe_audit.audit_probe_completeness",
                        lambda _path: {})
    campaign._run_confirmation()
    assert modes == [True]
    assert len(campaign.confirmed) == 1
    assert campaign.confirmed[0]["goal"]["runtime_id"] == 12
