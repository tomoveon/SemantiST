#!/usr/bin/env python3
"""Create the intake workspace of one ST library (deterministic, no LLM).

The library is normalized by the diagnostic-driven rule engine
(``compiler/intake/normalize``) against the pinned RuSTy compiler: rules apply
only where the compiler reports an error, every edit is recorded with rule id
and version in ``transformations.json`` and a SourceMap per file, and errors
no rule handles stay in the output as unsupported constructs.

Then run ``validate_library.py`` for the compile/IR/native-link gate of
selected POUs, or ``python3 -m compiler.intake library`` for the per-POU
evidence of the whole library.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compiler.intake import rusty  # noqa: E402
from compiler.intake.normalize.engine import normalize, write_outputs  # noqa: E402
from compiler.intake.normalize.profiles import dialect_identity, load_dialect  # noqa: E402
from compiler.intake.normalize.rules import rules_for  # noqa: E402

PROFILES = ROOT / "compiler/toolchain/compatibility/profiles"
COMPATIBILITY_SCHEMA = "semantist.compatibility/1.1.0"
DEFAULT_STDLIB_GLOB = "artifacts/rusty-semantic/libs/stdlib/iec61131-st/*.st"
DIALECT_ALIASES = {"twincat": "codesys", "generic": "iec"}
STUBS_TEMPLATE = (
    "(* Library-specific declarations, reviewed semantic adapters, and explicit\n"
    "   deterministic environment models belong here. Never add default-return\n"
    "   executable functions merely to make compilation pass. *)\n"
)


def normalization_status(result) -> tuple[str, int]:
    """Classify normalization without hiding compiler/process failures."""
    fatal_codes = {"COMPILER_EXIT", "CODEGEN_MISSING_IR", "PANIC", "ROUNDS"}
    if result.panic or any(item.get("code") in fatal_codes for item in result.remaining):
        return "FAILED", 1
    if result.remaining:
        return "UNSUPPORTED", 2
    return "READY", 0


def main() -> int:
    dialects = sorted(p.stem for p in (ROOT / "compiler/intake/normalize/dialects").glob("*.json"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="original .st file or directory")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dialect", choices=[*dialects, *DIALECT_ALIASES], default="iec")
    parser.add_argument("--already-compatible", action="store_true",
                        help="copy without normalization (sources already accepted by the pinned RuSTy)")
    parser.add_argument("--reviewed-stubs", type=Path,
                        help="existing reviewed library-specific stubs.st to use as compatibility source")
    parser.add_argument("--profile", action="append", default=[],
                        help="additional compatibility profile (the dialect's profiles are always included)")
    parser.add_argument("--native-source", type=Path, action="append", default=[])
    parser.add_argument("--native-object", type=Path, action="append", default=[])
    parser.add_argument("--native-library", type=Path, action="append", default=[])
    parser.add_argument("--native-compile-arg", action="append", default=[])
    parser.add_argument("--link-arg", action="append", default=[])
    parser.add_argument("--stdlib-glob", default=DEFAULT_STDLIB_GLOB)
    parser.add_argument("--runtime-archive", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output_dir.resolve()
    if not source.exists():
        raise SystemExit(f"source does not exist: {source}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    dialect = load_dialect(DIALECT_ALIASES.get(args.dialect, args.dialect))

    profile_names = list(dict.fromkeys([*dialect.get("compatibility_profiles", []), *args.profile]))
    profile_st, native_sources, rebind = [], [], {}
    native_compile_args, link_args = list(args.native_compile_arg), list(args.link_arg)
    for name in profile_names:
        root = PROFILES / name
        manifest = root / "profile.json"
        if not manifest.is_file():
            raise SystemExit(f"unknown compatibility profile: {name}")
        profile = json.loads(manifest.read_text(encoding="utf-8"))
        if dialect["family"] not in profile.get("dialects", []):
            raise SystemExit(f"compatibility profile {name} does not support dialect {dialect['id']}")
        shutil.copytree(root, output / "compatibility" / name)
        profile_st += [f"compatibility/{name}/{item}" for item in profile.get("st_sources", [])]
        native_sources += [f"compatibility/{name}/{item}" for item in profile.get("native_sources", [])]
        native_compile_args += [str(item) for item in profile.get("native_compile_args", [])]
        link_args += [str(item) for item in profile.get("link_args", [])]
        rebind.update(profile.get("rebind", {}))

    stubs = output / "stubs.st"
    if args.reviewed_stubs:
        if not args.reviewed_stubs.is_file():
            raise SystemExit(f"reviewed stubs do not exist: {args.reviewed_stubs}")
        stubs.write_bytes(args.reviewed_stubs.read_bytes())
    else:
        stubs.write_text(STUBS_TEMPLATE, encoding="utf-8")

    files_on_disk = [source] if source.is_file() else sorted(source.rglob("*.st"))
    names = {path: (path.name if source.is_file() else path.relative_to(source).as_posix()) for path in files_on_disk}
    files = {names[path]: path.read_text(encoding="utf-8") for path in files_on_disk}
    compatibility_sources = [stubs, *(output / p for p in profile_st)]
    compiler_args = list(dialect.get("compiler_args", []))
    metadata = {"target_identity": {
        "source": {name: {"bytes": len(text)} for name, text in files.items()},
        "dialect": dialect_identity(dialect),
        "compatibility_profiles": profile_names,
        "toolchain": rusty.toolchain_identity(),
    }}
    normalization_state, normalization_exit = "READY", 0
    if args.already_compatible:
        for name, text in files.items():
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (output / "transformations.json").write_text(json.dumps({
            "schema_version": "semantist.transformations/2.0.0", **metadata, "transformations": [],
            "rule_summary": {}, "intake_note": "already-compatible library; copied without rewrites",
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        if not rusty.plc_path().exists():
            raise SystemExit(f"the pinned RuSTy compiler is required for normalization: {rusty.plc_path()}")
        result = normalize(files, dialect, {}, rules_for(dialect), output / ".normalize", compatibility_sources,
                           compiler_args, rebind)
        write_outputs(result, output, metadata)
        normalization_state, normalization_exit = normalization_status(result)
        shutil.rmtree(output / ".normalize", ignore_errors=True)

    normalized = sorted(names.values())
    manifest = {
        "schema_version": COMPATIBILITY_SCHEMA,
        "name": source.stem if source.is_file() else source.name,
        "dialect": dialect["id"],
        "profiles": profile_names,
        "library": {
            "sources": [*normalized, "stubs.st", *profile_st],
            "normalized_sources": normalized,
            "compatibility_sources": ["stubs.st", *profile_st],
        },
        "native": {"sources": native_sources, "objects": [str(p.resolve()) for p in args.native_object],
                   "libraries": [str(p.resolve()) for p in args.native_library],
                   "compile_args": native_compile_args, "link_args": link_args},
        "compiler": {"args": compiler_args},
        "execution": {"string_bytes": "ascii" if dialect.get("string", {}).get("encoding") == "single-byte"
                      else "any"},
        "toolchain": {"stdlib_glob": args.stdlib_glob},
        "normalization_status": normalization_state,
    }
    for native in args.native_source:
        destination = output / "compatibility" / "library" / native.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(native, destination)
        manifest["native"]["sources"].append(destination.relative_to(output).as_posix())
    if args.runtime_archive:
        manifest["toolchain"]["runtime_archive"] = str(args.runtime_archive.resolve())
    (output / "compatibility.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
    missing = output / "missing-deps"
    missing.mkdir(exist_ok=True)
    (missing / "dependency-report.json").write_text(json.dumps({
        "schema_version": "semantist.dependency-report/1.0.0", "dependencies": [],
        "classifications": {"declaration": [], "semantic_adapter": [], "environment_model": [], "unresolved": []},
        "gate": {"status": "pending_compile_and_link_diagnostics", "unresolved_count": 0},
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "verification.json").write_text(json.dumps({
        "schema_version": "semantist.intake-verification/1.0.0", "status": "pending",
        "checks": {k: "pending" for k in ("parse_and_typecheck", "llvm_ir", "native_link", "reachable_externals",
                                          "audit_complete")},
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"prepared library intake workspace: {output}")
    if normalization_exit:
        print(f"normalization status: {normalization_state}; see transformations.json", file=sys.stderr)
    return normalization_exit


if __name__ == "__main__":
    raise SystemExit(main())
