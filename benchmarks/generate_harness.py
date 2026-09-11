#!/usr/bin/env python3
"""Compile target.st + program.st with RuSTy and generate the precise harness.c.

The harness layout is derived from the RuSTy LLVM IR so that the C struct ABI
(size, input field offsets) matches the compiled PLC_PRG exactly.

Usage:
    python3 benchmarks/generate_harness.py [--suite SUITE] [--id ID] [--runtime podman]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.lib.generator import (
    ROOT,
    POINTER_ELEMENT_CAP,
    build_harness_c,
    load_targets,
    plc_prg_layout,
    pointer_element_size,
)
from benchmarks.lib.st_extract import extract_pou_text, extract_signature, strip_pou

STDLIB_GLOB = "/opt/rusty-semantic/libs/stdlib/iec61131-st/*.st"
OSCAT_SRC = "/work/SemantiST/benchmarks/oscat_basic/source/oscat.st"
STUBS_SRC = "/work/SemantiST/benchmarks/oscat_basic/source/stubs.st"
CODESYS_IFACE = "/work/SemantiST/benchmarks/external/compatibility/codesys-memory/interfaces.st"
PLC = "/opt/rusty-semantic/target/release/plc"

OSCAT_SRC_HOST = ROOT / "benchmarks" / "oscat_basic" / "source" / "oscat.st"


def build_oscat_deps(t, sub: Path) -> tuple[str, bool]:
    """Generate oscat.st minus the target POU so target.st is compiled directly.

    Returns (container path to deps.st, equivalent) where ``equivalent`` reports
    whether the target.st POU text matches oscat.st's POU text byte-for-byte.
    """
    target_pou = extract_pou_text(t.st_file, t.function)
    oscat_source = OSCAT_SRC_HOST.read_text(encoding="utf-8")
    oscat_pou = extract_pou_text(str(OSCAT_SRC_HOST), t.function)
    equivalent = target_pou.strip() == oscat_pou.strip()
    deps = strip_pou(oscat_source, t.function)
    (sub / "deps.st").write_text(deps, encoding="utf-8")
    return f"/work/SemantiST/artifacts/benchmarks/harness-work/{t.id}/deps.st", equivalent


def plc_files_for(t, sub: Path) -> tuple[list[str], dict]:
    program_rel = t.rel_st_file.replace("target.st", "program.st")
    meta: dict = {"target_st_compiled": True}
    if t.suite == "oscat_basic":
        deps_path, equivalent = build_oscat_deps(t, sub)
        meta["oscat_pou_equivalent"] = equivalent
        return [f"/work/SemantiST/{t.rel_st_file}", deps_path, STUBS_SRC, f"/work/SemantiST/{program_rel}"], meta
    return [CODESYS_IFACE, f"/work/SemantiST/{t.rel_st_file}", f"/work/SemantiST/{program_rel}"], meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", action="append", default=[])
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--runtime", default="podman")
    parser.add_argument("--skip-compile", action="store_true", help="reuse existing main.ll files")
    args = parser.parse_args()

    targets = load_targets()
    if args.suite:
        targets = [t for t in targets if t.suite in args.suite]
    if args.id:
        targets = [t for t in targets if t.id in args.id or t.function in args.id]

    work_root = ROOT / "artifacts" / "benchmarks" / "harness-work"
    if not args.skip_compile:
        if work_root.exists():
            shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    sigs = {}
    meta_by_id: dict[str, dict] = {}
    for t in targets:
        sig = extract_signature(t.st_file, t.function)
        sigs[t.id] = sig
        sub = work_root / t.id
        sub.mkdir(parents=True, exist_ok=True)
        files, meta = plc_files_for(t, sub)
        meta_by_id[t.id] = meta
        cfg = {
            "name": f"semantist_{t.id}",
            "files": [STDLIB_GLOB, *files],
            "compile_type": "IR",
            "output": "main.ll",
            "libraries": [],
        }
        (sub / "plc.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    if not args.skip_compile:
        script_lines = ["set -e"]
        for t in targets:
            script_lines.append(
                f"{PLC} build /work/SemantiST/artifacts/benchmarks/harness-work/{t.id}/plc.json "
                f"--ir --single-module --error-format none "
                f"--build-location /work/SemantiST/artifacts/benchmarks/harness-work/{t.id}/build "
                f"-o /work/SemantiST/artifacts/benchmarks/harness-work/{t.id}/main.ll"
            )
        script = "\n".join(script_lines) + "\n"
        (work_root / "compile_all.sh").write_text(script, encoding="utf-8")

        runtime = args.runtime
        cmd = [
            runtime, "run", "--rm",
            "-v", f"{ROOT}:/work/SemantiST",
            "-w", "/work/SemantiST",
            "localhost/semantist:0.1.0",
            "bash", "/work/SemantiST/artifacts/benchmarks/harness-work/compile_all.sh",
        ]
        print("[harness] compiling all targets with RuSTy ...", flush=True)
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout[-4000:] if result.stdout else "")
        if result.returncode != 0:
            print(f"[harness] RuSTy compile batch failed with {result.returncode}", flush=True)

    ok = 0
    failed: list[str] = []
    for t in targets:
        ll_path = work_root / t.id / "main.ll"
        if not ll_path.exists():
            failed.append(t.id)
            print(f"[harness] {t.id}: no main.ll", flush=True)
            continue
        try:
            ll_text = ll_path.read_text(encoding="utf-8", errors="replace")
            sig = sigs[t.id]
            num_inputs = len(sig.all_inputs)
            instance_size, input_size, fields = plc_prg_layout(ll_text, num_inputs)
            pointer_sizes = [
                pointer_element_size(sig.all_inputs[i].type_raw) * POINTER_ELEMENT_CAP
                for i, (_off, _size, is_ptr) in enumerate(fields)
                if is_ptr
            ]
            harness = build_harness_c(
                instance_size,
                input_size,
                fields,
                pointer_sizes=pointer_sizes,
                scan_cycle=(t.suite == "icsquartz_scan_cycle"),
            )
            (Path(t.st_file).parent / "harness.c").write_text(harness, encoding="utf-8")
            ok += 1
            print(f"[harness] {t.id}: size={instance_size} input={input_size} fields={len(fields)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed.append(t.id)
            print(f"[harness] {t.id}: ERROR {exc}", flush=True)

    print(f"[harness] ok={ok} failed={len(failed)}")
    if failed:
        print(f"[harness] failed targets: {failed}")
    meta_path = work_root / "generation-meta.json"
    meta_path.write_text(json.dumps(meta_by_id, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
