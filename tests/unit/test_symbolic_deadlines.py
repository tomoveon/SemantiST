import json
import os
import shutil
import subprocess
from pathlib import Path

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from compiler.harness.contract import abi_json
from compiler.symbolic import build as symbolic_build
from compiler.symbolic import solver
from compiler.symbolic.driver import symbolic_scope
from fuzzer.pipeline.probe import Observation
import pytest


def test_build_timeout_removes_stale_manifest_and_bitcode(tmp_path: Path, monkeypatch) -> None:
    tool = tmp_path / "slow-tool"
    tool.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
    tool.chmod(0o755)
    for name in ("SEMANTIST_CLANG18", "SEMANTIST_LLVM_AS16", "SEMANTIST_LLVM_LINK16",
                 "SEMANTIST_LLVM_NM16", "SEMANTIST_LLVM_DIS16"):
        monkeypatch.setenv(name, str(tool))
    out = tmp_path / "out"
    klee = out / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text("old", encoding="utf-8")
    (klee / "bundle.bc").write_text("old", encoding="utf-8")
    ir = tmp_path / "state.ll"
    ir.write_text("", encoding="utf-8")
    result = symbolic_build.build(build_dir=tmp_path, harness=ir, state_ir=ir,
                                  function="TEST", out_dir=out, abi={}, time_budget_s=0.05)
    assert result["status"] == "timeout"
    assert not (klee / "manifest.json").exists()
    assert not list(klee.glob("*.bc*"))
    assert json.loads((klee / "UNSUPPORTED.json").read_text())["status"] == "timeout"


def test_symbolic_build_tool_launch_failure_is_structured(tmp_path: Path, monkeypatch) -> None:
    tool = tmp_path / "non-executable-tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    for name in ("SEMANTIST_CLANG18", "SEMANTIST_LLVM_AS16", "SEMANTIST_LLVM_LINK16",
                 "SEMANTIST_LLVM_NM16", "SEMANTIST_LLVM_DIS16"):
        monkeypatch.setenv(name, str(tool))
    monkeypatch.setattr(
        symbolic_build.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("permission denied")),
    )
    ir = tmp_path / "state.ll"
    ir.write_text("", encoding="utf-8")
    result = symbolic_build.build(build_dir=tmp_path, harness=ir, state_ir=ir,
                                  function="TEST", out_dir=tmp_path / "out", abi={}, time_budget_s=1)
    assert result["status"] == "failed"
    assert "could not start" in result["reason"]
    assert not (tmp_path / "out" / "klee" / "manifest.json").exists()


@pytest.mark.parametrize("status, expected_rc", [("supported", 0), ("unsupported", 0),
                                                  ("timeout", 1), ("failed", 1)])
def test_symbolic_build_cli_preserves_error_exit_status(tmp_path: Path, monkeypatch,
                                                         status: str, expected_rc: int) -> None:
    abi = tmp_path / "abi.json"
    abi.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(symbolic_build, "build", lambda **kwargs: {"status": status})
    assert symbolic_build.main([
        "--build-dir", str(tmp_path), "--harness", str(tmp_path / "harness.c"),
        "--state-ir", str(tmp_path / "state.ll"), "--function", "F",
        "--out", str(tmp_path / "out"), "--abi", str(abi),
    ]) == expected_rc


def test_query_replay_must_hit_requested_goal_and_obey_native_timeout(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")
    (klee / "bundle.bc").write_bytes(b"bc")
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        if any("klee-out" in str(arg) for arg in command):
            output = next(str(arg).split("=", 1)[1] for arg in command
                          if str(arg).startswith("--output-dir="))
            Path(output).mkdir()
            (Path(output) / "test000001.goal").write_bytes(b"")
            (Path(output) / "test000001.ktest").write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    monkeypatch.setattr(solver, "decode_ktest", lambda path: {"X": (3).to_bytes(2, "little")})
    monkeypatch.setattr(solver, "probe", lambda *args, **kwargs: Observation(0, False, "", hits={5: [0]}))
    kwargs = dict(bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
                  abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7, query_id="q1",
                  task_id="t1", time_budget_s=1, prefix_frames=[{"X": "1"}],
                  prefix_dt_ns=[None], dt_ns=123)
    mismatch = solver.run_query(**kwargs)
    assert mismatch["status"] == "REPLAY_MISMATCH"
    assert "requested goal" in mismatch["reason"]
    candidate = Path(mismatch["candidate_file"]).read_text(encoding="utf-8")
    assert "0.X,INT,1\n" in candidate and "1.X,INT,3\n" in candidate
    assert "0.__DT,TIME,123" in candidate and "1.__DT,TIME,123" in candidate
    driver = (tmp_path / "run" / "solver" / "queries" / "q1" / "driver.c").read_text()
    assert driver.count("semantist_step(storage, 123ULL)") == 2

    Path(kwargs["runtime_ids"]).write_text(json.dumps({"entries": [{
        "runtime_id": 7, "kind": "hazard_violation", "hazard": "array_access",
        "modeling": "conservative",
    }]}), encoding="utf-8")
    monkeypatch.setattr(solver, "probe", lambda *args, **kwargs: Observation(0, False, "", hits={7: [0]}))
    reachable = solver.run_query(**(kwargs | {"query_id": "q3"}))
    assert reachable["status"] == "SAT_APPROX"
    assert reachable["candidate"] is None
    assert reachable["native_goal_reached"] is True

    monkeypatch.setattr(solver, "probe", lambda *args, **kwargs: Observation(None, True, ""))
    timed_out = solver.run_query(**(kwargs | {"query_id": "q2"}))
    assert timed_out["status"] == "TIMEOUT"
    assert timed_out["solver_scope"]["symbolic_fields"] == ["X"]
    assert timed_out["solver_scope"]["concrete_fields"] == timed_out["solver_scope"]["fixed_fields"]
    assert timed_out["solver_scope"]["prefix_dt_ns"] == [123]
    assert timed_out["solver_scope"]["task_id"] == "t1"
    assert timed_out["solver_scope"]["suffix_frames"] == 1


def test_query_tool_launch_failure_is_structured_failed(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")
    (klee / "bundle.bc").write_bytes(b"bc")
    monkeypatch.setattr(
        solver.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("permission denied")),
    )
    result = solver.run_query(
        bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
        abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
        query_id="launch-failure", symbolic_fields=["X"], time_budget_s=1,
    )
    assert result["status"] == "FAILED"
    assert "could not start" in result["reason"]


def test_symbolic_real_request_is_exposed_but_cannot_be_reported_as_unsat(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("R", scalar_spec("REAL"), "VAR_INPUT")]))
    assert symbolic_scope(abi, ["R"]) == (["R"], [])
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    result = solver.run_query(bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
                              abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
                              query_id="real", symbolic_fields=["R"], time_budget_s=1)
    assert result["status"] == "UNKNOWN"
    assert result["imprecise"] == ["float_symbolic:R"]


def test_havoc_model_without_goal_is_unknown_not_unsat(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text(
        json.dumps({"imprecise": [], "model_classes": {"havoc": ["SIN__REAL"]}}),
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        if any("klee-out" in str(arg) for arg in command):
            output = next(str(arg).split("=", 1)[1] for arg in command
                          if str(arg).startswith("--output-dir="))
            Path(output).mkdir()
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    result = solver.run_query(
        bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
        abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
        query_id="havoc", symbolic_fields=["X"], time_budget_s=1,
    )
    assert result["status"] == "UNKNOWN"
    assert "SIN__REAL" in result["solver_scope"]["imprecise"]


def test_approximate_contract_without_goal_is_unknown_not_unsat(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text(json.dumps({"imprecise": []}), encoding="utf-8")
    (bundle / "execution-contract.json").write_text(
        json.dumps({"semantic_fidelity": "approximate", "stub_used": ["vendor_time.st"]}),
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        if any("klee-out" in str(arg) for arg in command):
            output = next(str(arg).split("=", 1)[1] for arg in command
                          if str(arg).startswith("--output-dir="))
            Path(output).mkdir()
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    result = solver.run_query(
        bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
        abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
        query_id="approximate-contract", symbolic_fields=["X"], time_budget_s=1,
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "approximate execution contract cannot establish UNSAT"
    assert result["solver_scope"]["contract_approximate"] is True
    assert result["solver_scope"]["semantic_fidelity"] == "approximate"


def test_no_goal_ktest_is_exported_for_native_seed_feedback(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")
    (bundle / "execution-contract.json").write_text(
        '{"semantic_fidelity": "approximate"}', encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        for arg in command:
            if str(arg).startswith("--output-dir="):
                output = Path(str(arg).split("=", 1)[1])
                output.mkdir()
                (output / "test000001.ktest").write_bytes(b"test")
                (output / "test000002.ktest").write_bytes(b"test")
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    monkeypatch.setattr(solver, "decode_ktest", lambda path: {
        "X": (7 if path.stem == "test000001" else 9).to_bytes(2, "little")})
    monkeypatch.setattr(solver, "probe", lambda *args, **kwargs: pytest.fail("seed-only query must not claim a native finding"))
    result = solver.run_query(
        bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
        abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
        query_id="no-goal-seed", symbolic_fields=["X"], time_budget_s=1,
    )
    assert result["status"] == "UNKNOWN"
    assert result["seed_only"] is True
    assert result["solver_scope"]["contract_approximate"] is True
    assert Path(result["candidate_file"]).read_bytes().startswith(b"0.X,INT,7\n")
    assert len(result["seed_files"]) == 1
    assert Path(result["seed_files"][0]).read_bytes().startswith(b"0.X,INT,9\n")


def test_pointer_snapshot_is_unsupported_with_import_provenance(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")
    (tmp_path / "state.bin").write_bytes(b"state")
    (bundle / "state-layout.json").write_text(json.dumps({
        "regions": [{"name": "target", "pointer_offsets": [0]}],
    }), encoding="utf-8")
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    result = solver.run_query(
        bundle=bundle,
        run_dir=tmp_path / "run",
        target_binary=tmp_path / "target",
        abi=abi,
        runtime_ids=tmp_path / "runtime.json",
        goal_id=7,
        query_id="pointer-snapshot",
        prefix_frames=[{"X": "1"}],
        state_file=tmp_path / "state.bin",
        time_budget_s=1,
    )
    assert result["status"] == "UNSUPPORTED"
    assert result["solver_scope"]["start"] == "snapshot_import"
    assert result["solver_scope"]["init"] == "import"
    assert result["solver_scope"]["state_file"].endswith("state.bin")


def test_complete_pointer_snapshot_is_imported_before_native_replay(tmp_path: Path, monkeypatch) -> None:
    abi = abi_json(TargetAbi("FUNCTION", "TEST", [Param("X", scalar_spec("INT"), "VAR_INPUT")]))
    bundle = tmp_path / "bundle"
    klee = bundle / "klee"
    klee.mkdir(parents=True)
    (klee / "manifest.json").write_text('{"imprecise": []}', encoding="utf-8")
    (klee / "bundle.bc").write_bytes(b"bc")
    state_file = tmp_path / "state.bin"
    state_file.write_bytes(b"\x00" * 32)
    (bundle / "state-layout.json").write_text(json.dumps({
        "regions": [{"name": "harness", "pointer_offsets": [0]}],
    }), encoding="utf-8")
    relocation_file = tmp_path / "state.reloc.json"
    relocation_file.write_text(json.dumps({
        "complete": True,
        "relocations": [{"region": 0, "offset": 0, "target_region": 0, "target_offset": 8}],
    }), encoding="utf-8")

    def fake_run(command, **kwargs):
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"bc")
        if any("klee-out" in str(arg) for arg in command):
            output = next(str(arg).split("=", 1)[1] for arg in command
                          if str(arg).startswith("--output-dir="))
            Path(output).mkdir()
            (Path(output) / "test000001.goal").write_bytes(b"")
            (Path(output) / "test000001.ktest").write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "completed paths")

    monkeypatch.setattr(solver.subprocess, "run", fake_run)
    monkeypatch.setattr(solver, "decode_ktest", lambda path: {"X": (3).to_bytes(2, "little")})
    monkeypatch.setattr(solver, "classify", lambda observation, runtime_index: {
        "oracle": "array_access", "runtime_id": 7, "kind": "hazard_violation",
    })
    monkeypatch.setattr(solver, "probe", lambda *args, **kwargs: Observation(0, False, "", hits={7: [0]}))
    result = solver.run_query(
        bundle=bundle, run_dir=tmp_path / "run", target_binary=tmp_path / "target",
        abi=abi, runtime_ids=tmp_path / "runtime.json", goal_id=7,
        query_id="pointer-import", prefix_frames=[{"X": "1"}], state_file=state_file,
        relocation_file=relocation_file, time_budget_s=1,
    )
    assert result["status"] == "SAT"
    assert result["solver_scope"]["start"] == "snapshot_import"
    assert result["solver_scope"]["relocation_count"] == 1
    driver = (tmp_path / "run" / "solver" / "queries" / "pointer-import" / "driver.c").read_text()
    assert "semantist_import_state(initial_state, initial_relocs, 1UL)" in driver


def test_inout_model_value_is_reconstructed_as_frame_action() -> None:
    inout = Param("Counter", scalar_spec("UINT"), "VAR_IN_OUT", index=1)
    abi = abi_json(TargetAbi("FUNCTION_BLOCK", "ACC", [inout], state_fields=[inout],
                             instance_size=16, instance_align=8))
    assert symbolic_scope(abi, None) == (["Counter"], [])
    frame = solver._field_from_objects(abi, {"Counter": (65535).to_bytes(2, "little")}, {})
    assert frame == {"Counter": "65535"}


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires C compiler")
def test_klee_libc_numeric_parsers_cover_concrete_frame_values(tmp_path: Path) -> None:
    include = tmp_path / "klee"
    include.mkdir()
    (include / "klee.h").write_text(
        "void klee_report_error(const char *, int, const char *, const char *);\n", encoding="utf-8")
    main = tmp_path / "main.c"
    main.write_text(
        "#include <stdlib.h>\n#include <errno.h>\n#include <limits.h>\n"
        "void klee_report_error(const char *a, int b, const char *c, const char *d) { (void)a;(void)b;(void)c;(void)d; }\n"
        "int main(void) { char *end; errno=0; if (strtoll(\"-9223372036854775808\", &end, 10)!=LLONG_MIN || *end || errno) return 1;\n"
        "errno=0; if (strtoull(\"0xffffffffffffffff\", &end, 16)!=ULLONG_MAX || *end || errno) return 2;\n"
        "errno=0; (void)strtoull(\"18446744073709551616\", &end, 10); if (errno!=ERANGE) return 3;\n"
        "errno=0; double value=strtod(\"3.125e2\", &end); if (value<312.49 || value>312.51 || *end || errno) return 4;\n"
        "return 0; }\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    binary = tmp_path / "numeric"
    build = subprocess.run(["cc", "-fno-builtin", "-I", str(tmp_path),
                            str(root / "compiler/symbolic/models/libc_stubs.c"), str(main),
                            "-o", str(binary)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
