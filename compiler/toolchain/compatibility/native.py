"""Compile native compatibility sources and return final linker inputs."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

from .project import CompatibilityProject, load_project


def _object_name(source: Path) -> str:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
    return f"{source.stem}-{digest}.o"


def build_native_inputs(
    project: CompatibilityProject,
    build_dir: Path,
    clang: Path,
    extra_compile_args: list[str] | None = None,
) -> list[str]:
    build_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    uses_cxx = False
    extra_compile_args = extra_compile_args or []
    for source in project.native_sources:
        output = build_dir / _object_name(source)
        command = [str(clang), "-g", "-O2", "-fno-omit-frame-pointer"]
        if source.suffix.lower() in {".cc", ".cpp", ".cxx"}:
            uses_cxx = True
            command.extend(("-x", "c++"))
        command.extend((
            *project.native_compile_args,
            *extra_compile_args,
            "-c",
            str(source),
            "-o",
            str(output),
        ))
        subprocess.run(command, check=True)
        inputs.append(str(output))
    inputs.extend(str(path) for path in project.native_objects)
    inputs.extend(str(path) for path in project.native_libraries)
    if uses_cxx and "-lstdc++" not in project.link_args:
        inputs.append("-lstdc++")
    inputs.extend(project.link_args)
    return inputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument(
        "--compile-arg",
        action="append",
        default=[],
        help="extra compiler argument for native compatibility objects",
    )
    args = parser.parse_args()

    project = load_project(args.manifest)
    for value in build_native_inputs(
        project,
        args.build_dir.resolve(),
        args.clang.resolve(),
        args.compile_arg,
    ):
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
