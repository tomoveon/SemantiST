import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.llm.plan import PlanRejected, validate_plan_batch
from fuzzer.pipeline.metrics import Metrics
from fuzzer.pipeline.plan_batches import run_plan_batches
from fuzzer.pipeline.probe import Observation


ABI = {"target": {"name": "RENAMED_CALCULATOR", "kind": "FUNCTION"},
       "frame_fields": [{"name": "VALUE", "block_kind": "VAR_INPUT",
                         "spec": {"kind": "scalar", "name": "DINT", "size": 4,
                                  "c_type": "int32_t"}}],
       "shared_fields": []}
TASK = {"task_id": "risk-1", "goal_class": "defect",
        "goal": {"stable_id": "stg-goal", "runtime_id": 1},
        "candidate_paths": [], "abstraction": [], "state_slice": [],
        "symbolic_suitable": True}


def _plan(value, plan_id="plan"):
    return {"plan_id": plan_id, "task_id": "risk-1", "contract_id": "contract",
            "evidence_refs": ["stg:stg-goal"], "suggested_symbolic_fields": [],
            "actions": [{"op": "set", "inputs": {"VALUE": value}}]}


def test_batch_rejects_invalid_peer_and_duplicate_actions():
    tasks = {"risk-1": {"evidence_refs": ["stg:stg-goal"],
                        "abstraction_fields": [], "reachable_prefix_ids": []}}
    seen = set()
    valid, rejected = validate_plan_batch(
        {"contract_id": "contract", "plans": [_plan(7), _plan(2**40, "overflow")]},
        abi=ABI, contract_id="contract", tasks=tasks, seen_actions=seen, max_plans=2)
    assert [plan["plan_id"] for plan in valid] == ["plan"]
    assert "type domain" in rejected[0]["reason"]
    valid, rejected = validate_plan_batch(
        {"contract_id": "contract", "plans": [_plan(7, "renamed")]},
        abi=ABI, contract_id="contract", tasks=tasks, seen_actions=seen, max_plans=2)
    assert valid == []
    assert "duplicate" in rejected[0]["reason"]
    with pytest.raises(PlanRejected, match="exactly"):
        validate_plan_batch({"plans": []}, abi=ABI, contract_id="contract",
                            tasks=tasks, seen_actions=seen, max_plans=2)


def _campaign(tmp_path: Path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "abi.json").write_text(json.dumps(ABI), encoding="utf-8")
    (bundle / "goal-tasks.json").write_text(json.dumps({
        "cycle_effects": {"mode": "single_cycle"}, "tasks": [TASK]}), encoding="utf-8")
    campaign = SimpleNamespace(
        run_dir=tmp_path, bundle=bundle, metrics=Metrics(tmp_path),
        config={"ablations": {"llm": True}, "contract_id": "contract",
                "llm_max_calls": 4, "llm_plan_rounds": 3,
                "llm_plan_batch_size": 2, "llm_plan_token_budget": 100},
        deadline=time.monotonic() + 5, startup_deadline=time.monotonic() + 5,
        model=SimpleNamespace(runtime_index={}, build=lambda frames:
                              f"0.VALUE,DINT,{frames[-1]['VALUE']}\n".encode()),
        _source_excerpt=lambda task: {"text": "VALUE := VALUE + 1;"},
        _llm_controllable_fields=lambda abi, allowed: ABI["frame_fields"],
        _default_action_frame=lambda abi: {"VALUE": "0"},
        _abi_like=lambda: TargetAbi("FUNCTION", "RENAMED_CALCULATOR",
                                   [Param("VALUE", scalar_spec("DINT"), "VAR_INPUT")]),
        _probe_with_deadline=lambda testcase, **kwargs:
            Observation(0, False, "", hits={int(testcase.decode().split(",")[-1]): [0]}),
        _solver_state_key=lambda task_id, observation: None,
        _solver_task_distance=lambda task_id, case, observation: None,
        _register_candidate=lambda *args, **kwargs: None,
    )
    return campaign


def test_revisions_receive_original_rejection_and_useful_seed_enters_corpus(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    prompts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            prompts.append(json.loads(messages[1]["content"]))
            plan = _plan(2**40 if len(prompts) == 1 else 7,
                         f"round-{len(prompts)}")
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                                                        "plans": [plan]}),
                                   usage={"total_tokens": 4}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.audit_probe_completeness",
                        lambda _: {"status": "INCOMPLETE", "risk_count": 2,
                                   "incomplete_count": 1, "platform_assumptions": [],
                                   "risks": [{"runtime_ids": [1],
                                              "status": "MISSING_IR_PROBE"}]})
    run_plan_batches(campaign)
    assert len(prompts) >= 2
    assert prompts[0]["tasks"][0]["probe_status"] == ["MISSING_IR_PROBE"]
    assert prompts[0]["probe_audit"]["incomplete_count"] == 1
    assert {form["op"] for form in prompts[0]["action_forms"]} == {
        "set", "pulse", "hold", "wait", "repeat"}
    feedback = prompts[1]["previous_feedback"][0]
    assert feedback["plan"]["actions"][0]["inputs"]["VALUE"] == 2**40
    assert "type domain" in feedback["reason"]
    seeds = list((tmp_path / "unified-corpus" / "seeds").glob("llm-plan-*.seed"))
    assert [seed.read_bytes() for seed in seeds] == [b"0.VALUE,DINT,7\n"]
    events = [json.loads(line) for line in
              (tmp_path / "metrics" / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "llm_plan_seed" for event in events)


def test_all_revisions_fail_explicitly_and_strict_mode_raises(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    campaign.config["llm_max_calls"] = 3

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                "plans": [_plan(2**40)]}), usage={"total_tokens": 1}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    run_plan_batches(campaign)
    events = [json.loads(line) for line in
              (tmp_path / "metrics" / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "llm_planning_failed" for event in events)
    campaign.config["llm_strict_planning"] = True
    with pytest.raises(RuntimeError, match="no useful"):
        run_plan_batches(campaign)


def test_valid_plan_without_native_gain_is_not_seeded(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    campaign.config["llm_max_calls"] = 1
    campaign._solver_task_distance = lambda task_id, case, observation: 5.0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                "plans": [_plan(0)]}), usage={"total_tokens": 1}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    run_plan_batches(campaign)
    assert not (tmp_path / "unified-corpus" / "seeds").exists()
    assert campaign._solver_seed_best_distance == {"risk-1": 5.0}
    events = [json.loads(line) for line in
              (tmp_path / "metrics" / "events.jsonl").read_text().splitlines()]
    rounds = [event for event in events if event["kind"] == "llm_plan_round"]
    assert rounds[0]["accepted"] == 1
    assert rounds[0]["feedback"][0]["reason"] == "no_new_probe_state_or_distance"


def test_next_round_receives_native_replay_coverage_details(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    prompts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            prompts.append(json.loads(messages[1]["content"]))
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                "plans": [_plan(0 if len(prompts) == 1 else 7,
                                f"round-{len(prompts)}")]}),
                usage={"total_tokens": 1}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    run_plan_batches(campaign)
    assert prompts[1]["native_baseline"]["runtime_hits"] == [0]
    replay = prompts[1]["previous_feedback"][0]["native_replay"]
    assert replay["runtime_hits"] == [0]
    assert replay["new_runtime_hits"] == []
    assert replay["returncode"] == 0


@pytest.mark.parametrize("rounds,tokens,expected", [(5, 100, 5), (5, 2, 2)])
def test_revision_rounds_and_token_budget(tmp_path, monkeypatch, rounds, tokens, expected):
    campaign = _campaign(tmp_path)
    campaign.config.update(llm_max_calls=5, llm_plan_rounds=rounds,
                           llm_plan_token_budget=tokens)
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                "plans": [_plan(2**40, f"invalid-{len(calls)}")]}),
                usage={"total_tokens": 1}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    run_plan_batches(campaign)
    assert len(calls) == expected
    assert campaign.llm_planning_summary["rounds"] == expected
    assert campaign.llm_planning_summary["status"] == "failed"


def test_enough_useful_plans_stop_after_first_batch(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    campaign.config.update(llm_plan_batch_size=1, llm_plan_rounds=5)
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content=json.dumps({"contract_id": "contract",
                "plans": [_plan(7)]}), usage={"total_tokens": 1}, cached=False)

    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=True))
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.LlmClient", FakeClient)
    run_plan_batches(campaign)
    assert len(calls) == 1
    assert campaign.llm_planning_summary["useful_plans"] == 1


def test_unconfigured_llm_has_failed_summary(tmp_path, monkeypatch):
    campaign = _campaign(tmp_path)
    monkeypatch.setattr("fuzzer.pipeline.plan_batches.load_config",
                        lambda: SimpleNamespace(available=False))
    run_plan_batches(campaign)
    assert campaign.llm_planning_summary["status"] == "failed"
    assert campaign.llm_planning_summary["reason"] == "not_configured"
