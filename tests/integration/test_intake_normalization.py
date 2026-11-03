"""Diagnostic-driven normalization through prepare_library.py (needs the pinned RuSTy)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from compiler.intake import rusty

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "compiler/parser/st-to-rusty-converter/scripts/prepare_library.py"

pytestmark = pytest.mark.skipif(not rusty.plc_path().exists(), reason="pinned RuSTy compiler is required")

LIBRARY = """(* OVERRIDE in this comment and the string below stay unchanged *)
FUNCTION OVERRIDE : REAL
VAR_INPUT CONSTANT
    X1, X2 : REAL;
END_VAR
VAR
    label : STRING := 'OVERRIDE';
END_VAR
OVERRIDE := X1;
IF ABS(X2) > ABS(OVERRIDE) THEN OVERRIDE := X2; END_IF;
END_FUNCTION

FUNCTION USER : REAL
USER := OVERRIDE(1.0, 2.0);
END_FUNCTION

METHOD ORPHAN : INT
ORPHAN := 1;
END_METHOD
"""


def prepare(tmp_path: Path, name: str) -> Path:
    source = tmp_path / "lib.st"
    source.write_text(LIBRARY, encoding="utf-8")
    output = tmp_path / name
    completed = subprocess.run(
        [sys.executable, str(PREPARE), str(source), "--dialect", "codesys", "--output-dir", str(output)],
        cwd=ROOT, check=False, capture_output=True,
    )
    manifest = json.loads((output / "compatibility.json").read_text(encoding="utf-8"))
    expected = {"READY": 0, "APPROXIMATE": 0, "UNSUPPORTED": 2, "FAILED": 1}
    assert completed.returncode == expected[manifest["normalization_status"]], completed.stderr.decode()
    return output


def test_rules_rewrite_only_code_and_record_every_edit(tmp_path: Path) -> None:
    output = prepare(tmp_path, "intake")
    text = (output / "lib.st").read_text(encoding="utf-8")
    record = json.loads((output / "transformations.json").read_text(encoding="utf-8"))

    assert "(* OVERRIDE in this comment and the string below stay unchanged *)" in text
    assert "'OVERRIDE'" in text
    assert "FUNCTION OVERRIDE__ : REAL" in text and "USER := OVERRIDE__(1.0, 2.0);" in text
    assert "VAR_INPUT (* CONSTANT *)" in text
    assert record["renames"] == {"OVERRIDE": "OVERRIDE__"}
    assert {t["rule_id"] for t in record["transformations"]} >= {"keyword-identifier-rename", "var-block-constant"}
    assert all(t["rule_version"] >= 1 and t["trigger"] for t in record["transformations"] if t["round"] is not None)
    sourcemap = json.loads((output / "lib.st.sourcemap.json").read_text(encoding="utf-8"))
    assert len(sourcemap["line_map"]) == text.count("\n") + 1


def test_unknown_constructs_are_reported_not_converted(tmp_path: Path) -> None:
    output = prepare(tmp_path, "intake")
    text = (output / "lib.st").read_text(encoding="utf-8")
    record = json.loads((output / "transformations.json").read_text(encoding="utf-8"))

    assert "METHOD ORPHAN : INT" in text
    remaining = [e for e in record["remaining_errors"] if e["unit"] == "ORPHAN"]
    assert remaining and all(e["line"] and e["code"] for e in remaining)


def test_normalization_is_deterministic(tmp_path: Path) -> None:
    first, second = prepare(tmp_path, "first"), prepare(tmp_path, "second")
    for name in ("lib.st", "lib.st.sourcemap.json", "transformations.json", "compatibility.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
