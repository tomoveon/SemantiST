#!/usr/bin/env python3
"""Create the non-destructive, auditable workspace for one ST library intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PROFILES = ROOT / "compiler/toolchain/compatibility/profiles"
COMPATIBILITY_SCHEMA = "semantist.compatibility/1.0.0"
DEFAULT_STDLIB_GLOB = "artifacts/rusty-semantic/libs/stdlib/iec61131-st/*.st"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="original .st file or directory")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dialect", choices=("codesys", "twincat", "generic", "all"), default="generic")
    parser.add_argument(
        "--already-compatible",
        action="store_true",
        help="copy without rewrites after an existing compatible library has been audited",
    )
    parser.add_argument(
        "--reviewed-stubs",
        type=Path,
        help="copy an existing reviewed library-specific stubs.st into the workspace",
    )
    parser.add_argument(
        "--allow-reviewed-rule",
        action="append",
        type=int,
        default=[],
        help="review-required transformation rule with recorded equivalence evidence",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="reusable compiler/toolchain/compatibility profile to include",
    )
    parser.add_argument("--native-source", type=Path, action="append", default=[])
    parser.add_argument("--native-object", type=Path, action="append", default=[])
    parser.add_argument("--native-library", type=Path, action="append", default=[])
    parser.add_argument("--native-compile-arg", action="append", default=[])
    parser.add_argument("--link-arg", action="append", default=[])
    parser.add_argument(
        "--stdlib-glob",
        default=DEFAULT_STDLIB_GLOB,
    )
    parser.add_argument("--runtime-archive", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output_dir.resolve()
    if not source.exists():
        raise SystemExit(f"source does not exist: {source}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.transformations.tmp.json"
    if temporary.exists():
        temporary.unlink()

    if args.already_compatible:
        temporary.write_text(
            json.dumps(
                {
                    "source_directory": str(source),
                    "dialect": args.dialect,
                    "total_files": 1 if source.is_file() else len(list(source.rglob("*.st"))),
                    "files_with_issues": 0,
                    "transformations": [],
                    "rule_summary": {},
                    "intake_note": "already-compatible library; copied without source rewrites",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        subprocess.run(
            [
                sys.executable,
                str(HERE / "st_analyzer.py"),
                str(source),
                "--dialect",
                args.dialect,
                "--output",
                str(temporary),
            ],
            check=True,
        )
    converter_command = [
            sys.executable,
            str(HERE / "st_converter.py"),
            str(temporary),
            "--output-dir",
            str(output),
        ]
    for rule_id in args.allow_reviewed_rule:
        converter_command.extend(("--allow-reviewed-rule", str(rule_id)))
    subprocess.run(converter_command, check=True)

    transformations = json.loads(temporary.read_text(encoding="utf-8"))
    transformations["schema_version"] = "semantist.transformations/1.0.0"
    transformations["original_preserved"] = True
    transformations["approved_reviewed_rule_ids"] = sorted(set(args.allow_reviewed_rule))
    if source.is_file():
        compatible = output / source.name
        transformations["source_sha256"] = sha256(source)
        transformations["compatible_library"] = str(compatible)
        transformations["compatible_sha256"] = sha256(compatible)
    (output / "transformations.json").write_text(
        json.dumps(transformations, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.unlink()

    if args.reviewed_stubs:
        reviewed_stubs = args.reviewed_stubs.resolve()
        if not reviewed_stubs.is_file():
            raise SystemExit(f"reviewed stubs do not exist: {reviewed_stubs}")
        (output / "stubs.st").write_bytes(reviewed_stubs.read_bytes())
    else:
        (output / "stubs.st").write_text(
            "(* Library-specific declarations, reviewed semantic adapters, and explicit\n"
            "   deterministic environment models belong here. Never add default-return\n"
            "   executable functions merely to make compilation pass. *)\n",
            encoding="utf-8",
        )

    profile_st_sources: list[str] = []
    native_sources: list[str] = []
    native_compile_args = list(args.native_compile_arg)
    link_args = list(args.link_arg)
    selected_profiles = list(dict.fromkeys(args.profile))
    for profile_name in selected_profiles:
        profile_root = PROFILES / profile_name
        profile_manifest = profile_root / "profile.json"
        if not profile_manifest.is_file():
            raise SystemExit(f"unknown compatibility profile: {profile_name}")
        profile = json.loads(profile_manifest.read_text(encoding="utf-8"))
        if args.dialect not in profile.get("dialects", []) and args.dialect != "all":
            raise SystemExit(
                f"compatibility profile {profile_name} does not support dialect {args.dialect}"
            )
        destination = output / "compatibility" / profile_name
        shutil.copytree(profile_root, destination)
        profile_st_sources.extend(
            str((Path("compatibility") / profile_name / item).as_posix())
            for item in profile.get("st_sources", [])
        )
        native_sources.extend(
            str((Path("compatibility") / profile_name / item).as_posix())
            for item in profile.get("native_sources", [])
        )
        native_compile_args.extend(str(item) for item in profile.get("native_compile_args", []))
        link_args.extend(str(item) for item in profile.get("link_args", []))

    custom_native_root = output / "compatibility" / "library"
    for source_path in args.native_source:
        source_path = source_path.resolve()
        if not source_path.is_file():
            raise SystemExit(f"native compatibility source does not exist: {source_path}")
        custom_native_root.mkdir(parents=True, exist_ok=True)
        destination = custom_native_root / source_path.name
        if destination.exists():
            raise SystemExit(f"duplicate native compatibility source name: {source_path.name}")
        shutil.copy2(source_path, destination)
        native_sources.append(str(destination.relative_to(output).as_posix()))

    copied_native_objects: list[str] = []
    copied_native_libraries: list[str] = []
    for paths, directory_name, destination_list in (
        (args.native_object, "objects", copied_native_objects),
        (args.native_library, "libraries", copied_native_libraries),
    ):
        destination_root = custom_native_root / directory_name
        for input_path in paths:
            input_path = input_path.resolve()
            if not input_path.is_file():
                raise SystemExit(f"native compatibility input does not exist: {input_path}")
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / input_path.name
            if destination.exists():
                raise SystemExit(f"duplicate native compatibility input name: {input_path.name}")
            shutil.copy2(input_path, destination)
            destination_list.append(str(destination.relative_to(output).as_posix()))

    converted_sources = [
        str(path.relative_to(output).as_posix())
        for path in sorted(output.rglob("*.st"))
        if path.name != "stubs.st" and "compatibility" not in path.relative_to(output).parts
    ]
    library_sources = converted_sources + ["stubs.st", *profile_st_sources]
    compatibility_manifest = {
        "schema_version": COMPATIBILITY_SCHEMA,
        "name": source.stem if source.is_file() else source.name,
        "dialect": args.dialect,
        "profiles": selected_profiles,
        "library": {
            "sources": library_sources,
            "normalized_sources": converted_sources,
            "compatibility_sources": ["stubs.st", *profile_st_sources],
        },
        "native": {
            "sources": native_sources,
            "objects": copied_native_objects,
            "libraries": copied_native_libraries,
            "compile_args": native_compile_args,
            "link_args": link_args,
        },
        "toolchain": {"stdlib_glob": args.stdlib_glob},
    }
    if args.runtime_archive:
        compatibility_manifest["toolchain"]["runtime_archive"] = str(args.runtime_archive.resolve())
    (output / "compatibility.json").write_text(
        json.dumps(compatibility_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    missing = output / "missing-deps"
    missing.mkdir()
    dependency_report = {
        "schema_version": "semantist.dependency-report/1.0.0",
        "resolution_order": [
            "compiler_intrinsic",
            "rusty_iec_st_library",
            "libiec61131std.a",
            "input_library",
            "stubs.st",
        ],
        "dependencies": [],
        "classifications": {
            "declaration": [],
            "semantic_adapter": [],
            "environment_model": [],
            "unresolved": [],
        },
        "gate": {"status": "pending_compile_and_link_diagnostics", "unresolved_count": 0},
    }
    (missing / "dependency-report.json").write_text(
        json.dumps(dependency_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verification = {
        "schema_version": "semantist.intake-verification/1.0.0",
        "status": "pending",
        "checks": {
            "parse_and_typecheck": "pending",
            "llvm_ir": "pending",
            "native_link": "pending",
            "reachable_externals": "pending",
            "audit_complete": "pending",
        },
    }
    (output / "verification.json").write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"prepared one-time library intake workspace: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
