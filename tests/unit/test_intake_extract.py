import io
import json
import zipfile
from pathlib import Path

from compiler.intake.extract import naming
from compiler.intake.extract.cli import apply_curation, header_source
from compiler.intake.extract.compare import normalize_reference
from compiler.intake.extract.inventory import classify
from compiler.intake.extract.pou_index import ContainerReader, _declaration_positions, units


def test_collision_check_ignores_case(tmp_path: Path) -> None:
    (tmp_path / "native_OSCAT160.st").write_text("x", encoding="utf-8")
    assert naming.exists(tmp_path / "native_oscat160.st")
    assert not naming.exists(tmp_path / "native_oscat161.st")


def test_reference_headers_lose_only_the_windows_root() -> None:
    text = (
        "(* Original library/project: D:\\IIOT\\projects\\rusty兼容层\\02_CODESYS23\\原始库文件\\源码\\a.zip!b/c.lib *)\n"
        "FUNCTION F : INT\nEND_FUNCTION\n"
    )
    normalized, count = normalize_reference(text)
    assert count == 1
    assert normalized.splitlines()[0] == "(* Original library/project: 02_CODESYS23/原始库文件/源码/a.zip!b/c.lib *)"
    assert normalized.splitlines()[1:] == text.splitlines()[1:]


def _legacy_output(folder: Path, name: str, source: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"(* Original library/project: {source} *)\n{body}", encoding="utf-8")
    return path


def test_curation_replays_merges_renames_and_headers(tmp_path: Path) -> None:
    folder = tmp_path / "02_CODESYS23" / ".st文件"
    _legacy_output(folder, "native_A.st", "x.zip!A.EXP", "FUNCTION A : INT\nEND_FUNCTION\n")
    _legacy_output(folder, "native_B.st", "x.zip!B.EXP", "FUNCTION B : INT\nEND_FUNCTION\n")
    _legacy_output(folder, "native_oscat_1234abcd.st", "y.zip!rel270/oscat.pro", "FUNCTION C : INT\nEND_FUNCTION\n")
    curation = {
        "merges": [{"vendor": "02_CODESYS23", "name": "native_patch.st", "header": "(* collection *)",
                    "members": ["x.zip!A.EXP", "x.zip!B.EXP"], "reason": "manual merge"}],
        "renames": [{"vendor": "02_CODESYS23", "source": "y.zip!rel270/oscat.pro",
                     "name": "native_oscat_basic_270.st", "reason": "manual rename"}],
        "headers": [{"vendor": "02_CODESYS23", "name": "native_oscat_basic_270.st", "source": "y.zip!rel270/oscat.pro",
                     "header_source": "y.zip!rel270", "reason": "refined"}],
    }
    applied = apply_curation(tmp_path, curation)

    assert [step["action"] for step in applied] == ["merge", "rename", "header"]
    assert sorted(p.name for p in folder.iterdir()) == ["native_oscat_basic_270.st", "native_patch.st"]
    merged = (folder / "native_patch.st").read_text(encoding="utf-8")
    assert merged.startswith("(* collection *)\n(* Original library/project: x.zip!A.EXP *)\n")
    assert "(* Original library/project: x.zip!B.EXP *)" in merged
    assert header_source(folder / "native_oscat_basic_270.st") == "y.zip!rel270"


def test_unit_scan_tracks_markers_members_and_namespaces() -> None:
    text = """(* Source: lib/a.st *)
NAMESPACE Vendor.Tools
CLASS PUBLIC Counter
  METHOD PUBLIC Increment
  END_METHOD
END_CLASS
END_NAMESPACE
(* Object: FB_X *)
FUNCTION_BLOCK FB_X (* END_FUNCTION_BLOCK in a comment *)
METHOD Run : BOOL
END_METHOD
END_FUNCTION_BLOCK
TYPE E_Mode : (Off, On); END_TYPE
"""
    found = units(text)
    assert [(u["kind"], u["name"], u["marker"]) for u in found] == [
        ("CLASS", "Vendor.Tools.Counter", "lib/a.st"),
        ("FUNCTION_BLOCK", "FB_X", "FB_X"),
        ("TYPE", "E_Mode", "FB_X"),
    ]
    assert found[0]["members"] == [{"kind": "METHOD", "name": "Increment", "line": 4}]
    assert found[1]["line"] == 9 and found[1]["end_line"] == 12


def test_container_chains_read_nested_archives(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("codesys/IP_CONTROL.EXP", "(* x *)\nFUNCTION_BLOCK IP_CONTROL\nEND_FUNCTION_BLOCK\n")
    (tmp_path / "vendor").mkdir()
    with zipfile.ZipFile(tmp_path / "vendor" / "outer.zip", "w") as archive:
        archive.writestr("patch/inner.zip", inner.getvalue())
    reader = ContainerReader(tmp_path)

    data = reader.read("vendor/outer.zip!patch/inner.zip!codesys/IP_CONTROL.EXP")
    assert _declaration_positions(data) == {("FUNCTION_BLOCK", "ip_control"): {"line": 2}}
    assert reader.read("vendor/outer.zip!missing.EXP") is None


def test_binary_records_are_located_by_offset() -> None:
    data = b"CoDeSys+\x00\x02\x00\x10\x00\x00\x00FUNCTION F1 : INT\nVAR_INPUT"
    assert _declaration_positions(data) == {("FUNCTION", "f1"): {"offset": data.index(b"FUNCTION")}}


def test_library_format_classification() -> None:
    assert classify(b"PK\x03\x04rest") == "zip"
    assert classify(bytes.fromhex("2389ed33") + b"\x00" * 8) == "compressed_or_encrypted"
    assert classify(b"<?xml") == "unknown"


def test_curation_file_is_complete() -> None:
    curation = json.loads(Path("compiler/intake/extract/curation.json").read_text(encoding="utf-8"))
    for section in ("renames", "merges", "headers", "fixed_inputs"):
        assert curation[section], section
        assert all(item["reason"] for item in curation[section])
