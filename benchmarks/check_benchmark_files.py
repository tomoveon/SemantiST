#!/usr/bin/env python3
"""Verify the four generated benchmark files across all toolchains.

Checks (per target):
  1. target.st SHA-256 unchanged (compared against git HEAD).
  2. All four files exist (target.st, program.st, harness.c, structuredfuzzer.st).
  3. RuSTy compiles target.st + program.st (target.st is the compiled POU source).
  4. harness.c links with the RuSTy output.
  5. AFL++ baseline image builds the target.
  6. ICSQuartz baseline image builds with the same harness.c.
  7. StructuredFuzzer baseline image compiles structuredfuzzer.st through the
     real stcompile (iec2c + C compile + link) flow.
  8. Short smoke replay (zero / normal inputs) does not crash in the harness.

The script exits non-zero if any selected stage fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.lib.generator import ROOT, load_targets

SEMANTIST_IMAGE = "localhost/semantist:0.1.0"
AFLPP_IMAGE = "localhost/semantist-aflplusplus-env:4.21c"
ICSQUARTZ_IMAGE = "localhost/semantist-icsquartz-env:8021bd4"
STRUCTUREDFUZZER_IMAGE = "localhost/semantist-structuredfuzzer-env:e648a52"

WORK = ROOT / "artifacts" / "benchmarks" / "harness-work"
STDLIB_LIB = "/opt/rusty-semantic/target/release/libiec61131std.a"

_MATIEC_HARNESS = r'''
#include "iec_types.h"
#include "iec_std_lib.h"
#include "inputs.h"
void set_plc_input(const char *name, const char *type, PLC_Value value) {
    (void)name; (void)type; (void)value;
}
void fuzzer_harness(void) {}
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head_sha(path: Path) -> str | None:
    rel = path.relative_to(ROOT)
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def load_generation_meta() -> dict:
    meta_path = WORK / "generation-meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return {}


def check_files(targets) -> dict:
    meta = load_generation_meta()
    results = {}
    for t in targets:
        d = Path(t.st_file).parent
        files = {name: (d / name).exists() for name in ("target.st", "program.st", "harness.c", "structuredfuzzer.st")}
        current = sha256(Path(t.st_file))
        head = git_head_sha(Path(t.st_file))
        results[t.id] = {
            "all_files": all(files.values()),
            "sha_unchanged": head is not None and current == head,
            "oscat_pou_equivalent": meta.get(t.id, {}).get("oscat_pou_equivalent"),
        }
    return results


def write_rusty_build_script(targets, out: Path) -> None:
    lines = ["set -e", "LLVM=/usr/lib/llvm-21/bin"]
    for t in targets:
        tid = t.id
        w = f"/work/SemantiST/artifacts/benchmarks/harness-work/{tid}"
        d = f"/work/SemantiST/{t.rel_st_file}".replace("target.st", "")
        harness = f"{d}harness.c"
        out_bin = f"/work/SemantiST/artifacts/benchmarks/rusty-build/{tid}/fuzz_target"
        lines += [
            f"mkdir -p /work/SemantiST/artifacts/benchmarks/rusty-build/{tid}",
            f"$LLVM/clang -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer -c {w}/main.ll -o /tmp/{tid}.o",
            f"$LLVM/clang -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer -c {harness} -o /tmp/{tid}_h.o",
        ]
        extra = ""
        if t.suite != "oscat_basic":
            lines.append(
                f"$LLVM/clang -g -O1 -fsanitize=address,undefined -fno-omit-frame-pointer -c "
                f"/work/SemantiST/benchmarks/external/compatibility/codesys-memory/runtime.c -o /tmp/{tid}_rt.o"
            )
            extra = f"/tmp/{tid}_rt.o"
        lines.append(
            f"$LLVM/clang -fsanitize=address,undefined -Wl,--allow-multiple-definition "
            f"/tmp/{tid}_h.o /tmp/{tid}.o {extra} {STDLIB_LIB} -ldl -lpthread -lm -o {out_bin}"
        )
        lines += [
            f"printf '\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00' > /tmp/{tid}_seed0",
            f"printf '\\x01\\x02\\x03\\x04\\x05\\x06\\x07\\x08\\x09\\x0a\\x0b\\x0c\\x0d\\x0e\\x0f\\x10' > /tmp/{tid}_seed1",
            f"set +e",
            f"ASAN_OPTIONS=detect_leaks=0 {out_bin} /tmp/{tid}_seed1 >/tmp/{tid}_out1 2>&1; rc1=$?",
            f"ASAN_OPTIONS=detect_leaks=0 {out_bin} /tmp/{tid}_seed0 >/tmp/{tid}_out0 2>&1; rc0=$?",
            f"set -e",
            f"cat /tmp/{tid}_out1 /tmp/{tid}_out0 > /tmp/{tid}_both 2>/dev/null || true",
            f"if grep -qE '#[12][^#]*harness\\.c' /tmp/{tid}_both; then echo 'HARNESS_CRASH {tid}'; exit 1; fi",
            f"echo 'RUSTY_OK {tid} normal_rc=$rc1 zero_rc=$rc0'",
        ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_aflpp_script(targets, out: Path) -> None:
    lines = ["set -e", "B=/work/SemantiST/artifacts/benchmarks/baseline-build"]
    for t in targets:
        tid = t.id
        d = f"/work/SemantiST/{t.rel_st_file}".replace("target.st", "")
        harness = f"{d}harness.c"
        out_bin = f"$B/afl_{tid}"
        extra = "$B/runtime.o" if t.suite != "oscat_basic" else ""
        lines.append(
            f"afl-clang-fast -fsanitize=address,undefined -fno-omit-frame-pointer {harness} "
            f"$B/{tid}.o {extra} $B/libiec61131std.a -ldl -lpthread -lm "
            f"-Wl,--allow-multiple-definition -o {out_bin} >/tmp/afl_{tid}.log 2>&1 || {{ echo 'AFL_BUILD_FAIL {tid}'; tail -5 /tmp/afl_{tid}.log; exit 1; }}"
        )
        lines.append(f"echo 'AFL_BUILD_OK {tid}'")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_icsquartz_script(targets, out: Path) -> None:
    lines = ["set -e", "B=/work/SemantiST/artifacts/benchmarks/baseline-build"]
    for t in targets:
        tid = t.id
        d = f"/work/SemantiST/{t.rel_st_file}".replace("target.st", "")
        harness = f"{d}harness.c"
        out_bin = f"$B/ics_{tid}"
        extra = "$B/runtime.o" if t.suite != "oscat_basic" else ""
        lines.append(
            f"/opt/icsquartz/target/release/libafl_cxx -DSEMANTIST_HARNESS_NO_MAIN "
            f"-fsanitize=address,undefined -fno-omit-frame-pointer {harness} "
            f"$B/{tid}.o {extra} $B/libiec61131std.a -ldl -lpthread -lm "
            f"-Wl,--allow-multiple-definition -o {out_bin} >/tmp/ics_{tid}.log 2>&1 || {{ echo 'ICS_BUILD_FAIL {tid}'; tail -8 /tmp/ics_{tid}.log; exit 1; }}"
        )
        lines.append(f"echo 'ICS_BUILD_OK {tid}'")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_structuredfuzzer_script(targets, out: Path) -> None:
    """Run the real stcompile flow (iec2c + C compile + link) per target."""
    lines = ["set -e", "FAILED=0"]
    for t in targets:
        tid = t.id
        d = f"/work/SemantiST/{t.rel_st_file}".replace("target.st", "")
        sf = f"{d}structuredfuzzer.st"
        build_dir = f"/work/SemantiST/artifacts/benchmarks/sf-build/{tid}"
        lines += [
            f"rm -rf {build_dir} && mkdir -p {build_dir}",
            f"cp {sf} {build_dir}/structuredfuzzer.st",
            f"cat > {build_dir}/harness.c <<'HARNESS_EOF'",
            _MATIEC_HARNESS,
            "HARNESS_EOF",
            f"cd {build_dir}",
            f"if stcompile structuredfuzzer.st harness.c -o build -n fuzz_target --no-analysis >/tmp/sf_{tid}.log 2>&1; then "
            f"  echo 'SF_PASS {tid}'; "
            f"else "
            f"  echo 'SF_FAIL {tid}: '$(grep -m1 -iE 'error' /tmp/sf_{tid}.log | head -c 140); FAILED=1; "
            f"fi",
            f"cd /work/SemantiST",
        ]
    lines.append('if [ "$FAILED" -eq 1 ]; then echo SF_HAS_FAILURES; exit 1; fi')
    lines.append("echo ALL_SF_OK")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_in_image(image: str, script: str, timeout=1800) -> tuple[int, str]:
    cmd = [
        "podman", "run", "--rm",
        "-v", f"{ROOT}:/work/SemantiST",
        "-w", "/work/SemantiST",
        image, "bash", script,
    ]
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return result.returncode, result.stdout or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", action="append", default=[])
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--runtime", default="podman")
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--rusty-build", action="store_true")
    parser.add_argument("--aflpp-build", action="store_true")
    parser.add_argument("--icsquartz-build", action="store_true")
    parser.add_argument("--structuredfuzzer-build", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    targets = load_targets()
    if args.suite:
        targets = [t for t in targets if t.suite in args.suite]
    if args.id:
        targets = [t for t in targets if t.id in args.id or t.function in args.id]

    if args.all:
        args.check_files = True
        args.rusty_build = True
        args.aflpp_build = True
        args.icsquartz_build = True
        args.structuredfuzzer_build = True

    report: dict = {}
    overall_fail = 0

    if args.check_files:
        results = check_files(targets)
        report["files"] = results
        ok = sum(1 for r in results.values() if r["all_files"] and r["sha_unchanged"])
        print(f"[check] files+sha ok={ok}/{len(targets)}")
        if ok != len(targets):
            overall_fail = 1

    if args.rusty_build:
        script_path = WORK / "rusty_build_all.sh"
        write_rusty_build_script(targets, script_path)
        print("[rusty-build] compiling + linking + smoking ...")
        rc, stdout = run_in_image(SEMANTIST_IMAGE, f"/work/SemantiST/artifacts/benchmarks/harness-work/rusty_build_all.sh")
        sys.stdout.write(stdout)
        report["rusty_build"] = {"returncode": rc, "ok": stdout.count("RUSTY_OK")}
        print(f"[rusty-build] returncode={rc}")
        if rc != 0:
            overall_fail = 1

    if args.aflpp_build:
        script_path = WORK / "aflpp_build_all.sh"
        write_aflpp_script(targets, script_path)
        print("[aflpp-build] building in AFL++ baseline image ...")
        rc, stdout = run_in_image(AFLPP_IMAGE, f"/work/SemantiST/artifacts/benchmarks/harness-work/aflpp_build_all.sh")
        sys.stdout.write(stdout)
        report["aflpp_build"] = {"returncode": rc, "ok": stdout.count("AFL_BUILD_OK")}
        print(f"[aflpp-build] returncode={rc}")
        if rc != 0:
            overall_fail = 1

    if args.icsquartz_build:
        script_path = WORK / "icsquartz_build_all.sh"
        write_icsquartz_script(targets, script_path)
        print("[icsquartz-build] building in ICSQuartz baseline image ...")
        rc, stdout = run_in_image(ICSQUARTZ_IMAGE, f"/work/SemantiST/artifacts/benchmarks/harness-work/icsquartz_build_all.sh")
        sys.stdout.write(stdout)
        report["icsquartz_build"] = {"returncode": rc, "ok": stdout.count("ICS_BUILD_OK")}
        print(f"[icsquartz-build] returncode={rc}")
        if rc != 0:
            overall_fail = 1

    if args.structuredfuzzer_build:
        script_path = WORK / "sf_build_all.sh"
        write_structuredfuzzer_script(targets, script_path)
        print("[structuredfuzzer-build] compiling structuredfuzzer.st via stcompile ...")
        rc, stdout = run_in_image(STRUCTUREDFUZZER_IMAGE, f"/work/SemantiST/artifacts/benchmarks/harness-work/sf_build_all.sh")
        sys.stdout.write(stdout)
        report["structuredfuzzer_build"] = {
            "returncode": rc,
            "pass": stdout.count("SF_PASS"),
            "fail": stdout.count("SF_FAIL"),
        }
        print(f"[structuredfuzzer-build] returncode={rc}")
        if rc != 0:
            overall_fail = 1

    out = ROOT / "artifacts" / "benchmarks" / "check-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[check] overall {'FAIL' if overall_fail else 'PASS'}")
    return overall_fail


if __name__ == "__main__":
    raise SystemExit(main())
