"""Build the KLEE bitcode for one target (spec section 6.2)."""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Models are package data. Resolve them from this module so an installed wheel
# does not accidentally depend on the source checkout's repository root.
MODELS = Path(__file__).resolve().parent / "models"


def _tool(env: str, default: str) -> str:
    return os.environ.get(env, default)


def build(
    *,
    build_dir: Path,
    harness: Path,
    state_ir: Path,
    function: str,
    out_dir: Path,
    abi: dict,
    time_budget_s: float = 120.0,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    klee_dir = out_dir / "klee"
    klee_dir.mkdir(parents=True, exist_ok=True)
    unsupported_path = klee_dir / "UNSUPPORTED.json"
    manifest_path = klee_dir / "manifest.json"
    deadline = time.monotonic() + max(0.0, time_budget_s)
    for stale in (*klee_dir.glob("*.bc"), *klee_dir.glob("*.bc.tmp"), manifest_path, unsupported_path):
        stale.unlink(missing_ok=True)

    class PhaseError(Exception):
        def __init__(self, status: str, reason: str, detail: str = "") -> None:
            super().__init__(reason)
            self.status, self.reason, self.detail = status, reason, detail

    def run(command: list[str], phase: str, output: Path | None = None) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhaseError("timeout", f"deadline expired before {phase}")
        temporary = output.with_suffix(output.suffix + ".tmp") if output else None
        if temporary is not None:
            command = [str(temporary) if arg == str(output) else arg for arg in command]
            temporary.unlink(missing_ok=True)
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
        except FileNotFoundError as error:
            raise PhaseError("unsupported", f"{phase} tool missing", str(error)) from error
        except OSError as error:
            raise PhaseError("failed", f"{phase} tool could not start", str(error)) from error
        try:
            stdout, stderr = process.communicate(timeout=remaining)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            raise PhaseError("timeout", f"{phase} timed out", str(error)) from error
        finally:
            if temporary is not None and not temporary.exists():
                output.unlink(missing_ok=True)
        if result.returncode != 0:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise PhaseError("failed", f"{phase} failed (exit {result.returncode})", result.stderr[:4000])
        if temporary is not None:
            if not temporary.exists():
                raise PhaseError("failed", f"{phase} produced no output")
            os.replace(temporary, output)
        return result

    def failure(error: PhaseError) -> dict:
        for stale in (*klee_dir.glob("*.bc"), *klee_dir.glob("*.bc.tmp"), manifest_path):
            stale.unlink(missing_ok=True)
        document = {"status": error.status, "reason": error.reason,
                    "detail": error.detail, "function": function}
        unsupported_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return document
    clang = _tool("SEMANTIST_CLANG18", "/usr/bin/clang-18")
    llvm_as = _tool("SEMANTIST_LLVM_AS16", "/usr/lib/llvm-18/bin/llvm-as")
    llvm_link = _tool("SEMANTIST_LLVM_LINK16", "/usr/lib/llvm-18/bin/llvm-link")
    llvm_nm = _tool("SEMANTIST_LLVM_NM16", "/usr/lib/llvm-18/bin/llvm-nm")
    llvm_dis = _tool("SEMANTIST_LLVM_DIS16", "/usr/lib/llvm-18/bin/llvm-dis")
    klee_include = _tool("SEMANTIST_KLEE_INCLUDE", "/opt/klee/include")

    def unsupported(reason: str, detail: str | None = None) -> dict:
        return failure(PhaseError("unsupported", reason, detail or ""))

    required_tools = {
        "clang": clang,
        "llvm-as": llvm_as,
        "llvm-link": llvm_link,
        "llvm-nm": llvm_nm,
        "llvm-dis": llvm_dis,
    }
    missing_tools = [name for name, path in required_tools.items() if not Path(path).exists()]
    if missing_tools:
        return unsupported("llvm-18 toolchain missing", ", ".join(missing_tools))
    if not Path(state_ir).exists():
        return unsupported("prepared state IR missing")

    try:
        target_bc = klee_dir / "target.bc"
        run([llvm_as, str(state_ir), "-o", str(target_bc)], "llvm-as", target_bc)

        harness_bc = klee_dir / "harness.bc"
        run(
            [clang, "-DSEMANTIST_KLEE", "-DSEMANTIST_IN_PROCESS", "-I", klee_include,
             "-O1", "-emit-llvm", "-c", str(harness), "-o", str(harness_bc)],
            "harness clang", harness_bc,
        )

        model_objects: list[Path] = []
        for model in ("semantic_klee.c", "libc_stubs.c", "stdlib_stubs.c", "memory_stubs.c"):
            output = klee_dir / (Path(model).stem + ".bc")
            run([clang, "-DSEMANTIST_KLEE", "-I", klee_include, "-O1", "-emit-llvm", "-c",
                 str(MODELS / model), "-o", str(output)], f"model {model} clang", output)
            model_objects.append(output)

        bundle_bc = klee_dir / "bundle.bc"
        run([llvm_link, str(harness_bc), str(target_bc), *[str(item) for item in model_objects],
             "-o", str(bundle_bc)], "llvm-link", bundle_bc)
        undefined = run([llvm_nm, "--undefined-only", str(bundle_bc)], "llvm-nm")
    except PhaseError as error:
        return failure(error)
    missing = []
    for line in undefined.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        symbol = parts[-1]
        if symbol.startswith(("llvm.", "klee_", "__klee_", "__semantist_")):
            continue
        if symbol in {"main", "memcpy", "memset", "memmove", "malloc", "free", "realloc",
                      "stderr", "stdout"}:
            continue
        if symbol.startswith("__ubsan_handle_"):
            continue
        missing.append(symbol)
    if missing:
        return unsupported("undefined external symbols", ", ".join(sorted(set(missing))[:40]))

    header = build_dir / "semantist_frame.h"
    if header.exists():
        shutil.copy2(header, klee_dir / "semantist_frame.h")
    symbols = set()
    try:
        dis = run([llvm_dis, str(target_bc), "-o", "-"], "llvm-dis")
        bodies = {
            match.group(1): match.group(2)
            for match in re.finditer(r"define[^\n]*@([A-Za-z0-9_$]+)\([^\n]*\n(.*?)\n\}", dis.stdout, re.S)
        }
        symbols = set()
        pending = [name for name in bodies if function in name]
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            for callee in re.findall(r"call [^@\n]*@([A-Za-z_][A-Za-z0-9_]*)", bodies[name]):
                symbols.add(callee)
                if callee in bodies and callee not in visited:
                    pending.append(callee)
    except PhaseError as error:
        return failure(error)
    faithful = {"TON", "TOF", "TP", "R_TRIG", "F_TRIG", "TIME",
                "DWORD_TO_REAL", "REAL_TO_DWORD", "ROL__DWORD", "ROL__BYTE",
                "ROR__BYTE", "MAX__TIME", "LEN__STRING",
                "CONCAT__STRING", "INSERT_EXT__STRING", "LINT_TO_STRING_EXT"}
    float_fields = [entry for section in ("frame_fields", "shared_fields") for entry in abi.get(section, [])
                    if entry.get("spec", {}).get("name") in ("REAL", "LREAL")]
    concretizing = {"strtod"} if float_fields else set()
    havoc = {"ACOS__REAL", "ASIN__REAL", "ATAN__REAL", "COS__REAL", "SIN__REAL",
             "SIN__LREAL", "TAN__REAL", "LN__REAL", "SQRT__REAL"}
    approximating = {"EXP__REAL", "ROUND__REAL", "STRING_TO_DINT"}
    imprecise = sorted((symbols & (havoc | approximating)) | concretizing)
    manifest = {
        "status": "supported",
        "function": function,
        "klee_version": "3.3",
        "llvm": "18",
        "target_bc": str(target_bc),
        "harness_bc": str(harness_bc),
        "bundle_bc": str(bundle_bc),
        "abi_frame_size": (abi.get("frame") or {}).get("size"),
        "model_classes": {
            "faithful": sorted(symbols & faithful),
            "concretizing": sorted(concretizing),
            "havoc": sorted(symbols & havoc),
            "approximating": sorted(symbols & approximating),
        },
        "imprecise": imprecise,
    }
    manifest_temp = manifest_path.with_suffix(".json.tmp")
    manifest_temp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(manifest_temp, manifest_path)
    if unsupported_path.exists():
        unsupported_path.unlink()
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--state-ir", required=True, type=Path)
    parser.add_argument("--function", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--abi", required=True, type=Path)
    parser.add_argument("--time-budget-s", type=float, default=120.0)
    args = parser.parse_args(argv)
    abi = json.loads(args.abi.read_text(encoding="utf-8"))
    manifest = build(
        build_dir=args.build_dir,
        harness=args.harness,
        state_ir=args.state_ir,
        function=args.function,
        out_dir=args.out,
        abi=abi,
        time_budget_s=args.time_budget_s,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    # Missing capability is an explicit, non-fatal UNSUPPORTED result. A
    # tool failure or deadline expiry must remain a process failure so callers
    # cannot continue as if the symbolic build completed.
    return 0 if manifest.get("status") in {"supported", "unsupported"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
