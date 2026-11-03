import json
from pathlib import Path
from types import SimpleNamespace

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.pipeline.campaign import Campaign
from fuzzer.pipeline.metrics import Metrics
from fuzzer.pipeline.probe import Observation


def _campaign(tmp_path: Path) -> Campaign:
    campaign = object.__new__(Campaign)
    campaign.run_dir = tmp_path
    campaign.metrics = Metrics(tmp_path)
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    campaign._abi_like = lambda: abi
    return campaign


def test_native_novelty_reinjects_solver_seed_once_in_order(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    first = b"0.X,INT,7\n0.__DT,TIME,10000000\n"
    second = b"0.X,INT,9\n"
    observation = Observation(0, False, "", hits={10: [0]})

    assert campaign._offer_solver_seed(first, observation, query_id="q1", task_id="task-1")
    assert not campaign._offer_solver_seed(second, observation, query_id="q2", task_id="task-1")
    assert campaign._offer_solver_seed(second, Observation(0, False, "", hits={11: [0]}),
                                       query_id="q3", task_id="task-2")
    directory = tmp_path / "solver" / "reinject"
    assert (directory / "solver-seed-00000001.seed").read_bytes() == first
    assert (directory / "solver-seed-00000002.seed").read_bytes() == second
    metadata = json.loads((directory / "solver-seed-00000001.json").read_text())
    assert metadata["task_id"] == "task-1"
    assert metadata["query_id"] == "q1"
    assert metadata["new_runtime_hits"] == [10]
    assert not list(directory.glob("*.tmp"))
    assert campaign.metrics.summary()["solver_seeds_enqueued"] == 2


def test_invalid_or_failed_native_testcase_never_enters_rust_queue(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    campaign.model = SimpleNamespace(runtime_index={10: {
        "kind": "hazard_violation", "modeling": "exact", "hazard": "division",
    }})
    valid = b"0.X,INT,7\n"
    hit = {10: [0]}
    assert not campaign._offer_solver_seed(valid, Observation(0, True, "", hits=hit),
                                           query_id="timeout", task_id=None)
    assert not campaign._offer_solver_seed(valid, Observation(7, False, "", hits=hit),
                                           query_id="failed", task_id=None)
    assert not campaign._offer_solver_seed(b"0.X,INT,40000\n", Observation(0, False, "", hits=hit),
                                           query_id="out-of-range", task_id=None)
    assert not campaign._offer_solver_seed(b"__INIT,ENUM,import\n0.X,INT,7\n",
                                           Observation(0, False, "", hits=hit),
                                           query_id="import", task_id=None)
    assert not campaign._offer_solver_seed(valid, Observation(0, False, "", hits=hit),
                                           query_id="finding", task_id=None)
    assert not (tmp_path / "solver" / "reinject").exists()


def test_non_goal_klee_testcase_can_be_used_as_seed_without_becoming_finding(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    testcase = tmp_path / "candidate.txt"
    testcase.write_bytes(b"0.X,INT,8\n")
    for status in ("UNKNOWN", "UNSAT", "REPLAY_MISMATCH"):
        result, loaded = campaign._solver_candidate({"status": status, "candidate_file": str(testcase)})
        assert result["status"] == status
        assert loaded == testcase.read_bytes()


def test_conservative_goal_replay_is_not_a_vulnerability_or_mismatch(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    hit = Observation(0, False, "", hits={17: [0]})
    assert campaign._record_klee_native_replay(
        prefix_id="p1", query_id="q1", task_id="task", observation=hit,
        matched=False, model_goal_reached=True,
    ) == "model_goal_reached"
    assert campaign._record_klee_native_replay(
        prefix_id="p1", query_id="q2", task_id="task",
        observation=Observation(0, False, ""), matched=False,
    ) == "REPLAY_MISMATCH"


def test_additional_klee_paths_are_individually_probed_and_filtered(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"0.X,INT,7\n")
    second.write_bytes(b"0.X,INT,9\n")
    observed = []

    def probe(testcase, **kwargs):
        observed.append(testcase)
        return Observation(0, False, "", hits={10 + len(observed): [0]})

    campaign._probe_with_deadline = probe
    campaign._offer_additional_solver_seeds(
        {"seed_only": True, "seed_files": [str(first), str(second)]},
        timeout_s=1.0, deadline=float("inf"), query_id="q", task_id="task")
    assert observed == [first.read_bytes(), second.read_bytes()]
    assert campaign.metrics.summary()["solver_seeds_enqueued"] == 2
    campaign._offer_additional_solver_seeds(
        {"status": "SAT", "seed_files": [str(first)]},
        timeout_s=1.0, deadline=float("inf"), query_id="q", task_id="task")
    assert len(observed) == 2
