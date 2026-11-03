import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.corpus.frames import build_testcase, parse_testcase
from fuzzer.pipeline.minimize import history_ablation, minimize
from fuzzer.pipeline.minimize import Minimized
from fuzzer.pipeline.campaign import Candidate, Campaign
from fuzzer.pipeline.probe import Observation, classify


def test_minimize_preserves_shared_and_frame_time(tmp_path):
    shared = Param("Shared", scalar_spec("INT"), "VAR_IN_OUT")
    abi = TargetAbi(
        "FUNCTION_BLOCK", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")],
        state_fields=[shared], instance_size=16,
    )
    original = build_testcase(
        abi, [{"X": "0"}, {"X": "1"}, {"X": "0"}], shared={"Shared": "3"},
        frame_dt_ns=[None, 500_000_000, None], include_dt=True,
    )
    runtime_index = {7: {"kind": "hazard_violation", "hazard": "array_access",
                         "modeling": "exact", "source": {"line": 4}}}
    signature = ("array_access", 7, "{'line': 4}")
    seen = []

    def fake_probe(_target, data, *, work_dir, timeout_s):
        parsed = parse_testcase(data, abi)
        seen.append(parsed)
        hits = {7: [0]} if parsed.shared.get("Shared") == "3" and any(
            frame.values["X"] == "1" and frame.dt_ns == 500_000_000
            for frame in parsed.frames
        ) else {}
        return Observation(0, False, "", hits=hits)

    minimized = minimize(
        original, abi_like=abi, runtime_index=runtime_index, target_binary=Path("unused"),
        work_dir=tmp_path, probe_fn=fake_probe, signature=signature, max_rounds=1,
        deadline=time.monotonic() + 2,
    )
    parsed = parse_testcase(minimized.testcase, abi)
    assert parsed.shared == {"Shared": "3"}
    assert any(frame.dt_ns == 500_000_000 for frame in parsed.frames)
    assert classify(fake_probe(None, minimized.testcase, work_dir=tmp_path, timeout_s=1), runtime_index)
    assert all(item.shared == {"Shared": "3"} for item in seen)

    history = history_ablation(
        minimized.testcase, abi_like=abi, runtime_index=runtime_index,
        target_binary=Path("unused"), work_dir=tmp_path, probe_fn=fake_probe,
        signature=signature, deadline=time.monotonic() + 2,
    )
    assert history["required_frames"]


def test_minimization_stops_at_deadline(tmp_path):
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    testcase = build_testcase(abi, [{"X": "1"}, {"X": "0"}])

    def fail_if_called(*args, **kwargs):
        raise AssertionError("probe ran after the deadline")

    with pytest.raises(TimeoutError, match="campaign deadline exhausted"):
        minimize(
            testcase, abi_like=abi, runtime_index={}, target_binary=Path("unused"),
            work_dir=tmp_path, probe_fn=fail_if_called, signature=("array_access", 7, "None"),
            deadline=time.monotonic() - 1,
        )


def test_repro_history_keeps_original_and_minimized_depths(tmp_path, monkeypatch):
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    original = build_testcase(abi, [{"X": "0"}, {"X": "1"}])
    reduced = build_testcase(abi, [{"X": "1"}])
    source = tmp_path / "target.st"
    source.write_text("FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path / "run"
    campaign.bundle = bundle
    campaign.target_binary = tmp_path / "missing-target"
    campaign.st_file = source
    campaign.function = "F"
    campaign.config = {"minimize": True}
    campaign.deadline = time.monotonic() + 10
    campaign.model = SimpleNamespace(runtime_index={})
    campaign.time_dependent = False
    campaign._abi_like = lambda: abi
    candidate = Candidate(
        "F0001", original, Observation(0, False, ""),
        {"oracle": "array_access", "runtime_id": 7, "source": None}, {}, 0.0,
    )
    monkeypatch.setattr("fuzzer.pipeline.campaign.minimize_module.minimize", lambda *args, **kwargs:
                        Minimized(reduced, 1, 1, 1, 0))
    monkeypatch.setattr("fuzzer.pipeline.campaign.minimize_module.history_ablation", lambda *args, **kwargs: {
        "observed_depth": 1, "shortest_found_depth": 1, "required_frames": [0],
        "ablation": [], "initialization_scope": "cold",
    })
    campaign._write_repro(candidate, {"finding_id": "F0001", "oracle": "array_access"})
    history_path = campaign.run_dir / "findings" / "confirmed" / "F0001" / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert history["observed_depth"] == 2
    assert history["shortest_found_depth"] == 1
    assert history["minimized_observed_depth"] == 1

    def deadline_expired(*args, **kwargs):
        raise TimeoutError("campaign deadline exhausted during minimization")

    monkeypatch.setattr("fuzzer.pipeline.campaign.minimize_module.minimize", deadline_expired)
    timeout_candidate = Candidate(
        "F0002", original, Observation(0, False, ""),
        {"oracle": "array_access", "runtime_id": 7, "source": None}, {}, 0.0,
    )
    campaign._write_repro(timeout_candidate, {"finding_id": "F0002", "oracle": "array_access"})
    timeout_package = campaign.run_dir / "findings" / "confirmed" / "F0002"
    timeout_history = json.loads((timeout_package / "history.json").read_text(encoding="utf-8"))
    assert timeout_history["minimization_status"] == "TIMEOUT"
    assert (timeout_package / "minimized.txt").read_bytes() == original
