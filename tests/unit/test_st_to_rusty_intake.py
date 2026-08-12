import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "compiler/parser/st-to-rusty-converter/scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stubs_analyzer = load_module("intake_stubs_analyzer", SCRIPTS / "stubs_analyzer.py")


def test_standard_library_wins_over_conflicting_stub(tmp_path: Path) -> None:
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    (stdlib / "standard.st").write_text("FUNCTION LEN : DINT\nEND_FUNCTION\n", encoding="utf-8")
    stubs = tmp_path / "stubs.st"
    stubs.write_text("FUNCTION LEN : DINT\nLEN := 0;\nEND_FUNCTION\n", encoding="utf-8")

    inventory = stubs_analyzer.Inventory.load(stdlib, None, None, stubs)
    report = stubs_analyzer.StubsAnalyzer(inventory).to_dict()

    assert inventory.resolve("LEN") == ("rusty_iec_st_library", "provided")
    assert report["conflicting_stub_symbols"]["rusty_iec_st_library"] == ["LEN"]
    assert report["gate"]["status"] == "blocked"


def test_date_to_dword_is_not_a_compiler_intrinsic() -> None:
    inventory = stubs_analyzer.Inventory()
    assert "DATE_TO_DWORD" not in stubs_analyzer.COMPILER_INTRINSICS
    assert inventory.resolve("DATE_TO_DWORD") == ("none", "unresolved")


def test_linker_failure_marks_unprovided_declaration_unresolved(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs.st"
    stubs.write_text("@EXTERNAL FUNCTION VENDOR_IO : DINT\nEND_FUNCTION\n", encoding="utf-8")
    analyzer = stubs_analyzer.StubsAnalyzer(
        stubs_analyzer.Inventory.load(None, None, None, stubs)
    )
    analyzer.parse("target.o: undefined reference to `VENDOR_IO'")
    report = analyzer.to_dict()

    assert report["gate"]["status"] == "blocked"
    assert report["classifications"]["unresolved"] == ["VENDOR_IO"]


def test_unknown_function_never_generates_default_implementation(tmp_path: Path) -> None:
    output = tmp_path / "dependency-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "stubs_analyzer.py"),
            "-",
            "--output",
            str(output),
        ],
        input="error[E048]: Could not resolve reference to MYSTERY_CALL\n",
        text=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["classifications"]["unresolved"] == ["MYSTERY_CALL"]
    assert list(tmp_path.glob("*.st")) == []


def test_converter_preserves_original_comments_and_strings(tmp_path: Path) -> None:
    source = tmp_path / "abc.st"
    original = "(* SET must stay in this comment *)\nFUNCTION F : BOOL\nVAR SET : BOOL; END_VAR\nF := SET;\nEND_FUNCTION\n"
    source.write_text(original, encoding="utf-8")
    manifest = tmp_path / "transformations.json"
    output = tmp_path / "derived"
    subprocess.run(
        [sys.executable, str(SCRIPTS / "st_analyzer.py"), str(source), "--dialect", "codesys", "-o", str(manifest)],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(SCRIPTS / "st_converter.py"), str(manifest), "--output-dir", str(output)],
        check=True,
    )

    converted = (output / "abc.st").read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == original
    assert "(* SET must stay in this comment *)" in converted
    assert "VAR SET0 : BOOL" in converted
    assert "F := SET0" in converted


def test_unproven_method_conversion_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "method.st"
    source.write_text("METHOD RUN : DINT\nRUN := 1;\nEND_METHOD\n", encoding="utf-8")
    manifest = tmp_path / "transformations.json"
    output = tmp_path / "derived"
    subprocess.run(
        [sys.executable, str(SCRIPTS / "st_analyzer.py"), str(source), "--dialect", "codesys", "-o", str(manifest)],
        check=True,
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "st_converter.py"), str(manifest), "--output-dir", str(output)],
        text=True,
        capture_output=True,
        check=False,
    )

    audit = json.loads(manifest.read_text(encoding="utf-8"))
    assert result.returncode != 0
    assert audit["unsupported_constructs"][0]["original_text"] == "METHOD"
    assert not output.exists()


def test_harness_has_no_standard_or_vendor_compatibility_fallbacks() -> None:
    source = (ROOT / "compiler/harness/generator.py").read_text(encoding="utf-8")
    for symbol in ("LEN__STRING", "MIN__DINT", "DATE_TO_DWORD", "TIME_TO_DWORD"):
        assert f"weak)) {symbol}" not in source
    assert not (ROOT / "compiler/harness/iec_compat.c").exists()


def test_harness_does_not_embed_pointer_canary_oracles() -> None:
    source = (ROOT / "compiler/harness/generator.py").read_text(encoding="utf-8")
    assert "SEMANTIST_POINTER_CANARY" not in source
    assert "check_pointer_canary" not in source


def test_prepare_directory_writes_multi_source_compatibility_manifest(tmp_path: Path) -> None:
    source = tmp_path / "library"
    source.mkdir()
    (source / "first.st").write_text(
        "FUNCTION FIRST : DINT\nFIRST := 1;\nEND_FUNCTION\n", encoding="utf-8"
    )
    (source / "second.st").write_text(
        "FUNCTION SECOND : DINT\nSECOND := 2;\nEND_FUNCTION\n", encoding="utf-8"
    )
    output = tmp_path / "intake"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_library.py"),
            str(source),
            "--dialect",
            "generic",
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )

    manifest = json.loads((output / "compatibility.json").read_text(encoding="utf-8"))
    assert manifest["library"]["normalized_sources"] == ["first.st", "second.st"]
    assert manifest["library"]["compatibility_sources"] == ["stubs.st"]


def test_prepare_selects_reusable_compatibility_profile(tmp_path: Path) -> None:
    source = tmp_path / "library.st"
    source.write_text("FUNCTION F : DINT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
    output = tmp_path / "intake"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_library.py"),
            str(source),
            "--dialect",
            "codesys",
            "--profile",
            "codesys-memory",
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )

    manifest = json.loads((output / "compatibility.json").read_text(encoding="utf-8"))
    assert manifest["profiles"] == ["codesys-memory"]
    assert "compatibility/codesys-memory/interfaces.st" in manifest["library"]["sources"]
    assert manifest["native"]["sources"] == ["compatibility/codesys-memory/runtime.c"]
