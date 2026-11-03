import json
from pathlib import Path

from compiler.intake.build import BuildEvidence, UnitResult, _codegen_key, _scrub
from compiler.intake.stubs import docs, gaps
from compiler.intake.stubs.generate import stub_fidelity
from compiler.intake.stubs.llm import ClaudeCLI
from compiler.intake.stubs.verify import _needed


def _intake_dir(tmp_path: Path, text: str, unresolved: list[str]) -> Path:
    directory = tmp_path / "lib"
    (directory / "normalized").mkdir(parents=True)
    (directory / "normalized" / "lib.st").write_text(text, encoding="utf-8")
    (directory / "dependency-report.json").write_text(json.dumps({"classifications": {"unresolved": unresolved}}))
    return directory


def test_gaps_skip_declared_names_and_formals_and_infer_signatures(tmp_path: Path) -> None:
    text = ("VAR_GLOBAL g_level : INT; END_VAR\n"
            "FUNCTION_BLOCK FB\nVAR_INPUT x : DINT; END_VAR\nVAR r : BOOL; t : TON_X; n : DINT; END_VAR\n"
            "r := LogIt(msg := 'x', level := x);\nIF Ready() THEN n := 1; END_IF;\n"
            "t(IN := r, PT := T#1s);\nr := t.Q;\nn := g_level + MAX_SIZE;\nEND_FUNCTION_BLOCK\n")
    directory = _intake_dir(tmp_path, text, ["LogIt", "Ready", "TON_X", "g_level", "MAX_SIZE", "msg", "level"])
    found = gaps.collect([("codesys", "v/lib", directory)])
    assert sorted(g.name for g in found.values()) == ["LogIt", "MAX_SIZE", "Ready", "TON_X"]
    assert gaps.inferred_declaration(found["codesys:LOGIT"]) == (
        "FUNCTION LogIt : BOOL\nVAR_INPUT\n    level : DINT;\n    msg : STRING;\nEND_VAR\nEND_FUNCTION")
    assert gaps.inferred_declaration(found["codesys:READY"]) == "FUNCTION Ready : BOOL\nEND_FUNCTION"
    fb = gaps.inferred_declaration(found["codesys:TON_X"])
    assert fb.startswith("FUNCTION_BLOCK TON_X") and "IN : BOOL;" in fb and "PT : TIME;" in fb and "Q : BOOL;" in fb
    assert gaps.inferred_declaration(found["codesys:MAX_SIZE"]) is None  # used in an expression: type unknown
    context = found["codesys:TON_X"].contexts[0]
    assert "VAR r : BOOL; t : TON_X" in context["unit_source"]
    assert "t(IN := r, PT := T#1s)" in context["unit_source"]
    assert "r := t.Q" in context["unit_source"]


def test_passages_are_found_by_whole_name_and_hashed(tmp_path: Path) -> None:
    manual = tmp_path / "01_CODESYS3" / "spec"
    manual.mkdir(parents=True)
    (manual / "a.txt").write_text("intro\nSysSockCreate creates a socket.\nSysSockCreateX is another\n")
    (tmp_path / "01_CODESYS3" / "原始库文件").mkdir()
    (tmp_path / "01_CODESYS3" / "原始库文件" / "b.txt").write_text("SysSockCreate in a library source\n")
    found = docs.find({"codesys": {"SysSockCreate"}}, root=tmp_path)
    passages = found[("codesys", "SYSSOCKCREATE")]
    assert len(passages) == 1 and passages[0]["line"] == 2 and len(passages[0]["sha256"]) == 64


def test_llm_cache_key_and_offline_replay(tmp_path: Path) -> None:
    llm = ClaudeCLI(tmp_path, offline=True)
    key = llm.key("s", "p", {"type": "object"})
    assert key == ClaudeCLI(tmp_path).key("s", "p", {"type": "object"}) != llm.key("s", "p2", {"type": "object"})
    (tmp_path / f"{key}.json").write_text(json.dumps({"answer": {"stubs": []}}))
    assert llm.ask("s", "p", {"type": "object"}) == ({"stubs": []}, key)
    try:
        llm.ask("s", "other", {"type": "object"})
    except KeyError:
        pass
    else:
        raise AssertionError("offline mode must not call the model")


def test_offline_corrupt_cache_is_a_cache_miss(tmp_path: Path) -> None:
    llm = ClaudeCLI(tmp_path, offline=True)
    key = llm.key("s", "p", {"type": "object"})
    (tmp_path / f"{key}.json").write_text("{not-json", encoding="utf-8")
    try:
        llm.ask("s", "p", {"type": "object"})
    except KeyError as error:
        assert "invalid cached answer" in str(error)
    else:
        raise AssertionError("corrupt offline cache must be blocked")


def test_llm_nonzero_exit_is_not_accepted_as_a_response(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    llm = ClaudeCLI(tmp_path)
    failed = subprocess.CompletedProcess(
        ["claude"], 7, '{"structured_output": {}}', "provider failed"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: failed)
    try:
        llm.ask("system", "prompt", {"type": "object"})
    except RuntimeError as error:
        assert "exited with status 7" in str(error)
        assert "provider failed" in str(error)
    else:
        raise AssertionError("nonzero Claude exit must not be accepted")


def test_stub_dependencies_are_included_transitively() -> None:
    stubs = {"F": "FUNCTION F : T_S\nEND_FUNCTION", "T_S": "TYPE T_S : STRING[255]; END_TYPE", "G": "X"}
    assert _needed({"F"}, stubs) == ["F", "T_S"]


def test_build_messages_are_path_free() -> None:
    output = ("Builder error: /tmp/.tmpAb12/artifacts/x/work/build/lib.st.ll:61229:41: error: insertvalue operand "
              "and field disagree in type: '[81 x i8]' instead of '[31 x i8]'\n  %2 = insertvalue %H undef\n   ^\n")
    assert _codegen_key(output) == "insertvalue operand and field disagree in type: '[81 x i8]' instead of '[31 x i8]'"
    assert _scrub(output).startswith("Builder error: lib.st.ll:61229:41: error:")


def test_failing_global_declarations_are_blanked_alone() -> None:
    from compiler.intake.build import _declaration_span, _declared_globals
    from compiler.intake.diagnostics import SourceIndex

    text = "VAR_GLOBAL CONSTANT\n\tLOCATION : CONSTANTS_LOCATION;\n\tSTRING_LENGTH : INT := 80;\nEND_VAR\n"
    index = SourceIndex(text)
    (unit,) = index.units
    start, end = _declaration_span(index, unit, text.index("CONSTANTS_LOCATION"))
    assert text[start:end] == "LOCATION : CONSTANTS_LOCATION;"
    assert _declared_globals(text[start:end]) == {"LOCATION"}
    inline = "VAR_GLOBAL\n\ts : STRUCT a : INT; END_STRUCT;\nEND_VAR\n"
    index = SourceIndex(inline)
    assert _declaration_span(index, index.units[0], inline.index("a :")) is None


def test_empty_stub_bodies_are_not_run_for_behaviour() -> None:
    from compiler.intake.stubs.evaluate import has_statements

    assert not has_statements("FUNCTION DEG : REAL\nVAR_INPUT\n    RAD : REAL;\nEND_VAR\nEND_FUNCTION")
    assert has_statements("FUNCTION C_TO_F : REAL\nVAR_INPUT\n    C : REAL;\nEND_VAR\nC_TO_F := C * 1.8 + 32.0;\nEND_FUNCTION")


def test_compile_only_stub_is_approximate_even_with_documentation() -> None:
    silent = {"evidence": "documentation", "declaration":
              "FUNCTION F : INT\nVAR_INPUT\n X : INT;\nEND_VAR\nEND_FUNCTION"}
    assert stub_fidelity(silent) == "approximate"
    implemented = {**silent, "declaration": silent["declaration"].replace(
        "END_FUNCTION", "F := X + 1;\nEND_FUNCTION")}
    assert stub_fidelity(implemented) == "semantic"
    assert stub_fidelity({**implemented, "evidence": "call_sites"}) == "approximate"


def test_offline_cache_miss_uses_verified_approximate_fallback(
        tmp_path: Path, monkeypatch) -> None:
    from compiler.intake.stubs import generate

    libraries = tmp_path / "libraries"
    libraries.mkdir()
    (libraries / "corpus.json").write_text(json.dumps({"libraries": [{"vendor": "v", "library": "lib.st",
                                                                  "family": "codesys", "status": "done"}]}),
                                           encoding="utf-8")
    fallback = gaps.Gap("codesys", "MissingCall")
    fallback.uses["call"] = 1
    fallback.arity[0] = 1
    fallback.results["<statement>"] = 1
    fallback.libraries.add("v/lib")
    unsupported = gaps.Gap("codesys", "__SYSTEM")
    unsupported.uses["call"] = 1
    unsupported.declarable = False
    unsupported.libraries.add("v/lib")
    found = {gap.key: gap for gap in (fallback, unsupported)}
    monkeypatch.setattr(generate, "collect", lambda _: found)
    monkeypatch.setattr(generate, "find_passages", lambda _: {})

    def fake_verify(work, round_number, candidates, gaps_by_key, library_dirs, keys):
        return {"stubs": {key: {"ok": True, "errors": [], "libraries": 1} for key in keys}}

    monkeypatch.setattr(generate, "_verify", fake_verify)
    work = tmp_path / "work"
    output = tmp_path / "out"
    assert generate.main(["--libraries", str(libraries), "--work", str(work),
                          "--library-out", str(output), "--offline", "--max-rounds", "1"]) == 0
    frozen = json.loads((output / "codesys.json").read_text(encoding="utf-8"))["stubs"]
    assert [(entry["name"], entry["semantic_fidelity"], entry["verified"]) for entry in frozen] == [
        ("MissingCall", "approximate", "compile")]
    assert "FUNCTION MissingCall : BOOL" in (work / "per-library" / "v" / "lib.st").read_text()
    summary = json.loads((work / "summary.json").read_text(encoding="utf-8"))
    assert summary["unsupported"] == ["codesys:__SYSTEM"]
    assert summary["llm"]["status"] == "blocked"
    assert summary["llm"]["blocked_batches"] == [{"round": 1, "family": "codesys", "names": 2,
                                                     "status": "blocked", "reason": "offline_cache_miss"}]


def test_gap_collection_reads_nested_normalized_sources(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized" / "subsystem"
    normalized.mkdir(parents=True)
    (normalized / "unit.st").write_text(
        "FUNCTION F : BOOL\nF := MissingReady();\nEND_FUNCTION\n", encoding="utf-8")
    (tmp_path / "dependency-report.json").write_text(
        json.dumps({"classifications": {"unresolved": ["MissingReady"]}}), encoding="utf-8")
    found = gaps.collect([("codesys", "v/lib", tmp_path)])
    assert list(found) == ["codesys:MISSINGREADY"]
    assert found["codesys:MISSINGREADY"].contexts[0]["file"] == "subsystem/unit.st"


def test_stub_compare_and_check_preserve_nested_library_paths(tmp_path: Path) -> None:
    from compiler.intake.stubs.compare import compare, main as compare_main
    from compiler.intake.stubs.evaluate_check import _sources

    core = tmp_path / "core"
    stubbed = tmp_path / "stubbed"
    library = Path("vendor") / "family" / "nested"
    for root in (core, stubbed):
        directory = root / library
        (directory / "normalized" / "sub").mkdir(parents=True)
        (directory / "normalized" / "sub" / "unit.st").write_text(
            "FUNCTION F : BOOL\nF := TRUE;\nEND_FUNCTION\n", encoding="utf-8")
        (directory / "compatibility.json").write_text(json.dumps({"dialect": "iec"}), encoding="utf-8")
        (directory / "compatibility-report.json").write_text(json.dumps({
            "units": [{"file": "sub/unit.st", "line": 1, "accepted": True}],
        }), encoding="utf-8")

    result = compare(core, stubbed)
    assert result["total"]["libraries"] == 1
    output = tmp_path / "reports" / "comparison.json"
    assert compare_main(["--core", str(core), "--stubbed", str(stubbed), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["total"]["units"] == 1

    work = tmp_path / "check-work"
    paths = _sources(core / library, ["sub/unit.st"], {}, work)
    assert paths == [work / "sub" / "unit.st"]
    assert paths[0].is_file()


def test_gap_context_carries_state_clock_boundaries_and_outputs(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "cycle.st").write_text(
        "FUNCTION_BLOCK FB_CYCLE\n"
        "VAR_STAT\n"
        "    Tick : UDINT;\n"
        "END_VAR\n"
        "VAR_OUTPUT\n"
        "    Ready : BOOL;\n"
        "END_VAR\n"
        "IF Tick >= 10 THEN Ready := MissingReady(TIME()); END_IF;\n"
        "Tick := Tick + 1;\n"
        "END_FUNCTION_BLOCK\n", encoding="utf-8")
    (tmp_path / "dependency-report.json").write_text(
        json.dumps({"classifications": {"unresolved": ["MissingReady"]}}), encoding="utf-8")
    context = gaps.collect([("codesys", "v/lib", tmp_path)])["codesys:MISSINGREADY"].contexts[0]
    assert context["persistent_state"] == ["Tick : UDINT;"]
    assert any("TIME()" in line for line in context["clock_io"])
    assert any("Tick >= 10" in line for line in context["boundaries"])
    assert any("Ready :=" in line for line in context["outputs"])
    assert any("Tick :=" in line for line in context["per_scan_updates"])


def test_var_external_without_a_definition_is_a_gap_with_its_declared_type(tmp_path: Path) -> None:
    import json

    from compiler.intake.stubs import gaps as gaps_module
    from compiler.intake.stubs.gaps import collect

    text = ("FUNCTION_BLOCK T_PLC_MS\nVAR_OUTPUT\n\tT_PLC_MS : UDINT;\nEND_VAR\nVAR_EXTERNAL\n"
            "\tPLC_TICKS_PER_SEC AT %MW1.44 : INT;\n\tOTHER : DINT;\nEND_VAR\n"
            "T_PLC_MS := TO_UDINT(1000 / PLC_TICKS_PER_SEC) + OTHER;\nEND_FUNCTION_BLOCK\n"
            "VAR_GLOBAL\n\tOTHER : DINT;\nEND_VAR\n")
    (tmp_path / "normalized").mkdir()
    (tmp_path / "normalized" / "lib.st").write_text(text, encoding="utf-8")
    (tmp_path / "dependency-report.json").write_text(json.dumps({"classifications": {
        "unresolved": ["PLC_TICKS_PER_SEC", "OTHER"]}}), encoding="utf-8")
    gaps = collect([("pcworx", "pcworx/lib", tmp_path)])
    assert list(gaps) == ["pcworx:PLC_TICKS_PER_SEC"]  # OTHER is defined by the VAR_GLOBAL list
    gap = gaps["pcworx:PLC_TICKS_PER_SEC"]
    assert gap.kind == "VAR_GLOBAL" and gap.uses["external"] == 1
    assert gaps_module.inferred_declaration(gap) == "VAR_GLOBAL\n    PLC_TICKS_PER_SEC : INT;\nEND_VAR"


def test_minimal_declaration_always_yields_a_silent_declaration() -> None:
    call = gaps.Gap("codesys", "SysSockBind")
    call.uses["call"] = 3
    call.arity[3] += 3
    call.positional[0]["DINT"] += 2
    call.positional[2]["?"] += 1
    call.results["<statement>"] += 3
    assert gaps.minimal_declaration(call) == (
        "FUNCTION SysSockBind : BOOL\nVAR_INPUT\n    IN1 : DINT;\n    IN2 : DINT;\n    IN3 : DINT;\nEND_VAR\nEND_FUNCTION")
    block = gaps.Gap("pcworx", "FB_X")
    block.uses["type"] = 1
    block.uses["instance_call"] = 1
    block.members_written["CMD"]["WORD"] += 1
    block.members_read["BUSY"]["BOOL"] += 1
    assert gaps.minimal_declaration(block) == (
        "FUNCTION_BLOCK FB_X\nVAR_INPUT\n    CMD : WORD;\nEND_VAR\nVAR_OUTPUT\n    BUSY : BOOL;\nEND_VAR\nEND_FUNCTION_BLOCK")
    empty = gaps.Gap("br", "FILE_DATA")
    empty.uses["type"] = 1
    assert "reserved : BYTE;" in gaps.minimal_declaration(empty)
    value = gaps.Gap("codesys", "BYTES")
    value.uses["value"] = 1
    assert gaps.minimal_declaration(value) == "VAR_GLOBAL\n    BYTES : DINT;\nEND_VAR"
    assert call.positional[0]["DINT"] == 2


def test_link_symbols_are_attributed_to_the_stub_that_owns_them() -> None:
    from compiler.intake.stubs.verify import symbol_belongs_to

    assert symbol_belongs_to("RD_SINFO____STIME_TOP_SI____STIME_START_UP_SI", "RD_SINFO")
    assert symbol_belongs_to("RD_SINFO", "RD_SINFO")
    assert symbol_belongs_to("__init_FB_X".upper(), "FB_X")
    assert symbol_belongs_to("FB_X.METHOD", "FB_X")
    assert not symbol_belongs_to("RD_SINFO2", "RD_SINFO")
    assert not symbol_belongs_to("MY_RD_SINFO", "RD_SINFO")


def test_stub_verification_does_not_accept_unattributed_link_failure(tmp_path: Path, monkeypatch) -> None:
    from compiler.intake.stubs import verify

    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "lib.st").write_text(
        "FUNCTION F : BOOL\nF := Missing();\nEND_FUNCTION\n", encoding="utf-8"
    )
    (tmp_path / "compatibility-report.json").write_text(json.dumps({
        "units": [{"file": "lib.st", "line": 1, "accepted": True, "reasons": []}],
    }), encoding="utf-8")
    (tmp_path / "compatibility.json").write_text(json.dumps({"profiles": [], "compiler": {"args": []}}),
                                                     encoding="utf-8")
    monkeypatch.setattr(verify, "resolve_profiles", lambda _names: {"st_sources": [], "native_sources": []})
    monkeypatch.setattr(verify, "compile_natives", lambda _sources, _work: [])
    monkeypatch.setattr(
        verify,
        "build_units",
        lambda *args: BuildEvidence(
            [UnitResult("lib.st", "FUNCTION", "F", 1, status="ok", ir="generated", link="link_failed")],
            [], Path("library.ll"), None,
        ),
    )
    result = verify.check_library({
        "id": "v/lib", "dir": str(tmp_path), "names": ["Missing"],
        "stubs": {"Missing": "FUNCTION Missing : BOOL\nEND_FUNCTION"},
    })
    assert result["usable"] is False
    assert result["gap_units_accepted"] == 0
    assert any("link=link_failed" in item for item in result["unattributed_errors"])


def test_stub_verification_reports_native_profile_compile_failure(tmp_path: Path, monkeypatch) -> None:
    from compiler.intake.stubs import verify
    import subprocess

    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "lib.st").write_text("FUNCTION F : BOOL\nF := Missing();\nEND_FUNCTION\n", encoding="utf-8")
    (tmp_path / "compatibility-report.json").write_text(json.dumps({"units": []}), encoding="utf-8")
    (tmp_path / "compatibility.json").write_text(json.dumps({"profiles": [], "compiler": {"args": []}}),
                                                     encoding="utf-8")
    monkeypatch.setattr(verify, "resolve_profiles", lambda _names: {"st_sources": [], "native_sources": []})
    monkeypatch.setattr(verify, "compile_natives", lambda *_args: (_ for _ in ()).throw(
        subprocess.CalledProcessError(7, ["clang"])))
    result = verify.check_library({
        "id": "v/lib", "dir": str(tmp_path), "names": ["Missing"],
        "stubs": {"Missing": "FUNCTION Missing : BOOL\nEND_FUNCTION"},
    })
    assert result["usable"] is False
    assert result["link_checked"] is False
    assert "native compatibility build failed" in result["unattributed_errors"][0]


def test_keywords_and_internal_names_are_not_declarable(tmp_path: Path) -> None:
    from compiler.intake.stubs.gaps import collect

    text = ("FUNCTION F : INT\nVAR a : ANY; b : __SYSTEM; c : Thing; END_VAR\nF := 1;\nEND_FUNCTION\n")
    (tmp_path / "normalized").mkdir()
    (tmp_path / "normalized" / "lib.st").write_text(text, encoding="utf-8")
    (tmp_path / "dependency-report.json").write_text(json.dumps({"classifications": {
        "unresolved": ["ANY", "__SYSTEM", "Thing"]}}), encoding="utf-8")
    found = collect([("siemens-scl", "s/lib", tmp_path)])
    assert {k: g.declarable for k, g in found.items()} == {"siemens-scl:ANY": False, "siemens-scl:THING": True,
                                                           "siemens-scl:__SYSTEM": False}


def test_blame_requires_the_diagnostic_to_be_about_the_stub() -> None:
    from compiler.intake.stubs.verify import blame

    used = ["T_MAXSTRING", "FB_X", "SYSCALL"]
    types = {"T_MAXSTRING", "FB_X"}
    instances = {"PATH": "T_MAXSTRING", "OBJ": "FB_X"}
    # A variable of the stub type passed to an unrelated call: not the stub's error.
    assert blame("Invalid call parameters", "p := FIND(STR1 := Path, STR2 := x);", used, types, instances) == set()
    # A member of an instance, an unresolved member type (VOID) or the type's name in the message.
    assert blame("Invalid assignment: cannot assign 'INT' to 'BOOL'", "Obj.Busy := 1;", used, types, instances) \
        == {"FB_X"}
    assert blame("Expected a boolean, got `VOID`", "IF Path THEN", used, types, instances) == {"T_MAXSTRING"}
    assert blame("Invalid assignment: cannot assign 'X' to 'T_MaxString'", "Path := x;", used, types, instances) \
        == {"T_MAXSTRING"}
    # A called function stub is named on the line.
    assert blame("Invalid call parameters", "SysCall(a, b);", used, types, instances) == {"SYSCALL"}
    # An unresolved member reference decides by the instance's type.
    assert blame("Could not resolve reference to Busy", "x := Obj.Busy;", used, types, instances) == {"FB_X"}
