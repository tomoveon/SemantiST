import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from compiler.intake import clones, matrix, pragmas, unsupported, validation
from compiler.intake import rusty
from compiler.intake.closure import direct_dependencies
from compiler.intake.diagnostics import SourceIndex, classify, locate
from compiler.intake.normalize.base import RuleContext
from compiler.intake.normalize.rules.ax import mangle
from compiler.intake.normalize.rules.codesys import EndFunctionBlockKeyword
from compiler.intake.normalize.rules.storage import VarInstMember, VarStatGlobal
from compiler.intake.normalize.sourcemap import EditableSource, line_map
from compiler.intake.normalize.engine import normalize
from compiler.intake.rusty import CompileResult, Diagnostic, parse_diagnostics
from compiler.intake.structure import split_units
from compiler.parser.st import significant, tokenize_lossless
from fuzzer.reporting.origin import classify as crash_origin


def test_sourcemap_tracks_replacements_insertions_and_moves() -> None:
    source = EditableSource("A\nbody\nM1\nM2\nEND\n")
    start, end = source.text.index("M1"), source.text.index("END")
    source.move(start, end, source.text.index("body"), "member-reorder")
    assert source.text == "A\nM1\nM2\nbody\nEND\n"
    # Moved lines keep their original line numbers.
    assert line_map(source) == [1, 3, 4, 2, 5, 6]
    position = source.text.index("M2")
    source.replace(position, position + 2, "XX", "rename")
    source.replace(0, 0, "(* c *)\n", "insert")
    assert source.text == "(* c *)\nA\nM1\nXX\nbody\nEND\n"
    assert source.to_original(source.text.index("body")) == source.original.index("body")
    rules = [s["rule"] for s in source.segments()]
    assert {"member-reorder", "rename", "insert"} <= set(rules)


def test_units_and_members_are_split_with_spans() -> None:
    text = "FUNCTION_BLOCK FB\nVAR x : INT; END_VAR\nx := 1;\nMETHOD M : INT\nM := x;\nEND_METHOD\nEND_FUNCTION_BLOCK\n"
    (unit,) = split_units(text)
    assert (unit.kind, unit.name) == ("FUNCTION_BLOCK", "FB")
    assert text[unit.start:unit.end].endswith("END_FUNCTION_BLOCK")
    assert [(m.kind, m.name) for m in unit.members] == [("METHOD", "M")]


def test_function_named_like_a_modifier_keeps_its_name() -> None:
    (unit,) = split_units("FUNCTION OVERRIDE : REAL\nOVERRIDE := 1.0;\nEND_FUNCTION\n")
    assert unit.name == "OVERRIDE"


def test_clang_diagnostics_and_codegen_errors_are_parsed() -> None:
    output = (
        "/p/a.st:3:29:{3:29-3:36}: error[E043]: Array assignments must be surrounded with `[]`\n"
        "Could not resolve reference to address at: /p/a.st:4:37:{4:37-5:6}: \n"
    )
    first, second = parse_diagnostics(output)
    assert (first.code, first.line, first.column) == ("E043", 3, 29)
    # Codegen errors are printed with 0-based positions.
    assert (second.code, second.line, second.column, second.end_line) == ("CODEGEN", 5, 38, 6)


def test_missing_rusty_compiler_is_structured_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "plc")

    monkeypatch.setattr(rusty.subprocess, "Popen", missing)
    result = rusty.run_plc(["--check"])
    assert result.returncode == 127
    assert "could not start" in result.output


def test_multi_line_codegen_errors_keep_their_message_and_location() -> None:
    output = (
        "/p/a.st:1:1:{1:1-1:14}: warning[E013]: the words in FUNCTIONBLOCK should be separated by a `_`\n"
        "Expected Struct-literal, got Assignment {\n"
        "    left: ReferenceExpr {\n"
        "    },\n"
        "} at: /p/a.st:2:4:{2:4-2:18}: .\n"
    )
    warning, error = parse_diagnostics(output)
    assert warning.message == "the words in FUNCTIONBLOCK should be separated by a `_`"
    assert error.message.splitlines()[0] == "Expected Struct-literal, got Assignment {"
    assert (error.code, error.line, error.column) == ("CODEGEN", 3, 5)


@pytest.mark.parametrize("failed_stage", ["check", "codegen"])
def test_normalization_reports_nonzero_compiler_exit_without_diagnostics(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str) -> None:
    monkeypatch.setattr(rusty, "stdlib_sources", lambda: [])

    def fake_check(files, *, ir=None, **kwargs):
        if (ir is None) == (failed_stage == "check"):
            return CompileResult(17, output="compiler failed without a clang diagnostic")
        return CompileResult(0)

    monkeypatch.setattr(rusty, "check", fake_check)
    result = normalize({"f.st": "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n"}, {}, {}, [], tmp_path)
    assert not result.type_checks
    assert result.remaining[0]["code"] == "COMPILER_EXIT"
    assert result.remaining[0]["stage"] == failed_stage
    assert result.remaining[0]["returncode"] == 17
    assert result.rounds[0][f"{failed_stage}_returncode"] == 17


def test_normalization_keeps_unhandled_codegen_diagnostic(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rusty, "stdlib_sources", lambda: [])

    def fake_check(files, *, ir=None, **kwargs):
        if ir is None:
            return CompileResult(0)
        return CompileResult(1, [Diagnostic("error", "CODEGEN", "unsupported IR operation",
                                            str(files[0]), 2, 1)])

    monkeypatch.setattr(rusty, "check", fake_check)
    result = normalize({"f.st": "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n"}, {}, {}, [], tmp_path)
    assert not result.type_checks
    assert [(error["code"], error["line"]) for error in result.remaining] == [("CODEGEN", 2)]
    assert result.rounds[0]["codegen_returncode"] == 1


def test_library_build_rejects_unparsed_check_and_non_undefined_link_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from compiler.intake.build import BuildEvidence, UnitResult, _link, validate
    from compiler.intake import build as intake_build

    monkeypatch.setattr(rusty, "check", lambda *args, **kwargs: CompileResult(19, output="compiler failed"))
    evidence = validate({"f.st": "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n"},
                        tmp_path / "check", [], [], [])
    assert evidence.ir_path is None
    assert evidence.configuration_errors[0]["returncode"] == 19

    ir = tmp_path / "f.ll"
    ir.write_text("define i32 @F() {\n  ret i32 1\n}\n", encoding="utf-8")
    linked = BuildEvidence([UnitResult("f.st", "FUNCTION", "F", 1, ir="generated")], [], ir, None)
    monkeypatch.setattr(intake_build, "internalize", lambda *args: subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(rusty, "runtime_archive", lambda: tmp_path / "lib.a")
    calls = iter([subprocess.CompletedProcess([], 0, "", ""),
                  subprocess.CompletedProcess([], 1, "", "ld: unexpected failure")])
    monkeypatch.setattr(intake_build.subprocess, "run", lambda *args, **kwargs: next(calls))
    _link(linked, tmp_path, [])
    assert linked.units[0].link == "link_failed"
    assert linked.summary()["linked"] == 0
    assert linked.executable is None


def test_compatibility_report_names_non_undefined_link_failure() -> None:
    from compiler.intake.report import compatibility_report

    unit = {"file": "f.st", "name": "F", "kind": "FUNCTION", "line": 1,
            "status": "ok", "ir": "generated", "link": "link_failed",
            "diagnostics": [], "undefined_symbols": []}
    report = compatibility_report({}, {"rule_summary": {}},
                                  {"units": [unit], "link_output": "ld: unexpected failure"},
                                  {"classifications": {"unresolved": []}, "time_sources": [],
                                   "implicit_check_functions": [], "external_uses": [], "gate": {}},
                                  {}, {}, {"profiles": {}}, {"f.st": "FUNCTION F : INT\nEND_FUNCTION\n"},
                                  {}, primary="f.st")
    reason = report["units"][0]["reasons"][0]
    assert reason == {"stage": "link", "code": "link_failed", "message": "ld: unexpected failure"}
    assert not report["units"][0]["accepted"]


def test_normalization_preserves_distinct_nested_source_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rusty, "stdlib_sources", lambda: [])
    seen = []

    def fake_check(files, *, ir=None, **kwargs):
        seen.append([path.resolve() for path in files])
        assert all(path.is_file() for path in files)
        if ir is not None:
            ir.write_text("; test IR\n", encoding="utf-8")
        return CompileResult(0)

    monkeypatch.setattr(rusty, "check", fake_check)
    source = "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n"
    result = normalize({"nested/a.st": source, "nested__a.st": source}, {}, {}, [], tmp_path)
    assert result.type_checks
    assert len(set(seen[0])) == 2
    assert seen[0][0] == (tmp_path / "nested" / "a.st").resolve()


def test_normalization_rejects_zero_exit_without_ir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rusty, "stdlib_sources", lambda: [])
    monkeypatch.setattr(rusty, "check", lambda *args, **kwargs: CompileResult(0))
    result = normalize({"f.st": "FUNCTION F : INT\nEND_FUNCTION\n"}, {}, {}, [], tmp_path)
    assert not result.type_checks
    assert result.remaining[0]["code"] == "CODEGEN_MISSING_IR"


def test_library_cli_exposes_fully_unsupported_intake(monkeypatch, tmp_path, capsys) -> None:
    from compiler.intake import __main__ as intake_cli

    source = tmp_path / "library.st"
    source.write_text("FUNCTION F : INT\nEND_FUNCTION\n", encoding="utf-8")
    monkeypatch.setitem(intake_cli.corpus.BY_DIRECTORY, "test-vendor", SimpleNamespace(dialect="iec"))
    monkeypatch.setattr(intake_cli, "intake", lambda *args, **kwargs: {
        "syntax": {"level": "none", "pous": 1, "pous_accepted": 0},
        "behaviour": {"level": "needs_platform"},
        "rules_applied": {},
    })

    assert intake_cli.main(["library", str(source), "--vendor", "test-vendor",
                            "--output", str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "UNSUPPORTED"


def test_library_cli_exposes_intake_exception(monkeypatch, tmp_path, capsys) -> None:
    from compiler.intake import __main__ as intake_cli

    source = tmp_path / "library.st"
    source.write_text("FUNCTION F : INT\nEND_FUNCTION\n", encoding="utf-8")
    monkeypatch.setitem(intake_cli.corpus.BY_DIRECTORY, "test-vendor", SimpleNamespace(dialect="iec"))
    monkeypatch.setattr(intake_cli, "intake", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("compiler invocation failed")
    ))

    assert intake_cli.main(["library", str(source), "--vendor", "test-vendor",
                            "--output", str(tmp_path / "out")]) == 1
    assert "compiler invocation failed" in capsys.readouterr().err


def test_construct_classification_uses_the_token_at_the_diagnostic() -> None:
    index = SourceIndex("FUNCTION OVERRIDE : REAL\nEND_FUNCTION\n")
    diagnostic = Diagnostic("error", "E007", "Unexpected token: expected Identifier but found OVERRIDE", "", 1, 10)
    token = index.token_at(index.offset(1, 10))
    assert classify(diagnostic, token, index) == "keyword_as_identifier:OVERRIDE"


def test_byte_columns_map_to_characters() -> None:
    index = SourceIndex("(* ä *) x\n")
    # 'x' is at byte column 10 (ä takes two bytes) but character column 9.
    assert index.text[index.offset(1, 10)] == "x"


def test_clone_normalization_ignores_names_layout_and_comments() -> None:
    a = clones.normalized_tokens("FUNCTION F : INT\n(* doc *)\nVAR_INPUT x : INT; END_VAR\nF := x + 1;\nEND_FUNCTION")
    b = clones.normalized_tokens("function G:INT VAR_INPUT y:INT; END_VAR G:=y+1; END_FUNCTION")
    c = clones.normalized_tokens("FUNCTION F : INT VAR_INPUT x : INT; END_VAR F := x + 2; END_FUNCTION")
    assert a == b
    assert a != c
    assert clones.estimated_jaccard(clones.signature(a), clones.signature(a)) == 1.0


def test_crash_origin_labels() -> None:
    assert crash_origin("thread 'main' panicked at libs/stdlib/src/string_functions.rs:9:5:")["origin"] \
        == "runtime_library"
    assert crash_origin("    #0 0x1 in __interceptor_memcpy\n    #1 0x2 in __SEMANTIST_MEMCPY_CHECKED runtime.c:4\n")[
        "origin"] == "compatibility_layer"
    assert crash_origin("    #0 0x1 in abort\n    #1 0x2 in FIFO_16 lib.st:10\n")["origin"] == "source_program"
    assert crash_origin("Segmentation fault")["origin"] == "undetermined"


def test_pragma_classes() -> None:
    assert pragmas.classify("{attribute 'qualified_only'}")[0] == "ignorable"
    assert pragmas.classify("{attribute 'pack_mode' := '1'}")[0] == "layout"
    assert pragmas.classify("{attribute 'call_after_init'}")[0] == "semantic"
    assert pragmas.classify("{__SET_VAR(data__->,Q,,1)}")[0] == "semantic"
    assert pragmas.classify("{Test}")[0] == "test_framework"
    assert pragmas.classify("{attribute 'never_seen_before'}")[0] == "unclassified"


def test_unsupported_constructs_are_detected_per_unit() -> None:
    text = ("FUNCTION_BLOCK FB\nVAR p : POINTER TO INT; tx : DWORD; bits AT tx : ARRAY[0..31] OF BOOL; END_VAR\n"
            "p := __NEW(INT);\nEND_FUNCTION_BLOCK\nTYPE S2 EXTENDS S1 : STRUCT a : INT; END_STRUCT END_TYPE\n")
    tokens = tokenize_lossless(text)
    found = unsupported.detect(significant(tokens), split_units(text, tokens))
    assert {f["construct"] for f in found} == {"AT overlay", "__NEW", "STRUCT EXTENDS"}


def test_ax_assignment_attempt_and_short_circuit_operators_are_unsupported() -> None:
    text = "FUNCTION F : BOOL\nj ?= i;\nF := a && b || c & d;\nEND_FUNCTION\n"
    tokens = tokenize_lossless(text)
    found = unsupported.detect(significant(tokens), split_units(text, tokens))
    assert [(f["construct"], f["line"]) for f in found] == [
        ("?= assignment attempt", 2), ("&& short-circuit operator", 3), ("|| short-circuit operator", 3)]


def test_codesys_storage_classes_left_in_place_are_unsupported() -> None:
    text = ("FUNCTION_BLOCK FB\nVAR_STAT n : INT; END_VAR\nMETHOD M : INT\nVAR_INST k : INT; END_VAR\n"
            "k := k + 1; M := k;\nEND_METHOD\nn := n + 1;\nEND_FUNCTION_BLOCK\n")
    tokens = tokenize_lossless(text)
    found = unsupported.detect(significant(tokens), split_units(text, tokens))
    assert [(f["construct"], f["line"]) for f in found] == [("VAR_STAT", 2), ("VAR_INST", 4)]


def _propose(rule, text: str, line: int, column: int):
    index = SourceIndex(text)
    context = RuleContext({"f.st": text}, {"f.st": index}, {}, {},
                          {t.upper for t in index.significant if t.is_word})
    diagnostic = Diagnostic("error", "E007", "Unexpected token", "f.st", line, column)
    return rule.propose(context, "f.st", locate(diagnostic, index))


def test_storage_rules_move_blocks_and_decline_retain_at_and_shadowing() -> None:
    plain = "FUNCTION F : INT\nVAR_STAT n : INT; END_VAR\nn := n + 1; F := n;\nEND_FUNCTION\n"
    proposal = _propose(VarStatGlobal(), plain, 2, 10)
    assert proposal.edits[0].replacement == "VAR_GLOBAL\nF_n__STAT : INT;\nEND_VAR\n"
    assert [e.replacement for e in proposal.edits[2:]] == ["F_n__STAT"] * 3
    assert _propose(VarStatGlobal(), plain.replace("VAR_STAT", "VAR_STAT RETAIN"), 2, 10) is None
    assert _propose(VarStatGlobal(), plain.replace("n : INT", "n AT %MW0 : INT"), 2, 10) is None
    shadowed = ("FUNCTION_BLOCK B\nVAR_STAT n : INT; END_VAR\nMETHOD M : INT\nVAR n : INT; END_VAR\nM := n;\n"
                "END_METHOD\nEND_FUNCTION_BLOCK\n")
    assert _propose(VarStatGlobal(), shadowed, 2, 10) is None
    method = ("FUNCTION_BLOCK B\nVAR x : INT; END_VAR\nMETHOD M : INT\nVAR_INST k : INT; END_VAR\n"
              "k := k + 1; M := THIS^.x + k;\nEND_METHOD\nEND_FUNCTION_BLOCK\n")
    proposal = _propose(VarInstMember(), method, 4, 10)
    assert proposal.edits[0].start == method.index("VAR x")
    assert proposal.edits[0].replacement == "VAR\nM_k__INST : INT;\nEND_VAR\n"
    assert _propose(VarInstMember(), method.replace("THIS^.x", "THIS^.k"), 4, 10) is None
    outside = "METHOD M : INT\nVAR_INST k : INT; END_VAR\nM := k;\nEND_METHOD\n"
    assert _propose(VarInstMember(), outside, 2, 10) is None


def test_end_functionblock_only_where_it_closes_a_function_block() -> None:
    text = "FUNCTIONBLOCK B\nVAR x : INT; END_VAR\nx := 1;\nEND_FUNCTIONBLOCK\nFUNCTION G : INT\nG := 1;\nEND_FUNCTION\n"
    proposal = _propose(EndFunctionBlockKeyword(), text, 5, 1)
    assert [(text[e.start:e.end], e.replacement) for e in proposal.edits] == [
        ("END_FUNCTIONBLOCK", "END_FUNCTION_BLOCK")]
    assert _propose(EndFunctionBlockKeyword(), text.replace("END_FUNCTIONBLOCK", "END_FUNCTION_BLOCK"), 5, 1) is None


def test_namespace_mangling() -> None:
    assert mangle("AXOpen.Core.AxoTask") == "AXOpen_Core__AxoTask"
    assert mangle("Plain") == "Plain"


def test_library_qualifiers_select_sibling_projects(tmp_path: Path) -> None:
    folder = tmp_path / "03_TwinCAT3" / ".st文件"
    folder.mkdir(parents=True)
    (folder / "xml_Repo__Core.st").write_text("FUNCTION_BLOCK Base\nEND_FUNCTION_BLOCK\n", encoding="utf-8")
    user = folder / "xml_Repo__App.st"
    user.write_text("FUNCTION_BLOCK Derived EXTENDS Core.Base\nEND_FUNCTION_BLOCK\n", encoding="utf-8")
    (folder / "xml_Other__Core.st").write_text("FUNCTION_BLOCK Base\nEND_FUNCTION_BLOCK\n", encoding="utf-8")
    assert [(p.name, reason) for p, reason in direct_dependencies(user, folder)] == [
        ("xml_Repo__Core.st", "library qualifier CORE.")]


def test_matrix_counts_clone_families_under_original_names(tmp_path: Path) -> None:
    vendor, library = "05_Siemens_TIA_SCL", "lib.st"
    directory = tmp_path / vendor / "lib"
    directory.mkdir(parents=True)
    unit = {"type_check": "passed", "ir": "passed", "link": "passed", "reasons": [], "line": 1, "kind": "FUNCTION_BLOCK"}
    units = [{**unit, "name": "FbBlink", "file": library, "accepted": True},
             {**unit, "name": "Ns__Helper", "file": "dep.st", "accepted": True}]
    report = {"syntax": {"level": "full", "pous": 2, "pous_accepted": 2, "units": 2, "type_checked": 2,
                         "ir_generated": 2, "linked": 2},
              "behaviour": {"level": "needs_platform", "unverified": []}, "unsupported_constructs": [],
              "time_sources": [], "implicit_check_functions": [], "rules_applied": {}, "target_identity": {},
              "units": units}
    (directory / "compatibility-report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "dependency-report.json").write_text(json.dumps({"classifications": {"unresolved": []}}))
    renames = {'"FbBlink"': "FbBlink", "NS.HELPER": "Ns__Helper"}
    (directory / "transformations.json").write_text(json.dumps({"renames": renames}), encoding="utf-8")
    entry = {"vendor": vendor, "family": "siemens-scl", "priority": "B", "library": library, "status": "done"}
    (tmp_path / "corpus.json").write_text(json.dumps({"libraries": [entry]}), encoding="utf-8")
    families = [{"vendor": vendor, "library": library, "name": "FbBlink", "near": "a"},
                {"vendor": vendor, "library": "dep.st", "name": "Ns.Helper", "near": "b"}]
    result = matrix.build(tmp_path, {"pou_families": families, "per_family": {}}, None)
    (row,) = [f for f in result["families"] if f["family"] == "siemens-scl"]
    assert row["independent_pous_accepted"] == 2


def test_validation_log_summaries(tmp_path: Path) -> None:
    log = tmp_path / "suite.log"
    log.write_text("159 passed, 6 subtests passed in 467.78s (0:07:47)\n"
                   "test result: ok. 92 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
                   "test result: FAILED. 3 passed; 1 failed; 2 ignored; 0 measured; 0 filtered out\n"
                   "FAIL: <unnamed> :: ir_tests/x.st (9 of 377)\n"
                   "  Passed           : 362 (96.02%)\n  Failed           :   1 (0.27%)\n", encoding="utf-8")
    summary = validation.summarize_log("suite", log)
    assert summary["pytest"] == {"passed": 159, "subtests passed": 6}
    assert summary["cargo"] == {"suites": 2, "passed": 95, "failed": 1, "ignored": 2, "failed_suites": 1}
    assert summary["lit"] == {"Passed": 362, "Failed": 1, "failed_tests": ["ir_tests/x.st"]}


def test_ax_member_offsets_are_unsupported_but_located_io_is_not() -> None:
    text = ("TYPE L : STRUCT\n a AT %B0 : ARRAY[0..1] OF BYTE;\n b AT %X2.1 : BOOL;\nEND_STRUCT; END_TYPE\n"
            "PROGRAM P\nVAR i AT %IX0.1 : BOOL; END_VAR\ni := TRUE;\nEND_PROGRAM\n")
    tokens = tokenize_lossless(text)
    found = unsupported.detect(significant(tokens), split_units(text, tokens))
    assert [(f["construct"], f["line"]) for f in found] == [("AT member offset", 2), ("AT member offset", 3)]


def test_culprit_search_without_candidates_finds_none() -> None:
    from compiler.intake.build import _culprit_prefix

    # Every unit is already excluded and the error persists: it comes from
    # the compatibility sources, not from a unit.
    assert _culprit_prefix({}, {}, {}, [], lambda: True, 0) is None


def test_scl_case_else_colon_and_dword_shift_literal_decline_outside_their_premise() -> None:
    from compiler.intake.normalize.rules.scl import CaseElseColon, ShiftLiteralDword

    case = "FUNCTION F : INT\nCASE F OF\n1: IF TRUE THEN F := 1; ELSE F := 2; END_IF;\nELSE:\nF := 3;\nEND_CASE;\nEND_FUNCTION\n"
    proposal = _propose(CaseElseColon(), case, 4, 5)
    assert [(case[e.start:e.end], e.replacement) for e in proposal.edits] == [(":", "(* : *)")]
    in_if = "FUNCTION F : INT\nIF TRUE THEN F := 1;\nELSE:\nF := 3;\nEND_IF;\nEND_FUNCTION\n"
    assert _propose(CaseElseColon(), in_if, 3, 5) is None
    shift = "FUNCTION F : DWORD\nF := SHR(IN:=(16#FFFFFFFF),N:=4) OR SHL(IN:=16#FF,N:=1);\nEND_FUNCTION\n"
    proposal = _propose(ShiftLiteralDword(), shift, 2, 6)
    assert [(e.start, e.replacement) for e in proposal.edits] == [(shift.index("16#FFFFFFFF"), "DWORD#")]
    small = "FUNCTION F : DWORD\nF := SHL(IN:=(1),N:=4) OR SHL(IN:=16#FFFF,N:=1);\nEND_FUNCTION\n"
    assert _propose(ShiftLiteralDword(), small, 2, 6) is None


def test_manifest_prefers_the_accepted_sources(tmp_path: Path) -> None:
    import json

    from compiler.intake.build import BuildEvidence
    from compiler.intake.library import write_accepted
    from compiler.toolchain.compatibility import load_project

    evidence = BuildEvidence([], [], None, None, accepted_texts={"lib.st": "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n"})
    assert write_accepted(evidence, tmp_path) == ["accepted/lib.st"]
    (tmp_path / "normalized").mkdir()
    (tmp_path / "normalized" / "lib.st").write_text("FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
    library = {"sources": ["normalized/lib.st"], "normalized_sources": ["normalized/lib.st"],
               "compatibility_sources": []}
    manifest = tmp_path / "compatibility.json"
    for accepted, expected in (([], "normalized/lib.st"), (["accepted/lib.st"], "accepted/lib.st")):
        manifest.write_text(json.dumps({"schema_version": "semantist.compatibility/1.1.0",
                                        "library": {**library, "accepted_sources": accepted}}), encoding="utf-8")
        assert load_project(manifest).library_sources == ((tmp_path / expected).resolve(),)


def test_manifest_build_includes_explicit_library_and_stubs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys

    from compiler.instrumentation.compile import compile_st_project_main

    target = tmp_path / "target.st"
    target.write_text("FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
    manifest_source = tmp_path / "accepted.st"
    manifest_source.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    library = tmp_path / "extra_library.st"
    stubs = tmp_path / "extra_stubs.st"
    library.write_text("FUNCTION G : INT\nG := 2;\nEND_FUNCTION\n", encoding="utf-8")
    stubs.write_text("FUNCTION H : INT\nEND_FUNCTION\n", encoding="utf-8")
    manifest = tmp_path / "compatibility.json"
    manifest.write_text(json.dumps({"schema_version": "semantist.compatibility/1.1.0",
                                    "library": {"accepted_sources": ["accepted.st"]}}), encoding="utf-8")
    plc = tmp_path / "plc"
    plc.write_text("", encoding="utf-8")
    output = tmp_path / "target.ll"
    work = tmp_path / "work"
    monkeypatch.setattr(sys, "argv", ["compile_st_project", str(target), "F", "--plc", str(plc),
                                     "--compatibility-manifest", str(manifest), "--library", str(library),
                                     "--stubs", str(stubs), "--out-ll", str(output), "--work-dir", str(work)])

    def fake_run(command, **kwargs):
        output.write_text("; test IR\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compile_st_project_main() == 0
    config = json.loads((work / "plc.json").read_text(encoding="utf-8"))
    assert config["files"][1:] == [str(manifest_source), str(library), str(stubs)]


def test_header_semicolon_only_before_a_var_block() -> None:
    from compiler.intake.normalize.rules.codesys import HeaderSemicolon

    method = "FUNCTION_BLOCK B\nMETHOD M : LREAL;\nVAR_INPUT x : LREAL; END_VAR\nM := x;\nEND_METHOD\nEND_FUNCTION_BLOCK\n"
    proposal = _propose(HeaderSemicolon(), method, 3, 1)
    assert [(method[e.start:e.end], e.replacement) for e in proposal.edits] == [(";", "(* ; *)")]
    # A semicolon ending a declaration inside a VAR block is not a header's.
    declarations = "FUNCTION F : INT\nVAR a : INT; END_VAR\nVAR b : INT; END_VAR\nF := a;\nEND_FUNCTION\n"
    assert _propose(HeaderSemicolon(), declarations, 3, 1) is None
    statements = "FUNCTION F : INT;\nF := 1;\nEND_FUNCTION\n"
    assert _propose(HeaderSemicolon(), statements, 2, 1) is None
