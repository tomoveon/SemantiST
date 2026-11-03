"""Rule probes: evidence that each rule is needed and what it produces.

Every probe directory holds ``original.st``, ``probe.json`` and the
``rewritten.st`` the engine must produce from the original with the probe's
dialect profile. A probe passes when

1. the pinned RuSTy rejects ``original.st`` with one of ``reject_codes`` (for
   representation rewrites: accepts it, and running it prints
   ``original_output``, showing the meaning RuSTy gives it);
2. the engine turns ``original.st`` into exactly ``rewritten.st``, applying the
   probe's rule;
3. the pinned RuSTy accepts ``rewritten.st``; and, with ``expected_output``,
   running it prints the vendor-documented result.

``legacy.json`` records what became of each rule of the earlier regex
converter and of constructs it did not cover; each entry's source must be
accepted or rejected by the pinned RuSTy as the entry states, and the rule or
unsupported construct that takes over must exist (with its own probe).

``python3 -m compiler.intake.normalize.probes [--update] [--json PATH]`` runs
all probes and legacy checks (``--update`` rewrites ``rewritten.st`` from the
engine; review the diff; ``--json`` writes the results).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

from .. import rusty, unsupported
from ..build import LLVM_BIN, internalize
from ..structure import split_units
from .engine import normalize
from .profiles import load_dialect
from .rules import RULES, rules_for

HERE = Path(__file__).resolve().parent / "probes"
LEGACY = Path(__file__).resolve().parent / "legacy.json"
PRINTF = HERE / "printf.pli"
ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "compiler" / "toolchain" / "compatibility" / "profiles"


def probe_names() -> list[str]:
    return sorted(p.name for p in HERE.iterdir() if (p / "probe.json").is_file())


def _profile_sources(names: list[str]) -> tuple[list[Path], dict[str, str]]:
    sources, rebind = [], {}
    for name in names:
        profile = json.loads((PROFILES / name / "profile.json").read_text(encoding="utf-8"))
        sources += [PROFILES / name / item for item in profile.get("st_sources", [])]
        rebind.update(profile.get("rebind", {}))
    return sources, rebind


def run_program(source: str, extra_sources: list[Path], compiler_args: list[str]) -> tuple[int, str]:
    """Compile ``source`` with printf and the stdlib, link it against the
    static runtime archive (which holds the standard blocks' constructors) and
    run it."""
    with tempfile.TemporaryDirectory(prefix="semantist-probe-run-") as directory:
        path = Path(directory) / "probe.st"
        path.write_text(source, encoding="utf-8")
        ir = Path(directory) / "probe.ll"
        executable = Path(directory) / "probe"
        build = rusty.check([path, *extra_sources], ir=ir, extra=compiler_args)
        if not build.ok or not ir.exists():
            return -1, build.output[-2000:]
        public = Path(directory) / "probe.public.ll"
        if internalize(ir, public, ["main"]).returncode == 0:
            ir = public
        link = subprocess.run([str(LLVM_BIN / "clang"), "-Wno-override-module", str(ir),
                               str(rusty.runtime_archive()), "-ldl",
                               "-lpthread", "-lm", "-o", str(executable)], capture_output=True, text=True)
        if link.returncode:
            return -1, link.stderr[-2000:]
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout


def run_probe(name: str, update: bool = False) -> dict:
    directory = HERE / name
    meta = json.loads((directory / "probe.json").read_text(encoding="utf-8"))
    original = (directory / "original.st").read_text(encoding="utf-8")
    dialect = load_dialect(meta["dialect"])
    compiler_args = list(dialect.get("compiler_args", []))
    sources, rebind = _profile_sources(meta.get("profiles", []))
    failures = []
    sources = [PRINTF, *sources]
    # A probe with "reject_stage": "codegen" is rejected only when LLVM IR is
    # generated (the original type checks).
    original_paths = _write_tmp(original)
    ir = original_paths[0].with_suffix(".ll") if meta.get("reject_stage") == "codegen" else None
    first = rusty.check([*original_paths, *sources], extra=compiler_args, ir=ir)
    codes = sorted({d.code for d in first.errors})
    if meta.get("reject_codes"):
        if not set(codes) & set(meta["reject_codes"]):
            failures.append(f"original not rejected with {meta['reject_codes']} (got {codes})")
    elif codes or first.panic:
        failures.append(f"original expected to compile, got {codes} {first.panic or ''}")
    if "original_output" in meta:
        code, output = run_program(original, sources, compiler_args)
        if output != meta["original_output"]:
            failures.append(f"original printed {output!r}, expected {meta['original_output']!r}")
    with tempfile.TemporaryDirectory(prefix="semantist-probe-norm-") as work:
        result = normalize({"probe.st": original}, dialect, {}, rules_for(dialect), Path(work), sources,
                           compiler_args, rebind)
    rewritten = result.texts()["probe.st"]
    applied = sorted({t["rule_id"] for t in result.transformations})
    if meta["rule"] not in applied:
        failures.append(f"rule {meta['rule']} not applied (applied: {applied})")
    expected_path = directory / "rewritten.st"
    if update:
        expected_path.write_text(rewritten, encoding="utf-8")
    elif not expected_path.is_file() or expected_path.read_text(encoding="utf-8") != rewritten:
        failures.append("engine output differs from rewritten.st")
    if not result.type_checks:
        failures.append(f"rewritten source still has errors: {[e['code'] for e in result.remaining]}")
    if "expected_output" in meta:
        code, output = run_program(rewritten, sources, compiler_args)
        if output != meta["expected_output"]:
            failures.append(f"rewritten printed {output!r}, expected {meta['expected_output']!r}")
    return {"probe": name, "rule": meta["rule"], "rule_version": RULES[meta["rule"]].spec.version
            if meta["rule"] in RULES else None, "original_codes": codes, "applied": applied,
            "passed": not failures, "failures": failures}


def legacy_entries() -> list[dict]:
    return json.loads(LEGACY.read_text(encoding="utf-8"))["entries"]


def _probed_rules() -> set[str]:
    return {json.loads((HERE / name / "probe.json").read_text(encoding="utf-8"))["rule"] for name in probe_names()}


def run_legacy(entry: dict) -> dict:
    failures = []
    result = rusty.check([*_write_tmp(entry["source"]), PRINTF])
    codes = sorted({d.code for d in result.errors})
    if result.panic:
        failures.append(f"compiler panic: {result.panic}")
    if entry["expect"] == "accepted" and codes:
        failures.append(f"expected to compile, got {codes}")
    if entry["expect"] == "rejected" and not set(codes) & set(entry["codes"]):
        failures.append(f"expected rejection with {entry['codes']}, got {codes}")
    handler = entry.get("handled_by")
    if handler and handler.startswith("unsupported:"):
        tokens = tokenize_lossless(entry["source"])
        found = {f["construct"] for f in unsupported.detect(significant(tokens), split_units(entry["source"], tokens))}
        if handler.split(":", 1)[1] not in found:
            failures.append(f"{handler} not detected (found {sorted(found)})")
    elif handler and (handler not in RULES or handler not in _probed_rules()):
        failures.append(f"rule {handler} does not exist or has no probe")
    if "expected_output" in entry:
        _, output = run_program(entry["source"], [PRINTF], [])
        if output != entry["expected_output"]:
            failures.append(f"printed {output!r}, expected {entry['expected_output']!r}")
    return {"legacy": entry["id"], "name": entry["name"], "disposition": entry["disposition"],
            "handled_by": handler, "codes": codes, "passed": not failures, "failures": failures}


def _write_tmp(text: str) -> list[Path]:
    directory = Path(tempfile.mkdtemp(prefix="semantist-probe-src-"))
    path = directory / "probe.st"
    path.write_text(text, encoding="utf-8")
    return [path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    results = [run_probe(name, args.update) for name in (args.names or probe_names())]
    legacy = [] if args.names else [run_legacy(entry) for entry in legacy_entries()]
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {result['probe']}: {'; '.join(result['failures'])}")
    for result in legacy:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} legacy {result['legacy']} ({result['disposition']}): {'; '.join(result['failures'])}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"toolchain": rusty.toolchain_identity(), "probes": results, "legacy": legacy},
                                        ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if all(r["passed"] for r in results + legacy) else 1


if __name__ == "__main__":
    raise SystemExit(main())
