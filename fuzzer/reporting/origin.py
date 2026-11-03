"""Where a crash happened: source program, RuSTy runtime library or compatibility layer.

This is the "cause location" dimension of design section 9.3, decided from the
replayed crash output only (no root-cause analysis). The first frame that is
not part of a sanitizer or the C library decides:

* ``runtime_library`` -- RuSTy's ``libiec61131std`` (Rust frames, or a Rust
  panic in ``libs/stdlib``), e.g. string functions panicking on out-of-range
  arguments or invalid UTF-8;
* ``compatibility_layer`` -- adapters of the compatibility profiles
  (``__CDS_*``, ``__S7_*``, the ``codesys-memory`` runtime) or files below
  ``compiler/toolchain/compatibility``;
* ``source_program`` -- anything else (the normalized library or the harness);
* ``undetermined`` -- no frame and no panic location in the output.

A label other than ``source_program`` means the crash must be attributed to the
toolchain before it is reported as a defect of the tested program.
"""

from __future__ import annotations

import re

FRAME = re.compile(r"^\s*#\d+\s+(?:0x[0-9a-fA-F]+\s+)?(?:in\s+)?(?P<symbol>\S+)(?:\s+(?P<location>\S+))?")
PANIC = re.compile(r"panicked at (?P<location>[^\s:]+)")
IGNORED = ("__asan", "__ubsan", "__sanitizer", "__interceptor", "abort", "raise", "__libc", "__GI_", "_start",
           "__pthread", "gsignal", "std::panicking", "core::panicking", "rust_begin_unwind",
           "__rust_start_panic", "__rustc")
ADAPTER_PREFIXES = ("__CDS_", "__S7_", "__SEMANTIST_MEM", "SysMem")


def classify(output: str) -> dict:
    panic = PANIC.search(output)
    if panic and ("stdlib" in panic["location"] or "iec61131std" in panic["location"]):
        return {"origin": "runtime_library", "evidence": "Rust panic at " + panic["location"]}
    for line in output.splitlines():
        match = FRAME.match(line)
        if not match:
            continue
        symbol = match["symbol"]
        location = match["location"] or ""
        if symbol.startswith(IGNORED) or "libc.so" in location or "libclang_rt" in location:
            continue
        if symbol.startswith(ADAPTER_PREFIXES) or "compiler/toolchain/compatibility" in location:
            return {"origin": "compatibility_layer", "evidence": f"{symbol} {location}".strip()}
        if "iec61131std" in symbol or "iec61131std" in location or "libs/stdlib" in location \
                or symbol.startswith("_ZN") or "::" in symbol:
            return {"origin": "runtime_library", "evidence": f"{symbol} {location}".strip()}
        return {"origin": "source_program", "evidence": f"{symbol} {location}".strip()}
    if panic:
        return {"origin": "runtime_library", "evidence": "Rust panic at " + panic["location"]}
    return {"origin": "undetermined", "evidence": ""}
