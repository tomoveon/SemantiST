"""STG entrypoints must not silently run stale cached release binaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script,binary", [
    ("generate_stg.sh", "semantist-stg"),
    ("instrument_stg_ir.sh", "stg-ir-instrument"),
])
def test_stg_script_checks_release_build_before_exec(tmp_path: Path, script: str, binary: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    release = tmp_path / "target" / "release"
    release.mkdir(parents=True)
    cached = release / binary
    cached.write_text("#!/bin/sh\nprintf 'stale\\n'\n", encoding="ascii")
    cached.chmod(0o755)
    fresh = tmp_path / "fresh"
    fresh.write_text("#!/bin/sh\nprintf 'fresh:%s\\n' \"$1\"\n", encoding="ascii")
    fresh.chmod(0o755)
    cargo = bin_dir / "cargo"
    cargo.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$STG_TEST_LOG\"\n"
        "cp \"$STG_TEST_FRESH\" \"$CARGO_TARGET_DIR/release/$STG_TEST_BINARY\"\n",
        encoding="ascii",
    )
    cargo.chmod(0o755)
    log = tmp_path / "cargo.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "CARGO_TARGET_DIR": str(tmp_path / "target"),
           "STG_TEST_LOG": str(log), "STG_TEST_FRESH": str(fresh),
           "STG_TEST_BINARY": binary}
    env.pop("SEMANTIST_STG_BIN", None)
    env.pop("SEMANTIST_STG_IR_INSTRUMENT_BIN", None)

    result = subprocess.run([str(ROOT / "compiler" / "scripts" / script), "--ping"],
                            env=env, capture_output=True, text=True, check=True)

    assert result.stdout == "fresh:--ping\n"
    arguments = log.read_text(encoding="ascii").splitlines()
    assert arguments[0] == "build"
    assert "--release" in arguments
    assert binary in arguments
