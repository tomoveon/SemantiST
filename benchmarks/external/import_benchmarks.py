#!/usr/bin/env python3
"""Import third-party ST benchmark programs into SemantiST's runnable layout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "benchmarks" / "external"
OSCAT = ROOT / "benchmarks" / "oscat_basic"


ICSFUZZ_IDS = [
    "icsfuzz_bf_mcpy_1",
    "icsfuzz_bf_mcpy_12",
    "icsfuzz_bf_mcpy_6",
    "icsfuzz_bf_mcpy_8",
    "icsfuzz_bf_mmove_1",
    "icsfuzz_bf_mmove_12",
    "icsfuzz_bf_mmove_4",
    "icsfuzz_bf_mmove_7",
    "icsfuzz_bf_mset_1",
    "icsfuzz_bf_mset_3",
    "icsfuzz_bf_mset_5",
    "icsfuzz_oob_1_arr_1",
    "icsfuzz_oob_1_arr_13",
    "icsfuzz_oob_1_arr_6",
    "icsfuzz_oob_2_1",
    "icsfuzz_oob_2_13",
    "icsfuzz_oob_2_5",
]

SCAN_IDS = [
    "scan_cycle_aircraft_oobr",
    "scan_cycle_aircraft_oobw_4",
    "scan_cycle_aircraft_oobw_5",
    "scan_cycle_anaerobic_oobr_1",
    "scan_cycle_anaerobic_oobr_2",
    "scan_cycle_anaerobic_oobw_1",
    "scan_cycle_anaerobic_oobw_2",
    "scan_cycle_anaerobic_oobw_3",
    "scan_cycle_chemical_oobr_1",
    "scan_cycle_chemical_oobw_1",
    "scan_cycle_smart_oobr_1",
    "scan_cycle_smart_oobw_1",
]

OSCAT_TARGETS = [
    # FUNCTION targets
    "BIT_COUNT",
    "BUFFER_COMP",
    "_BUFFER_INSERT",
    "CHARNAME",
    "CLEAN",
    "DEL_CHARS",
    "DT_TO_STRF",
    "FIND_CHAR",
    "FIND_CTRL",
    "FINDB_NONUM",
    "FINDB_NUM",
    "FINDP",
    "FSTRING_TO_BYTE",
    "FSTRING_TO_DWORD",
    "IS_CC",
    "IS_NCC",
    "MIRROR",
    "MONTH_TO_STRING",
    "REAL_TO_STRF",
    "REPLACE_ALL",
    "REPLACE_CHARS",
    "TRIM",
    "TRIM1",
    "TRIME",
    "UPPERCASE",
    "WEEKDAY_TO_STRING",
    "INC2",
    "DAYS_IN_MONTH",
    "LIST_GET",
    "MULTI_IN",
    "_STRING_TO_BUFFER",
    # FUNCTION_BLOCK targets
    "ALARM_2",
    "BAR_GRAPH",
    "CALIBRATE",
    "CONTROL_SET1",
    "COUNT_DR",
    "DELAY",
    "DRIVER_1",
    "FIFO_16",
    "FILTER_I",
    "FILTER_MAV_DW",
    "FLOW_CONTROL",
    "FLOW_METER",
    "GEN_PULSE",
    "HYST_1",
    "INTEGRATE",
    "INTERLOCK",
    "LIST_NEXT",
    "MANUAL_1",
    "METER",
    "PARSET",
    "PWM_DC",
    "RMP_B",
    "SCHEDULER_2",
    "SIGNAL",
    "STACK_16",
    "STORE_8",
    "TOGGLE",
    "TREND",
    "CTRL_PI",
    "CTRL_PID",
]


@dataclass(frozen=True)
class ImportedTarget:
    suite: str
    id: str
    kind: str
    function: str
    st_file: Path
    compatibility_manifest: Path | None
    upstream_repo: str
    upstream_commit: str
    upstream_path: Path
    upstream_sha256: str
    lineage: str
    transformations: list[str]

    def to_manifest(self) -> dict[str, object]:
        value: dict[str, object] = {
            "suite": self.suite,
            "id": self.id,
            "kind": self.kind,
            "function": self.function,
            "st_file": rel(self.st_file),
            "upstream_repo": self.upstream_repo,
            "upstream_commit": self.upstream_commit,
            "upstream_path": str(self.upstream_path),
            "upstream_sha256": self.upstream_sha256,
            "lineage": self.lineage,
            "transformations": self.transformations,
        }
        if self.compatibility_manifest:
            value["compatibility_manifest"] = rel(self.compatibility_manifest)
        return value


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def load_extractor():
    path = ROOT / "compiler" / "scripts" / "genfunction.py"
    spec = importlib.util.spec_from_file_location("semantist_genfunction", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.extract_pou


def slug(value: str) -> str:
    return value.lower().lstrip("_")


def pou_name(prefix: str, bench_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", bench_id).upper()
    return f"STFZ_{prefix}_{name}"


def normalize_codesys_dialect(src: str) -> str:
    src = re.sub(r"\b(W?STRING)\((\d+)\)", r"\1[\2]", src)
    src = re.sub(r"\bADR\s*\(", "REF(", src, flags=re.IGNORECASE)
    return src


def strip_program(src: str) -> str:
    return re.sub(r"(?is)[ \t]*(?:/\*.*?\*/\s*)?PROGRAM\s+\w+\b.*?END_PROGRAM\s*", "", src, count=1).lstrip()


def first_pou(src: str, kind: str) -> str:
    match = re.search(rf"(?im)^[ \t]*{kind}[ \t]+([A-Za-z_][A-Za-z0-9_]*)\b", src)
    if not match:
        raise ValueError(f"no {kind} found")
    return match.group(1)


def rename_word(src: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, src)


def setup_compatibility() -> Path:
    compat = SUITE / "compatibility"
    memory = compat / "codesys-memory"
    memory.mkdir(parents=True, exist_ok=True)
    src_profile = ROOT / "compiler" / "toolchain" / "compatibility" / "profiles" / "codesys-memory"
    shutil.copyfile(src_profile / "runtime.c", memory / "runtime.c")
    runtime = (memory / "runtime.c").read_text(encoding="utf-8")
    runtime += """

#include <stdbool.h>

bool __SEMANTIST_BITCPY(
    uint8_t *dest,
    uint16_t dest_start_bit,
    const uint8_t *source,
    uint16_t source_start_bit,
    uint16_t bit_count) {
    if (!dest || !source) {
        return false;
    }
    while (bit_count > 0) {
        uint16_t src_byte = source_start_bit / 8;
        uint16_t src_bit = source_start_bit % 8;
        uint16_t dst_byte = dest_start_bit / 8;
        uint16_t dst_bit = dest_start_bit % 8;
        uint16_t bits = 8 - (src_bit > dst_bit ? src_bit : dst_bit);
        if (bits > bit_count) {
            bits = bit_count;
        }
        uint8_t src_mask = (uint8_t)(((1u << bits) - 1u) << src_bit);
        uint8_t dst_mask = (uint8_t)(((1u << bits) - 1u) << dst_bit);
        uint8_t value = (uint8_t)((source[src_byte] & src_mask) >> src_bit);
        dest[dst_byte] = (uint8_t)((dest[dst_byte] & ~dst_mask) | (value << dst_bit));
        dest_start_bit = (uint16_t)(dest_start_bit + bits);
        source_start_bit = (uint16_t)(source_start_bit + bits);
        bit_count = (uint16_t)(bit_count - bits);
    }
    return true;
}
"""
    write(memory / "runtime.c", runtime)
    interfaces = (src_profile / "interfaces.st").read_text(encoding="utf-8")
    interfaces += """

@EXTERNAL FUNCTION __SEMANTIST_BITCPY : BOOL
VAR_INPUT
    DEST : REF_TO BYTE;
    DEST_START_BIT : WORD;
    SOURCE : REF_TO BYTE;
    SOURCE_START_BIT : WORD;
    BIT_COUNT : WORD;
END_VAR
END_FUNCTION

FUNCTION MemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
MemSet := SysMemSet(pDest, udiValue, udiCount);
END_FUNCTION

FUNCTION MemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
MemCpy := SysMemCpy(pDest, pSrc, udiCount);
END_FUNCTION

FUNCTION MemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
MemMove := SysMemMove(pDest, pSrc, udiCount);
END_FUNCTION

FUNCTION __memcpy : REF_TO BYTE
VAR_INPUT
    dest : REF_TO BYTE;
    src : REF_TO BYTE;
    size : DINT;
END_VAR
__memcpy := __SEMANTIST_MEMCPY(dest, src, DINT_TO_UDINT(size));
END_FUNCTION

FUNCTION BitCpy : BOOL
VAR_INPUT
    pDest : REF_TO BYTE;
    wDstStartBit : WORD;
    pSource : REF_TO BYTE;
    wSrcStartBit : WORD;
    wSize : WORD;
END_VAR
BitCpy := __SEMANTIST_BITCPY(pDest, wDstStartBit, pSource, wSrcStartBit, wSize);
END_FUNCTION
"""
    write(memory / "interfaces.st", interfaces)
    memory_manifest = memory / "compatibility.json"
    write(
        memory_manifest,
        json.dumps(
            {
                "schema_version": "semantist.compatibility/1.0.0",
                "name": "external_codesys_memory",
                "dialect": "codesys",
                "profiles": ["codesys-memory", "codesys-memory-aliases"],
                "library": {
                    "sources": ["interfaces.st"],
                    "normalized_sources": ["interfaces.st"],
                    "compatibility_sources": [],
                },
                "native": {
                    "sources": ["runtime.c"],
                    "objects": [],
                    "libraries": [],
                    "compile_args": [],
                    "link_args": [],
                },
                "toolchain": {
                    "stdlib_glob": "artifacts/rusty-semantic/libs/stdlib/iec61131-st/*.st"
                },
            },
            indent=2,
        )
        + "\n",
    )
    return memory_manifest


def import_icsquartz(icsquartz: Path, memory_manifest: Path) -> list[ImportedTarget]:
    commit = git_commit(icsquartz)
    targets: list[ImportedTarget] = []
    license_file = icsquartz / "LICENSE"
    if license_file.exists():
        (SUITE / "icsquartz").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, SUITE / "icsquartz" / "LICENSE")

    for bench_id in ICSFUZZ_IDS:
        src_path = icsquartz / "benchmarks" / bench_id / "src" / "program.st"
        src = src_path.read_text(encoding="utf-8", errors="replace")
        body = normalize_codesys_dialect(strip_program(src))
        old = first_pou(body, "FUNCTION")
        new = pou_name("ICSF", bench_id)
        body = rename_word(body, old, new)
        out = SUITE / "icsquartz" / "icsfuzz" / bench_id / "target.st"
        write(out, body.strip() + "\n")
        targets.append(
            ImportedTarget(
                "icsquartz_icsfuzz",
                bench_id,
                "FUNCTION",
                new,
                out,
                memory_manifest,
                "https://github.com/momalab/ICSQuartz",
                commit,
                src_path.relative_to(icsquartz),
                sha256(src_path),
                "ICSQuartz extracted ST benchmark with ICSFuzz lineage",
                ["removed PROGRAM PLC_PRG harness wrapper", f"renamed target FUNCTION {old} to {new}", "normalized CODESYS string lengths and ADR()"],
            )
        )

    for bench_id in SCAN_IDS:
        src_path = icsquartz / "benchmarks" / bench_id / "src" / "program.st"
        src = src_path.read_text(encoding="utf-8", errors="replace")
        body = normalize_codesys_dialect(strip_program(src))
        old = first_pou(body, "FUNCTION_BLOCK")
        new = pou_name("SCAN", bench_id)
        body = rename_word(body, old, new)
        out = SUITE / "icsquartz" / "scan_cycle" / bench_id / "target.st"
        write(out, body.strip() + "\n")
        targets.append(
            ImportedTarget(
                "icsquartz_scan_cycle",
                bench_id,
                "FUNCTION_BLOCK",
                new,
                out,
                memory_manifest,
                "https://github.com/momalab/ICSQuartz",
                commit,
                src_path.relative_to(icsquartz),
                sha256(src_path),
                "ICSQuartz scan-cycle ST extracted from ICSPatch/industrial-control examples",
                ["removed PROGRAM PLC_PRG harness wrapper", f"renamed target FUNCTION_BLOCK {old} to {new}", "normalized CODESYS string lengths and ADR()"],
            )
        )

    return targets


def import_oscat_targets(icsquartz: Path) -> list[ImportedTarget]:
    commit = git_commit(icsquartz)
    extract_pou = load_extractor()
    oscat_source = (OSCAT / "source" / "oscat.st").read_text(encoding="utf-8", errors="replace")
    targets: list[ImportedTarget] = []
    for function in OSCAT_TARGETS:
        actual, kind, block = extract_pou(oscat_source, function)
        out = OSCAT / ("functions" if kind == "FUNCTION" else "function_blocks") / slug(actual) / "target.st"
        write(out, block)
        bench_id = "oscat_basic_" + function.lower()
        src_path = icsquartz / "benchmarks" / bench_id / "src" / "program.st"
        has_icsquartz_origin = src_path.exists()
        targets.append(
            ImportedTarget(
                "oscat_basic",
                bench_id,
                kind,
                actual,
                out,
                OSCAT / "source" / "compatibility.json",
                "https://github.com/momalab/ICSQuartz" if has_icsquartz_origin else "SemantiST bundled OSCAT Basic source",
                commit if has_icsquartz_origin else f"source-sha256:{sha256(OSCAT / 'source' / 'oscat.st')}",
                src_path.relative_to(icsquartz) if has_icsquartz_origin else Path("benchmarks/oscat_basic/source/oscat.st"),
                sha256(src_path) if has_icsquartz_origin else sha256(OSCAT / "source" / "oscat.st"),
                (
                    "OSCAT Basic target also present in ICSQuartz; extracted verbatim from SemantiST's normalized full OSCAT Basic source"
                    if has_icsquartz_origin
                    else "Additional OSCAT Basic target extracted verbatim from SemantiST's normalized full OSCAT Basic source"
                ),
                ["extracted target POU verbatim from benchmarks/oscat_basic/source/oscat.st"],
            )
        )
    return targets


def write_oscat_manifest(targets: list[ImportedTarget]) -> None:
    entries = []
    for target in targets:
        entries.append(
            {
                "id": slug(target.function),
                "name": target.function,
                "kind": target.kind,
                "function": target.function,
                "st_file": str(target.st_file.relative_to(OSCAT)),
                "source": "source/oscat.st",
                "source_sha256": sha256(OSCAT / "source" / "oscat.st"),
                "compatibility_manifest": "source/compatibility.json",
                "rusty_compile": True,
                "pipeline": True,
                "oracle": "runtime_or_semantic_objective",
            }
        )
    write(
        OSCAT / "manifest.json",
        json.dumps(
            {
                "schema_version": "semantist.oscat-benchmarks/1.0.0",
                "suite": "oscat_basic",
                "description": "Curated OSCAT Basic FUNCTION and FUNCTION_BLOCK targets extracted verbatim from the normalized library.",
                "target_count": len(entries),
                "targets": entries,
            },
            indent=2,
        )
        + "\n",
    )


def write_readme(targets: list[ImportedTarget]) -> None:
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.suite] = counts.get(target.suite, 0) + 1
    lines = [
        "# External ST benchmark suite",
        "",
        "This directory contains SemantiST-runnable OSCAT and ICSQuartz benchmark targets.",
        "",
        "The authoritative index is `manifest.json`. Each entry records upstream repository, commit, original path, SHA-256, target POU, and transformations.",
        "",
        "Counts:",
    ]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines += [
        "",
        "Compatibility notes:",
        "- ICSFuzz and scan-cycle imports use `compatibility/codesys-memory/compatibility.json` for CODESYS memory APIs.",
        "- OSCAT Basic targets use `benchmarks/oscat_basic/source/compatibility.json`.",
        "",
        "Reproduce:",
        "- Regenerate this suite with `python3 benchmarks/external/import_benchmarks.py`.",
        "- Build-check all targets with `python3 benchmarks/external/check_compile_suite.py --keep-going --timeout 420`.",
        "- Build-check one suite with `python3 benchmarks/external/check_compile_suite.py --suite icsquartz_icsfuzz --keep-going`.",
    ]
    write(SUITE / "README.md", "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--icsquartz", type=Path, default=ROOT.parent / "ICSQuartz")
    args = parser.parse_args()
    icsquartz = args.icsquartz.resolve()
    if not icsquartz.is_dir():
        raise SystemExit(f"ICSQuartz repo not found: {icsquartz}")

    memory_manifest = setup_compatibility()
    targets: list[ImportedTarget] = []
    oscat_targets = import_oscat_targets(icsquartz)
    write_oscat_manifest(oscat_targets)
    targets.extend(oscat_targets)
    targets.extend(import_icsquartz(icsquartz, memory_manifest))
    data = {
        "schema_version": "semantist.external-benchmarks/1.0.0",
        "description": "Runnable SemantiST benchmark index imported from related public ST fuzzing artifacts.",
        "target_count": len(targets),
        "targets": [target.to_manifest() for target in targets],
    }
    write(SUITE / "manifest.json", json.dumps(data, indent=2) + "\n")
    write_readme(targets)
    print(f"[external_benchmarks] imported {len(targets)} targets into {SUITE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
