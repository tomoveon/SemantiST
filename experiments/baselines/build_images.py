#!/usr/bin/env python3
"""Build and smoke-test the pinned baseline environment images."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOCK_FILE = ROOT / "baselines.lock.json"
TOOL_ORDER = ("aflplusplus", "icsquartz", "icsfuzz", "structuredfuzzer")
SMOKE_COMMANDS = {
    "aflplusplus": (
        "command -v afl-fuzz && command -v afl-clang-fast && "
        "command -v afl-showmap && afl-clang-fast --version >/dev/null"
    ),
    "icsquartz": (
        "test -x /opt/icsquartz/target/release/libafl_cc && "
        "test -x /opt/icsquartz/target/release/libafl_cxx && "
        "test -r /opt/icsquartz/target/release/liblibfuzzer_icsquartz.a && "
        "test \"$(llvm-config-18 --version | cut -d. -f1)\" = 18"
    ),
    "icsfuzz": (
        "test -x /opt/icsfuzz/fuzzer && "
        "test -x /opt/codesys/bin/codesyscontrol.bin && "
        "test -x /usr/sbin/CodeMeterLin && "
        "test -r /etc/CODESYSControl.cfg"
    ),
    "structuredfuzzer": (
        "command -v stfuzz && command -v stcode && "
        "command -v afl-fuzz && command -v afl-clang-fast && "
        "command -v iec2c && "
        "test -r /opt/structuredfuzzer/README.md && "
        "test -r /usr/local/bin/stfuzz/matiec/COPYING && "
        "python3 -c 'import analyzer, tree_sitter, z3' && "
        "stfuzz --help >/dev/null && stcode --help >/dev/null"
    ),
}


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def load_lock() -> dict[str, object]:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def selected_tools(requested: list[str]) -> list[str]:
    if not requested or "all" in requested:
        return list(TOOL_ORDER)
    return list(dict.fromkeys(requested))


def build(tool: str, config: dict[str, object], platform: str, pull: bool) -> None:
    dockerfile = ROOT / str(config["dockerfile"])
    command = ["docker", "build", "--platform", platform]
    if pull:
        command.append("--pull")
    command.extend(("--tag", str(config["tag"]), "--file", str(dockerfile)))
    for name, value in dict(config["build_args"]).items():
        command.extend(("--build-arg", f"{name}={value}"))
    command.append(str(dockerfile.parent))
    run(command)


def smoke(tool: str, config: dict[str, object], platform: str) -> None:
    run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            str(config["tag"]),
            "/bin/bash",
            "-lc",
            SMOKE_COMMANDS[tool],
        ]
    )
    run(
        [
            "docker",
            "image",
            "inspect",
            str(config["tag"]),
            "--format",
            "{{.Id}} {{.Os}}/{{.Architecture}}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tools",
        nargs="*",
        metavar="TOOL",
        help=(
            "images to process; default: all; choices: "
            + ", ".join(("all", *TOOL_ORDER))
        ),
    )
    parser.add_argument("--platform", help="override the locked Docker platform")
    parser.add_argument("--skip-build", action="store_true", help="only smoke-test existing images")
    parser.add_argument("--skip-smoke", action="store_true", help="build without running smoke checks")
    parser.add_argument("--no-pull", action="store_true", help="do not refresh pinned base images")
    args = parser.parse_args()

    valid_tools = ("all", *TOOL_ORDER)
    invalid_tools = [tool for tool in args.tools if tool not in valid_tools]
    if invalid_tools:
        parser.error(
            "argument tools: invalid choice: "
            + ", ".join(repr(tool) for tool in invalid_tools)
            + " (choose from "
            + ", ".join(repr(tool) for tool in valid_tools)
            + ")"
        )

    if args.skip_build and args.skip_smoke:
        parser.error("--skip-build and --skip-smoke cannot be used together")
    if shutil.which("docker") is None:
        raise SystemExit("docker CLI was not found; run this script on the Docker host")

    lock = load_lock()
    platform = args.platform or str(lock["platform"])
    images = dict(lock["images"])
    for tool in selected_tools(args.tools):
        config = dict(images[tool])
        if not args.skip_build:
            build(tool, config, platform, pull=not args.no_pull)
        if not args.skip_smoke:
            smoke(tool, config, platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
