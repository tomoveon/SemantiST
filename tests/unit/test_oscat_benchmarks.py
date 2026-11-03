"""The default benchmark index covers the requested OSCAT targets."""

from collections import Counter
from pathlib import Path
import shlex
import subprocess

from compiler.scripts.genfunction import extract_pou
from fuzzer.cli.batch import _benchmark_targets


def test_requested_oscat_targets_are_extracted_and_scheduled() -> None:
    targets = _benchmark_targets()
    assert len(targets) == 100
    assert len({target["id"] for target in targets}) == 100
    assert Counter(target["suite"] for target in targets) == {
        "oscat_basic": 80,
        "oscat_network": 10,
        "oscat_building": 10,
    }
    assert {
        "INC2", "_BUFFER_CLEAR", "_BUFFER_INIT", "_STRING_TO_BUFFER",
        "WEEKDAY_TO_STRING", "REAL_TO_STRF", "INC", "DIR_TO_DEG",
        "_BUFFER_INSERT", "SET_DATE", "MONTH_END", "FSTRING_TO_MONTH",
        "DEG_TO_DIR", "SCHEDULER_2", "BUFFER_SEARCH", "BUFFER_COMP",
        "DELAY", "SH_2", "SEQUENCE_64", "HOLIDAY", "NET_VAR_BUFFER",
        "IRTRANS_DECODE", "XML_READER", "SMTP_CLIENT", "DLOG_STORE_MYSQL",
        "TIMER_P4", "FILTER_MAV_DW",
    } <= {target["function"] for target in targets}
    for target in targets:
        path = Path(target["st_file"])
        assert path.is_file()
        name, kind, _body = extract_pou(path.read_text(encoding="utf-8"), target["function"])
        assert (name, kind) == (target["function"], target["manifest"]["kind"])


def test_seven_target_hybrid_script_selects_only_its_manifest(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = root / "benchmarks/oscat/seven_targets.json"
    targets = _benchmark_targets(manifest)
    assert len(targets) == len({target["id"] for target in targets}) == 7
    for target in targets:
        source = Path(target["st_file"])
        assert source.is_file()
        name, kind, _body = extract_pou(source.read_text(encoding="utf-8"), target["function"])
        assert (name, kind) == (target["function"], target["manifest"]["kind"])

    script = root / "benchmarks/oscat/run_seven_hybrid_llm.sh"
    result = subprocess.run([str(script), "--dry-run", str(tmp_path / "run")],
                            check=True, capture_output=True, text=True)
    command = shlex.split(result.stdout)
    assert command[command.index("--benchmark-manifest") + 1] == str(manifest)
    assert command[command.index("--time-per-target") + 1] == "600"
    assert "--switch=--full-hybrid-duration" in command
    assert "--switch=--llm" in command
    assert "--switch=--symbolic-start=import" in command
    assert "--switch=--full-fuzz-duration" not in command

    changed = subprocess.run([str(script), "--dry-run", "--time-per-target", "900",
                              "--build-overhead", "240"],
                             check=True, capture_output=True, text=True)
    changed_command = shlex.split(changed.stdout)
    assert changed_command[changed_command.index("--time-per-target") + 1] == "900"
    assert changed_command[changed_command.index("--build-overhead") + 1] == "240"
    assert changed_command[changed_command.index("--out") + 1].endswith(
        "artifacts/batch/oscat-seven-hybrid-llm-900s"
    )
    invalid = subprocess.run([str(script), "--dry-run", "--time-per-target", "0"],
                             capture_output=True, text=True)
    assert invalid.returncode == 2
