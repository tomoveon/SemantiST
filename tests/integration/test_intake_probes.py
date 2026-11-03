"""Rule probes and generated profiles against the pinned RuSTy toolchain."""

import subprocess
import sys

import pytest

from compiler.intake import rusty
from compiler.intake.normalize import probes

pytestmark = pytest.mark.skipif(
    not rusty.plc_path().exists() or not rusty.stdlib_sources(),
    reason="pinned RuSTy compiler and standard library are required",
)


@pytest.mark.parametrize("name", probes.probe_names())
def test_rule_probe(name: str) -> None:
    result = probes.run_probe(name)
    assert result["passed"], result["failures"]


@pytest.mark.parametrize("entry", probes.legacy_entries(), ids=lambda e: e["id"])
def test_legacy_rule_disposition(entry: dict) -> None:
    result = probes.run_legacy(entry)
    assert result["passed"], result["failures"]


def test_every_dialect_rule_has_a_probe() -> None:
    from compiler.intake.normalize.profiles import DIALECTS, load_dialect

    probed = {probes.json.loads((probes.HERE / name / "probe.json").read_text())["rule"]
              for name in probes.probe_names()}
    used = {rule for path in DIALECTS.glob("*.json") for rule in load_dialect(path.stem)["rules"]}
    assert used <= probed, sorted(used - probed)


@pytest.mark.parametrize("generator", ["generate_codesys.py", "generate_s7.py"])
def test_generated_profiles_match_the_pinned_stdlib(generator: str) -> None:
    script = probes.PROFILES / generator
    result = subprocess.run([sys.executable, str(script), "--stdlib", str(rusty.stdlib_dir()), "--check"],
                            capture_output=True, text=True, cwd=probes.PROFILES)
    assert result.returncode == 0, result.stderr
