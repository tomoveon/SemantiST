#!/usr/bin/env python3
"""Compile, generate IR, and native-link selected POUs from an intake workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compiler.toolchain.compatibility import load_project
from compiler.toolchain.compatibility.native import build_native_inputs
from stubs_analyzer import Inventory, StubsAnalyzer


DEFAULT_STDLIB = ROOT / "artifacts/rusty-semantic/libs/stdlib/iec61131-st"
DEFAULT_RUNTIME = ROOT / "artifacts/cargo-target/release/libiec61131std.a"
DEFAULT_PLC = ROOT / "artifacts/rusty-semantic/target/release/plc"
DEFAULT_LLVM_BIN = Path("/usr/lib/llvm-21/bin")


def run_logged(command: list[str], log: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout, encoding="utf-8")
    return result


def ir_declarations(path: Path) -> set[str]:
    pattern = re.compile(r'^declare\b.*@(?:"([^"]+)"|([^\s(]+))\(', re.MULTILINE)
    text = path.read_text(encoding="utf-8", errors="replace")
    return {(match.group(1) or match.group(2)) for match in pattern.finditer(text)}


def pou_source(sources: list[Path], pou: str) -> Path:
    pattern = re.compile(rf"(?im)^\s*(FUNCTION|FUNCTION_BLOCK|PROGRAM)\s+{re.escape(pou)}\b")
    for source in sources:
        if pattern.search(source.read_text(encoding="utf-8", errors="replace")):
            return source
    raise ValueError(f"POU {pou} is not defined by the compatibility project")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--compatibility-manifest", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--stubs", type=Path)
    parser.add_argument("--pou", action="append", required=True, help="repeat for each link smoke target")
    parser.add_argument("--plc", type=Path, default=DEFAULT_PLC)
    parser.add_argument("--stdlib", type=Path, default=DEFAULT_STDLIB)
    parser.add_argument("--runtime-archive", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--native-object", type=Path, action="append", default=[])
    parser.add_argument("--environment-model", action="append", default=[])
    parser.add_argument("--llvm-bin", type=Path, default=DEFAULT_LLVM_BIN)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    project = None
    compatibility_manifest = args.compatibility_manifest.resolve() if args.compatibility_manifest else None
    if compatibility_manifest:
        try:
            project = load_project(compatibility_manifest)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        library_sources = list(project.library_sources)
        compatibility_sources = list(project.compatibility_sources)
        st_sources = list(project.st_sources)
        if project.stdlib_glob:
            stdlib_path = Path(project.stdlib_glob.removesuffix("/*.st"))
            if args.stdlib == DEFAULT_STDLIB:
                args.stdlib = stdlib_path
        if project.runtime_archive and args.runtime_archive == DEFAULT_RUNTIME:
            args.runtime_archive = project.runtime_archive
    else:
        if args.library is None or args.stubs is None:
            raise SystemExit("provide --compatibility-manifest or both --library and --stubs")
        library_sources = [args.library.resolve()]
        compatibility_sources = [args.stubs.resolve()]
        st_sources = [*library_sources, *compatibility_sources]
    runtime = args.runtime_archive.resolve()
    logs = workspace / "validation"
    workspace.mkdir(parents=True, exist_ok=True)
    native_objects = [path.resolve() for path in args.native_object]
    if project:
        native_build_dir = workspace / "validation" / "compatibility-native"
        clang = args.llvm_bin.resolve() / "clang"
        native_inputs = build_native_inputs(project, native_build_dir, clang)
        native_objects.extend(
            Path(value)
            for value in native_inputs
            if not value.startswith("-")
        )
        native_link_args = [value for value in native_inputs if value.startswith("-")]
    else:
        native_link_args = []
    required = [*st_sources, args.plc.resolve(), args.stdlib.resolve(), runtime, *native_objects]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing validation inputs: " + ", ".join(missing))

    inventory = Inventory.load(
        args.stdlib,
        runtime,
        library_sources,
        compatibility_sources,
        args.environment_model,
        native_objects,
    )
    checks: dict[str, str] = {
        "parse_and_typecheck": "passed",
        "llvm_ir": "passed",
        "native_link": "passed",
        "reachable_externals": "passed",
        "audit_complete": "passed",
    }
    targets: list[dict[str, object]] = []
    diagnostics = ""
    declared_symbols: set[str] = set()

    for pou in args.pou:
        target_dir = logs / re.sub(r"[^A-Za-z0-9_.-]+", "_", pou.lower())
        target_dir.mkdir(parents=True, exist_ok=True)
        llvm_ir = target_dir / "library.ll"
        try:
            selected_source = pou_source(st_sources, pou)
        except ValueError as error:
            checks["parse_and_typecheck"] = "failed"
            checks["llvm_ir"] = "failed"
            checks["native_link"] = "not_run"
            diagnostics += str(error) + "\n"
            targets.append({"pou": pou, "compile": "failed", "link": "not_run"})
            continue
        compile_command = [
                sys.executable,
                "-m",
                "compiler.scripts.compile_st_project",
                str(selected_source),
                pou,
                "--out-ll",
                str(llvm_ir),
                "--plc",
                str(args.plc.resolve()),
                "--stdlib-glob",
                str(args.stdlib.resolve() / "*.st"),
                "--work-dir",
                str(target_dir / "project"),
            ]
        if compatibility_manifest:
            compile_command.extend(("--compatibility-manifest", str(compatibility_manifest)))
        else:
            compile_command.extend(
                ("--library", str(library_sources[0]), "--stubs", str(compatibility_sources[0]))
            )
        compile_result = run_logged(
            compile_command,
            target_dir / "compile.log",
        )
        if compile_result.returncode != 0 or not llvm_ir.exists():
            checks["parse_and_typecheck"] = "failed"
            checks["llvm_ir"] = "failed"
            checks["native_link"] = "not_run"
            checks["reachable_externals"] = "not_run"
            diagnostics += compile_result.stdout
            targets.append({"pou": pou, "compile": "failed", "link": "not_run"})
            continue

        declared_symbols.update(ir_declarations(llvm_ir))
        harness = target_dir / "harness.c"
        harness_env = os.environ.copy()
        harness_env["HARNESS_OUT"] = str(harness)
        harness_result = run_logged(
            [sys.executable, "-m", "compiler.scripts.generate_harness", str(selected_source), pou],
            target_dir / "harness.log",
            harness_env,
        )
        prepared = target_dir / "prepared.ll"
        prepare_result = run_logged(
            [
                sys.executable,
                "-m",
                "compiler.scripts.prepare_target_ir",
                str(llvm_ir),
                pou,
                "--output-ll",
                str(prepared),
                "--llvm-bin",
                str(args.llvm_bin.resolve()),
            ],
            target_dir / "prepare.log",
        )
        if harness_result.returncode != 0 or prepare_result.returncode != 0:
            checks["llvm_ir"] = "failed"
            checks["native_link"] = "not_run"
            diagnostics += harness_result.stdout + prepare_result.stdout
            targets.append({"pou": pou, "compile": "passed", "link": "not_run"})
            continue

        executable = target_dir / "smoke-executable"
        link_result = run_logged(
            [
                str(args.llvm_bin.resolve() / "clang"),
                "-O1",
                str(harness),
                str(prepared),
                str(runtime),
                *(str(path) for path in native_objects),
                *native_link_args,
                "-ldl",
                "-lpthread",
                "-lm",
                "-o",
                str(executable),
            ],
            target_dir / "link.log",
        )
        if link_result.returncode != 0 or not executable.exists():
            checks["native_link"] = "failed"
            diagnostics += link_result.stdout
            targets.append({"pou": pou, "compile": "passed", "link": "failed"})
        else:
            targets.append(
                {"pou": pou, "compile": "passed", "link": "passed", "executable": str(executable)}
            )

    unexplained: list[str] = []
    for symbol in sorted(declared_symbols):
        if symbol.startswith("llvm."):
            continue
        source, _classification = inventory.resolve(symbol)
        if source == "none":
            unexplained.append(symbol)
    if unexplained:
        checks["reachable_externals"] = "failed"
        diagnostics += "\n".join(f"undefined symbol: {name}" for name in unexplained)

    dependency_analyzer = StubsAnalyzer(inventory)
    dependency_analyzer.parse(diagnostics)
    dependency_report = dependency_analyzer.to_dict()
    if unexplained:
        dependency_report["gate"]["status"] = "blocked"
    elif (
        dependency_report["gate"]["status"] != "blocked"
        and checks["native_link"] == "passed"
        and checks["parse_and_typecheck"] == "passed"
    ):
        dependency_report["gate"]["status"] = "passed"
    missing_dir = workspace / "missing-deps"
    missing_dir.mkdir(exist_ok=True)
    (missing_dir / "dependency-report.json").write_text(
        json.dumps(dependency_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    transformations = workspace / "transformations.json"
    if not transformations.exists():
        checks["audit_complete"] = "failed"
    else:
        audit = json.loads(transformations.read_text(encoding="utf-8"))
        approved = set(audit.get("approved_reviewed_rule_ids", []))
        for item in audit.get("transformations", []):
            if item.get("safety") == "unsupported":
                checks["audit_complete"] = "failed"
            if item.get("safety") == "review_required" and item.get("rule_id") not in approved:
                checks["audit_complete"] = "failed"
    passed = all(value == "passed" for value in checks.values()) and dependency_report["gate"]["status"] != "blocked"
    verification = {
        "schema_version": "semantist.intake-verification/1.0.0",
        "status": "passed" if passed else "failed",
        "compatibility_manifest": str(compatibility_manifest) if compatibility_manifest else None,
        "library_sources": [str(path) for path in library_sources],
        "compatibility_sources": [str(path) for path in compatibility_sources],
        "stdlib": str(args.stdlib.resolve()),
        "runtime_archive": str(runtime),
        "native_objects": [str(path) for path in native_objects],
        "checks": checks,
        "targets": targets,
        "ir_external_declarations": sorted(declared_symbols),
        "unexplained_external_declarations": unexplained,
    }
    (workspace / "verification.json").write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"intake verification: {verification['status']} ({workspace / 'verification.json'})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
