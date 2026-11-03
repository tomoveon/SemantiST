import argparse
import json
from pathlib import Path

import pytest

from fuzzer.cli import batch


def test_batch_keeps_candidate_and_nested_execution_status(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T1" / "buggy"

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            (run_dir / "findings" / "candidates").mkdir(parents=True)
            (run_dir / "findings" / "candidates" / "candidate-1.txt").write_text(
                "0.X,INT,1\n", encoding="utf-8"
            )
            (run_dir / "report.json").write_text(json.dumps({
                "candidates": 1,
                "findings": [],
                "execution_status": {
                    "build": "completed",
                    "native": "completed",
                    "symbolic": "UNSUPPORTED",
                },
                "symbolic_queries": [
                    {"status": "UNSUPPORTED"},
                    {"status": "SAT", "native_replay_status": "REPLAY_MISMATCH"},
                ],
                "native_replay_results": [{"testcase_file": "findings/candidates/candidate-1.txt",
                                           "status": "TIMEOUT"}],
                "status_counts": {"TIMEOUT": 1, "REPLAY_MISMATCH": 1},
            }), encoding="utf-8")
            (run_dir / "vulnerability-report").mkdir()
            (run_dir / "vulnerability-report" / "report.json").write_text(json.dumps({
                "summary": {"execution_status": {"replay": "completed"}},
                "replay": {"enabled": True, "status_counts": {"ok": 1}},
            }), encoding="utf-8")
            return "", ""

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    target = {
        "id": "T1",
        "suite": "defects",
        "variant": "buggy",
        "function": "F",
        "st_file": tmp_path / "target.st",
    }
    args = argparse.Namespace(time_per_target=1, build_overhead=1, search_jobs=1, seed=0, switches=[])

    result = batch._run_one(target, out_dir, args)

    assert result["candidate"] is True
    assert result["reached"] is False
    assert result["candidates"] == 1
    assert result["execution_status"]["symbolic"] == "UNSUPPORTED"
    assert result["execution_status"]["campaign"] == "completed"
    assert result["execution_status"]["replay"] == "completed"
    assert result["replay_status_counts"] == {"ok": 1}
    assert result["native_replay_results"][0]["status"] == "TIMEOUT"
    assert result["replay_mismatches"] == 1
    assert result["status_counts"] == {
        "candidate": 1,
        "confirmed": 0,
        "FAILED": 0,
        "ERROR": 0,
        "UNSUPPORTED": 1,
        "TIMEOUT": 1,
        "REPLAY_MISMATCH": 1,
    }


def test_batch_counts_symbolic_build_unsupported_without_queries(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T1" / "buggy"

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            klee = run_dir / "bundle" / "klee"
            klee.mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            (klee / "UNSUPPORTED.json").write_text(json.dumps({
                "reason": "undefined external symbols", "detail": "TEST_EXTERNAL",
            }), encoding="utf-8")
            (run_dir / "report.json").write_text(json.dumps({
                "execution_status": {"symbolic": "NOT_RUN"},
                "symbolic_queries": [], "status_counts": {"UNSUPPORTED": 0},
            }), encoding="utf-8")
            return "", ""

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    target = {"id": "T1", "suite": "defects", "variant": "buggy",
              "function": "F", "st_file": tmp_path / "target.st"}
    args = argparse.Namespace(time_per_target=1, build_overhead=1,
                              search_jobs=1, seed=0, switches=[])
    result = batch._run_one(target, out_dir, args)
    assert result["execution_status"]["symbolic"] == "UNSUPPORTED"
    assert "TEST_EXTERNAL" in result["execution_status"]["symbolic_reason"]
    assert result["symbolic_status_counts"]["UNSUPPORTED"] == 1
    assert result["status_counts"]["UNSUPPORTED"] == 1


def test_batch_preserves_public_failure_and_symbolic_queries(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T2" / "buggy"

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            public = run_dir / "vulnerability-report"
            public.mkdir()
            (public / "report.json").write_text(json.dumps({
                "summary": {
                    "status": "campaign_failed",
                    "execution_status": {"campaign": "FAILED"},
                    "symbolic_queries": [{"status": "REPLAY_MISMATCH"}],
                },
            }), encoding="utf-8")
            return "", ""

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    target = {
        "id": "T2",
        "suite": "defects",
        "variant": "buggy",
        "function": "F",
        "st_file": tmp_path / "target.st",
    }
    args = argparse.Namespace(time_per_target=1, build_overhead=1, search_jobs=1, seed=0, switches=[])

    result = batch._run_one(target, out_dir, args)

    assert result["status"] == "campaign_failed"
    assert result["execution_status"]["campaign"] == "FAILED"
    assert result["symbolic_status_counts"]["REPLAY_MISMATCH"] == 1


def test_batch_does_not_let_malformed_public_summary_erase_raw_evidence(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({
        "status": "completed",
        "status_counts": {"UNSUPPORTED": 1},
        "symbolic_queries": [{"status": "UNSUPPORTED"}],
        "semantic_fidelity": "approximate",
    }), encoding="utf-8")
    public = run_dir / "vulnerability-report"
    public.mkdir()
    (public / "report.json").write_text(json.dumps({
        "summary": {
            "status": "campaign_failed",
            "status_counts": "not-an-object",
            "symbolic_queries": {"status": "UNSUPPORTED"},
            "semantic_fidelity": {"value": "semantic"},
        },
    }), encoding="utf-8")

    report = batch._read_report(run_dir)

    assert report["status"] == "campaign_failed"
    assert report["status_counts"] == {"UNSUPPORTED": 1}
    assert report["symbolic_queries"] == [{"status": "UNSUPPORTED"}]
    assert report["semantic_fidelity"] == "approximate"


def test_batch_counts_solver_errors_separately(tmp_path: Path, monkeypatch) -> None:
    # Keep the test at the report boundary: batch must preserve the solver's
    # explicit ERROR instead of treating it as an empty symbolic phase.
    run_dir = tmp_path / "run"
    (run_dir / "bundle").mkdir(parents=True)
    (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps({
        "status": "completed", "symbolic_queries": [{"status": "ERROR"}],
    }), encoding="utf-8")
    monkeypatch.setattr(batch, "_read_report", lambda _path: json.loads((run_dir / "report.json").read_text()))
    target = {"id": "x", "suite": "defects", "variant": "buggy", "function": "F",
              "st_file": tmp_path / "target.st", "manifest": {}}
    target["st_file"].write_text("FUNCTION F : BOOL\nEND_FUNCTION\n", encoding="utf-8")
    args = argparse.Namespace(time_per_target=1, build_overhead=1, search_jobs=1, seed=0, switches=[])
    # _run_one invokes the pipeline subprocess; replace it with a completed process.
    class _Process:
        returncode = 0
        def communicate(self, timeout=None):
            return "", ""
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: _Process())
    result = batch._run_one(target, tmp_path / "out", args)
    assert result["symbolic_status_counts"]["ERROR"] == 1


def test_batch_keeps_native_engine_timeout_after_pipeline_failure(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T3" / "buggy"

    class TimedOutPipeline:
        returncode = 1

        def communicate(self, timeout=None):
            del timeout
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            (run_dir / "report.json").write_text(json.dumps({
                "findings": [], "execution_status": {"native": "TIMEOUT"},
                "symbolic_queries": [{"status": "UNSAT", "query_id": "q1"}],
            }), encoding="utf-8")
            return "", "engine wall-clock guard"

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: TimedOutPipeline())
    target = {"id": "T3", "suite": "defects", "variant": "buggy", "function": "F",
              "st_file": tmp_path / "target.st"}
    args = argparse.Namespace(time_per_target=1, build_overhead=1, search_jobs=1, seed=0, switches=[])

    result = batch._run_one(target, out_dir, args)

    assert result["status"] == "campaign_failed"
    assert result["timed_out"] is True
    assert result["execution_status"]["native"] == "TIMEOUT"
    assert result["execution_status"]["campaign"] == "TIMEOUT"
    assert result["symbolic_status_counts"]["UNSAT"] == 1


def test_full_duration_requires_recorded_native_runtime(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T4" / "buggy"

    class ShortRun:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            (run_dir / "metrics").mkdir()
            (run_dir / "metrics" / "summary.json").write_text(
                json.dumps({"elapsed_s": 299.9, "ended": "budget", "scans": 10}), encoding="utf-8"
            )
            (run_dir / "report.json").write_text('{"status": "completed"}', encoding="utf-8")
            return "", ""

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: ShortRun())
    target = {"id": "T4", "suite": "benchmarks", "variant": "buggy", "function": "F",
              "st_file": tmp_path / "target.st"}
    args = argparse.Namespace(time_per_target=300, build_overhead=180, search_jobs=1,
                              seed=0, switches=["--full-fuzz-duration"])

    result = batch._run_one(target, out_dir, args)

    assert result["native_fuzz_elapsed_s"] == 299.9
    assert result["status"] == "campaign_failed"
    assert result["execution_status"]["campaign"] == "FAILED"


def test_full_hybrid_duration_checks_engine_budget_with_report_headroom(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    run_dir = out_dir / "T5" / "buggy"
    elapsed = [9.9]
    guards = []

    class Run:
        returncode = 0

        def communicate(self, timeout=None):
            guards.append(timeout)
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "abi.json").write_text("{}", encoding="utf-8")
            (run_dir / "metrics").mkdir()
            (run_dir / "metrics" / "summary.json").write_text(
                json.dumps({"elapsed_s": elapsed[0], "budget_s": 10, "scans": 10}),
                encoding="utf-8",
            )
            (run_dir / "report.json").write_text('{"status": "completed"}', encoding="utf-8")
            return "", ""

    monkeypatch.setattr(batch.subprocess, "Popen", lambda *args, **kwargs: Run())
    target = {"id": "T5", "suite": "benchmarks", "variant": "buggy", "function": "F",
              "st_file": tmp_path / "target.st"}
    args = argparse.Namespace(time_per_target=600, build_overhead=180, search_jobs=1,
                              seed=11, switches=["--full-hybrid-duration", "--llm"])

    assert batch._run_one(target, out_dir, args)["status"] == "campaign_failed"
    elapsed[0] = 10.01
    assert batch._run_one(target, out_dir, args)["status"] == "completed"
    assert guards == [1020, 1020]


def test_batch_summary_aggregates_solver_errors(tmp_path: Path) -> None:
    args = argparse.Namespace(suite="defects", variant="buggy", time_per_target=1,
                              build_overhead=1, search_jobs=1, seed=0, switches=[])
    results = [{"id": "x", "variant": "buggy", "oracle": None,
                "first_candidate_s": None, "confirmed_s": None, "provenance": None,
                "repro_package": False, "candidate": False, "candidates": 0, "reached": False, "confirmed": 0,
                "false_positive": False, "timed_out": False, "status": "completed",
                "semantic_fidelity": "semantic", "model_result_counts": {},
                "confirmed_model_counts": {}, "symbolic_status_counts": {"ERROR": 2},
                "execution_status": {}}]
    summary, _ = batch._write_summary(results, tmp_path, args)
    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["counts"]["symbolic_error"] == 2
    markdown = (tmp_path / "batch-summary.md").read_text(encoding="utf-8")
    assert "campaign" in markdown.splitlines()[4]
    assert "fidelity" in markdown.splitlines()[4]


def test_batch_summary_counts_native_replay_mismatch(tmp_path: Path) -> None:
    args = argparse.Namespace(suite="defects", variant="buggy", time_per_target=1,
                              build_overhead=1, search_jobs=1, seed=0, switches=[])
    results = [{"id": "x", "variant": "buggy", "oracle": None,
                "first_candidate_s": None, "confirmed_s": None, "provenance": None,
                "repro_package": False, "candidate": True, "candidates": 1, "reached": False,
                "confirmed": 0, "false_positive": False, "timed_out": False,
                "status": "completed", "semantic_fidelity": "semantic",
                "model_result_counts": {}, "confirmed_model_counts": {},
                "symbolic_status_counts": {}, "status_counts": {"REPLAY_MISMATCH": 1},
                "execution_status": {}}]

    summary, _ = batch._write_summary(results, tmp_path, args)

    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["counts"]["replay_mismatch"] == 1


def test_batch_resume_keeps_completed_run_and_rejects_different_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    targets = [
        {"id": name, "suite": "defects", "variant": "buggy", "function": name,
         "st_file": tmp_path / f"{name}.st"}
        for name in ("T1", "T2")
    ]
    monkeypatch.setattr(batch, "_defect_targets", lambda *_args: targets)
    out_dir = tmp_path / "out"
    calls = []
    interrupt = True

    def fake_run(target, output, _args):
        nonlocal interrupt
        calls.append(target["id"])
        if target["id"] == "T2" and interrupt:
            interrupt = False
            raise RuntimeError("interrupted target")
        run_dir = output / target["id"] / "buggy"
        run_dir.mkdir(parents=True)
        (run_dir / "evidence.txt").write_text(target["id"], encoding="utf-8")
        return {
            "id": target["id"], "suite": "defects", "variant": "buggy",
            "function": target["function"], "status": "completed", "reached": False,
            "candidate": False, "candidates": 0, "confirmed": 0, "oracle": None,
            "first_candidate_s": None, "confirmed_s": None, "provenance": None,
            "repro_package": False, "false_positive": False, "timed_out": False,
            "semantic_fidelity": "semantic", "model_result_counts": {},
            "confirmed_model_counts": {}, "symbolic_status_counts": {},
            "execution_status": {},
        }

    monkeypatch.setattr(batch, "_run_one", fake_run)
    command = ["--suite", "defects", "--variant", "buggy", "--time-per-target", "5",
               "--out", str(out_dir)]
    with pytest.raises(RuntimeError, match="interrupted target"):
        batch.main(command)
    partial = json.loads((out_dir / "batch-summary.json").read_text(encoding="utf-8"))
    assert partial["expected_targets"] == 2
    assert partial["complete"] is False
    assert [item["id"] for item in partial["targets"]] == ["T1"]

    with pytest.raises(SystemExit) as error:
        batch.main([*command, "--time-per-target", "6", "--resume"])
    assert error.value.code == 2
    assert calls == ["T1", "T2"]

    assert batch.main([*command, "--resume"]) == 0
    assert calls == ["T1", "T2", "T2"]
    assert (out_dir / "T1" / "buggy" / "evidence.txt").read_text() == "T1"
    completed = json.loads((out_dir / "batch-summary.json").read_text(encoding="utf-8"))
    assert completed["complete"] is True
    assert completed["counts"]["total"] == 2


def test_batch_resume_preserves_incomplete_target_directory(tmp_path: Path, monkeypatch) -> None:
    target = {"id": "T1", "suite": "defects", "variant": "buggy", "function": "T1",
              "st_file": tmp_path / "T1.st"}
    monkeypatch.setattr(batch, "_defect_targets", lambda *_args: [target])
    out_dir = tmp_path / "out"
    old_run = out_dir / "T1" / "buggy"
    old_run.mkdir(parents=True)
    (old_run / "evidence.txt").write_text("prior execution", encoding="utf-8")

    def fake_run(_target, output, _args):
        run_dir = output / "T1" / "buggy"
        run_dir.mkdir()
        return {
            "id": "T1", "suite": "defects", "variant": "buggy", "function": "T1",
            "status": "completed", "reached": False, "candidate": False,
            "candidates": 0, "confirmed": 0, "oracle": None,
            "first_candidate_s": None, "confirmed_s": None, "provenance": None,
            "repro_package": False, "false_positive": False, "timed_out": False,
            "semantic_fidelity": "semantic", "model_result_counts": {},
            "confirmed_model_counts": {}, "symbolic_status_counts": {},
            "execution_status": {},
        }

    monkeypatch.setattr(batch, "_run_one", fake_run)
    assert batch.main(["--suite", "defects", "--variant", "buggy",
                       "--out", str(out_dir), "--resume"]) == 0
    assert (out_dir / "T1" / "buggy.interrupted-1" / "evidence.txt").read_text() == "prior execution"


def test_batch_pipeline_launch_failure_is_recorded_and_next_target_runs(
    tmp_path: Path, monkeypatch,
) -> None:
    targets = [
        {"id": name, "suite": "defects", "variant": "buggy", "function": name,
         "st_file": tmp_path / f"{name}.st"}
        for name in ("T1", "T2")
    ]
    monkeypatch.setattr(batch, "_defect_targets", lambda *_args: targets)
    attempts = []

    def fail_launch(*_args, **_kwargs):
        attempts.append(True)
        raise FileNotFoundError("pipeline interpreter missing")

    monkeypatch.setattr(batch.subprocess, "Popen", fail_launch)
    out_dir = tmp_path / "out"
    assert batch.main(["--suite", "defects", "--variant", "buggy",
                       "--out", str(out_dir)]) == 1
    assert len(attempts) == 2
    summary = json.loads((out_dir / "batch-summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is True
    assert summary["counts"]["build_failed"] == 2
    for target in targets:
        run_dir = out_dir / target["id"] / "buggy"
        result = json.loads((run_dir / "batch-result.json").read_text(encoding="utf-8"))
        assert result["returncode"] == 127
        assert result["execution_status"]["campaign"] == "FAILED"
        assert "pipeline launch failed" in (run_dir / "error.log").read_text(encoding="utf-8")
