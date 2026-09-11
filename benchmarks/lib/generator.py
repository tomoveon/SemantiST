"""Generate program.st, structuredfuzzer.st, and harness.c for benchmarks.

For each benchmark in benchmarks/external/manifest.json this module produces:

- ``program.st``: a Rusty-compatible ``PROGRAM PLC_PRG`` wrapper that declares
  the target's inputs as VAR_INPUT (in declaration order), calls the target
  POU, and stores the result/outputs.
- ``structuredfuzzer.st``: a matiec/StructuredFuzzer-compatible standalone ST
  file containing the target POU, its minimal library dependencies, the
  PLC_PRG wrapper, and a CONFIGURATION block.
- ``harness.c``: a precise C harness (LLVMFuzzerTestOneInput + ICSQuartz
  scan-cycle symbols) whose struct layout is derived from the RuSTy LLVM IR.

The harness needs the RuSTy LLVM IR, so harness generation is a separate pass
that runs after ``program.st`` is compiled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from benchmarks.lib.llvm_ir import parse_named_definitions, struct_field_layout, struct_size
from benchmarks.lib.st_extract import Signature, extract_signature, resolve_dependencies

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "benchmarks" / "external" / "manifest.json"
OSCAT_SRC = ROOT / "benchmarks" / "oscat_basic" / "source" / "oscat.st"
STUBS_SRC = ROOT / "benchmarks" / "oscat_basic" / "source" / "stubs.st"
CODESYS_INTERFACES = ROOT / "benchmarks" / "external" / "compatibility" / "codesys-memory" / "interfaces.st"

MATIEC_STDLIB_NAMES = {
    line.strip().upper()
    for line in (ROOT / "benchmarks" / "lib" / "matiec_stdlib_names.txt").read_text().splitlines()
    if line.strip()
}

RESULT_VAR = "semantist_result"
FB_VAR = "semantist_fb"


def _out_var(name: str) -> str:
    return f"semantist_out_{name}"


def _sanitize_ident(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def build_program_st(sig: Signature) -> str:
    inputs = "\n".join(f"    {d.name} : {d.type_raw};" for d in sig.all_inputs)
    if sig.kind == "FUNCTION":
        body_var = f"    {RESULT_VAR} : {sig.ret_type_raw};"
        args = ", ".join(f"{d.name} := {d.name}" for d in sig.all_inputs)
        body = f"{RESULT_VAR} := {sig.name}({args});"
    else:
        out_vars = "\n".join(f"    {_out_var(d.name)} : {d.type_raw};" for d in sig.outputs)
        body_var = f"    {FB_VAR} : {sig.name};\n{out_vars}"
        args = ", ".join(f"{d.name} := {d.name}" for d in sig.all_inputs)
        call = f"{FB_VAR}({args});"
        assigns = "\n".join(f"{_out_var(d.name)} := {FB_VAR}.{d.name};" for d in sig.outputs)
        body = "\n".join([call, assigns]) if assigns else call

    return (
        "PROGRAM PLC_PRG\n"
        "VAR_INPUT\n"
        f"{inputs}\n"
        "END_VAR\n"
        "VAR\n"
        f"{body_var}\n"
        "END_VAR\n"
        f"{body}\n"
        "END_PROGRAM\n"
    )


def _collect_constants(suite: str, globals_raw: str) -> dict[str, int]:
    """Collect integer constant values for matiec array-bound resolution."""
    constants: dict[str, int] = {}
    blocks: list[str] = [globals_raw]
    if suite == "oscat_basic":
        oscat = OSCAT_SRC.read_text(encoding="utf-8")
        for match in re.finditer(r"(?is)\bVAR_GLOBAL\b[ \t]*CONSTANT\b.*?END_VAR\b", oscat):
            blocks.append(match.group(0))
    for block in blocks:
        for match in re.finditer(
            r"(?im)^[ \t]*([A-Z_][A-Z0-9_]*)[ \t]*:[^;:=]*:=[ \t]*([+-]?[0-9]+)\s*;",
            block,
        ):
            constants[match.group(1).upper()] = int(match.group(2))
    return constants


def _collect_local_constants(text: str) -> dict[str, int]:
    """Collect VAR CONSTANT block initializers (array-size constants like n:=16)."""
    constants: dict[str, int] = {}
    for block in re.findall(r"(?is)\bVAR\b[ \t]*CONSTANT\b.*?END_VAR\b", text):
        for match in re.finditer(
            r"(?im)^[ \t]*([A-Z_][A-Z0-9_]*)[ \t]*:[^;:=]*:=[ \t]*([+-]?[0-9]+)\s*;",
            block,
        ):
            constants[match.group(1).upper()] = int(match.group(2))
    return constants


def _matiec_compat(text: str, constants: dict[str, int], local_constants: dict[str, int]) -> str:
    """Apply matiec-required, semantics-preserving ST compatibility transforms.

    - ``constants`` are global constants (VAR_GLOBAL CONSTANT, dropped before
      compile) and are resolved everywhere (CASE labels, FOR bounds, ...).
    - ``local_constants`` are VAR CONSTANT block values (array-size constants)
      and are resolved only inside array range bounds ``[a..b]`` so that their
      declaration lines are left intact.
    """
    # 1. limited strings -> plain STRING
    text = re.sub(r"\bSTRING\s*\[[^\]]*\]", "STRING", text)
    # 2. `//` line comments -> block comments
    text = re.sub(r"(?m)//(.*)$", r"(* \1 *)", text)
    # 3. resolve local constants inside array range bounds [a..b]
    def sub_range(match: re.Match) -> str:
        inner = match.group(0)
        for name, value in sorted(local_constants.items(), key=lambda kv: -len(kv[0])):
            inner = re.sub(rf"\b{re.escape(name)}\b", str(value), inner, flags=re.IGNORECASE)
        return inner

    text = re.sub(r"\[[0-9A-Za-z_+\-]+\s*\.\.[^\]]*\]", sub_range, text)
    # 4. resolve global constants everywhere (their VAR_GLOBAL block is dropped)
    for name, value in sorted(constants.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(name)}\b", str(value), text, flags=re.IGNORECASE)
    # 5. VAR modifier pragmas / keywords
    text = re.sub(r"\(\*\s*(?:CONSTANT|RETAIN|PERSISTENT|NON_RETAIN)\s*\*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR)\s+(?:CONSTANT|RETAIN|PERSISTENT|NON_RETAIN)\b",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    # 6. strip VAR_INPUT initializers (defaults are never used by the wrapper)
    def strip_input_block(match: re.Match) -> str:
        return re.sub(r":=\s*[^;]*;", ";", match.group(0))

    text = re.sub(r"(?is)\bVAR_INPUT\b.*?END_VAR\b", strip_input_block, text)
    # 7. merge consecutive VAR_INPUT blocks (OSCAT uses VAR_INPUT (* CONSTANT *)
    #    for a second input block, which matiec rejects as a duplicate block).
    text = re.sub(r"END_VAR\s*\n\s*VAR_INPUT\s*\n", "\n", text)
    return text


def build_structuredfuzzer_st(target_dir: Path, sig: Signature, target_pou_text: str,
                              suite: str) -> tuple[str, list[str]]:
    """Return (structuredfuzzer.st text, list of unresolved/missing deps)."""
    if suite == "oscat_basic":
        libraries = [OSCAT_SRC.read_text(encoding="utf-8"), STUBS_SRC.read_text(encoding="utf-8")]
    else:
        libraries = [CODESYS_INTERFACES.read_text(encoding="utf-8")]

    dep_texts, unresolved = resolve_dependencies(
        sig.name, target_pou_text, libraries, MATIEC_STDLIB_NAMES
    )

    constants = _collect_constants(suite, sig.globals_raw)

    globals_text = sig.globals_raw

    inputs = "\n".join(f"    {d.name} : {d.type_raw};" for d in sig.all_inputs)
    if sig.kind == "FUNCTION":
        body_var = f"    {RESULT_VAR} : {sig.ret_type_raw};"
        args = ", ".join(f"{d.name} := {d.name}" for d in sig.all_inputs)
        body = f"{RESULT_VAR} := {sig.name}({args});"
    else:
        out_vars = "\n".join(f"    {_out_var(d.name)} : {d.type_raw};" for d in sig.outputs)
        body_var = f"    {FB_VAR} : {sig.name};\n{out_vars}"
        args = ", ".join(f"{d.name} := {d.name}" for d in sig.all_inputs)
        call = f"{FB_VAR}({args});"
        assigns = "\n".join(f"{_out_var(d.name)} := {FB_VAR}.{d.name};" for d in sig.outputs)
        body = "\n".join([call, assigns]) if assigns else call

    config = (
        "\n\nCONFIGURATION STD_CONF\n"
        "  RESOURCE STD_RESSOURCE ON PLC\n"
        "    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);\n"
        "    PROGRAM Inst0 WITH TaskMain : PLC_PRG;\n"
        "  END_RESOURCE\n"
        "END_CONFIGURATION\n"
    )

    parts: list[str] = []
    for dep in dep_texts:
        parts.append(dep)
    parts.append(target_pou_text)
    wrapper = (
        "PROGRAM PLC_PRG\n"
        "VAR_INPUT\n"
        f"{inputs}\n"
        "END_VAR\n"
        "VAR\n"
        f"{body_var}\n"
        "END_VAR\n"
        f"{body}\n"
        "END_PROGRAM\n"
    )
    parts.append(wrapper)

    joined = "\n\n".join(parts) + config
    # Collect local VAR CONSTANT block constants and resolve everything.
    local_constants = _collect_local_constants(joined)
    # Drop VAR_GLOBAL blocks (matiec rejects them at file scope); the global
    # constants they define are resolved to literals in _matiec_compat.
    joined = re.sub(r"(?is)\bVAR_GLOBAL\b(?:[ \t]*(?:CONSTANT|RETAIN|PERSISTENT|NON_RETAIN))?\s*.*?END_VAR\s*", "", joined)
    joined = _matiec_compat(joined, constants, local_constants)
    # Convert VAR CONSTANT blocks to plain VAR (their array-size uses are literals now).
    joined = re.sub(r"\bVAR\b[ \t]*CONSTANT\b", "VAR", joined, flags=re.IGNORECASE)
    return joined, unresolved


def load_stdlib_names() -> set[str]:
    return MATIEC_STDLIB_NAMES


def plc_prg_layout(ll_text: str, num_input_fields: int) -> tuple[int, int, list[tuple[int, int, bool]]]:
    """Return (instance_size, input_size, [(offset, size, is_pointer), ...]).

    ``input_size`` is the byte offset of the first state field (i.e. the size of
    the contiguous input region including trailing alignment padding).
    ``is_pointer`` is True for REF_TO / POINTER input fields, whose fuzz bytes
    must fill an allocated buffer rather than the 8-byte pointer slot itself.
    """
    definitions = parse_named_definitions(ll_text)
    plc = definitions.get("%PLC_PRG")
    if plc is None or plc.kind != "struct":
        raise ValueError("could not find %PLC_PRG struct in LLVM IR")
    layout = struct_field_layout(plc.fields, definitions)
    total = struct_size(plc.fields, definitions)
    inputs = [(off, size, ty.kind == "ptr") for (off, size, ty) in layout[:num_input_fields]]
    if num_input_fields < len(layout):
        input_size = layout[num_input_fields][0]
    else:
        input_size = total
    return total, input_size, inputs


_HARNESS_TEMPLATE = """/* Auto-generated precise C harness for the RuSTy-compiled PLC_PRG wrapper.
 * The struct size and input field offsets below are derived from the LLVM IR
 * produced by compiling target.st + program.st.  Do not edit by hand; regenerate
 * with benchmarks/generate_benchmark_files.py. */

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#define SEMANTIST_INSTANCE_SIZE {instance_size}
#define SEMANTIST_INPUT_SIZE    {input_size}

#ifdef __cplusplus
extern "C" {{
#endif
void PLC_PRG(void *instance);
void PLC_PRG__ctor(void *instance);
extern unsigned char PLC_PRG_instance[SEMANTIST_INSTANCE_SIZE];
#ifdef __cplusplus
}}
#endif

/* ICSQuartz scan-cycle / sizing symbols. */
size_t PLC_PRG_instance_size = SEMANTIST_INSTANCE_SIZE;
size_t PLC_PRG_input_size = SEMANTIST_INPUT_SIZE;
unsigned scan_cycle = 0;
unsigned scan_cycle_max = 2;

static unsigned char semantist_fuzzer_instance[SEMANTIST_INSTANCE_SIZE];

uint8_t *program_state_fresh = PLC_PRG_instance + SEMANTIST_INPUT_SIZE;
uint8_t *program_state_start = semantist_fuzzer_instance + SEMANTIST_INPUT_SIZE;
uint8_t *program_state_end = semantist_fuzzer_instance + SEMANTIST_INSTANCE_SIZE;
unsigned program_state_size = SEMANTIST_INSTANCE_SIZE - SEMANTIST_INPUT_SIZE;

{pointer_buffers}

int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {{
    static int semantist_initialized = 0;
    if (!semantist_initialized) {{
        PLC_PRG__ctor(PLC_PRG_instance);
        memcpy(semantist_fuzzer_instance, PLC_PRG_instance, SEMANTIST_INSTANCE_SIZE);
        semantist_initialized = 1;
    }}

{reset_lines}

    /* Write fuzz bytes into explicit input fields only (never padding). */
    size_t off = 0;
{field_writes}

    PLC_PRG(semantist_fuzzer_instance);

    /* Consume the output/state region so the run is not dead-code eliminated. */
    volatile uint8_t sink = 0;
    for (size_t i = SEMANTIST_INPUT_SIZE; i < SEMANTIST_INSTANCE_SIZE; i++) {{
        sink ^= semantist_fuzzer_instance[i];
    }}
    (void)sink;
    return 0;
}}

#ifndef SEMANTIST_HARNESS_NO_MAIN
int main(int argc, char **argv) {{
    FILE *fp = stdin;
    if (argc >= 2) {{
        fp = fopen(argv[1], "rb");
        if (!fp) {{
            return 1;
        }}
    }}
    unsigned char *buf = (unsigned char *)malloc(1 << 20);
    if (!buf) {{
        if (argc >= 2) fclose(fp);
        return 1;
    }}
    size_t n = fread(buf, 1, 1 << 20, fp);
    if (argc >= 2) {{
        fclose(fp);
    }}
    LLVMFuzzerTestOneInput(buf, n);
    free(buf);
    return 0;
}}
#endif
"""


_SCALAR_TYPE_SIZES = {
    "BOOL": 1, "SINT": 1, "USINT": 1, "BYTE": 1, "CHAR": 1, "WCHAR": 2,
    "INT": 2, "UINT": 2, "WORD": 2, "DINT": 4, "UDINT": 4, "DWORD": 4,
    "LINT": 8, "ULINT": 8, "LWORD": 8, "REAL": 4, "LREAL": 8, "TIME": 8,
    "LTIME": 8, "DATE": 8, "DT": 8, "TOD": 8, "DATE_AND_TIME": 8,
    "TIME_OF_DAY": 8, "LDATE": 8, "LTOD": 8,
}

POINTER_ELEMENT_CAP = 4096


def pointer_element_size(type_raw: str) -> int:
    """Return the byte size of the pointee scalar type (e.g. REF_TO ... BYTE -> 1)."""
    for token in reversed(re.findall(r"[A-Za-z_]+", type_raw)):
        size = _SCALAR_TYPE_SIZES.get(token.upper())
        if size is not None:
            return size
    return 1


def build_harness_c(
    instance_size: int,
    input_size: int,
    input_fields: list[tuple[int, int, bool]],
    pointer_sizes: list[int] | None = None,
    scan_cycle: bool = False,
) -> str:
    pointer_sizes = pointer_sizes or []
    pointer_buffers: list[str] = []
    field_writes: list[str] = []
    ptr_idx = 0
    for idx, (off, size, is_pointer) in enumerate(input_fields):
        if is_pointer:
            buf = f"semantist_ptr_buf_{idx}"
            buf_size = pointer_sizes[ptr_idx] if ptr_idx < len(pointer_sizes) else 1
            ptr_idx += 1
            pointer_buffers.append(f"static unsigned char {buf}[{buf_size}];")
            field_writes.append(
                f"    if (off < Size) {{\n"
                f"        size_t n = {buf_size};\n"
                f"        if (n > Size - off) n = Size - off;\n"
                f"        memcpy({buf}, Data + off, n);\n"
                f"        memset({buf} + n, 0, {buf_size} - n);\n"
                f"        off += n;\n"
                f"    }} else {{\n"
                f"        memset({buf}, 0, {buf_size});\n"
                f"    }}\n"
                f"    {{ void *semantist_ptr = {buf}; memcpy(semantist_fuzzer_instance + {off}, &semantist_ptr, sizeof(semantist_ptr)); }}"
            )
        else:
            field_writes.append(
                f"    if (off < Size) {{\n"
                f"        size_t n = {size};\n"
                f"        if (n > Size - off) n = Size - off;\n"
                f"        memcpy(semantist_fuzzer_instance + {off}, Data + off, n);\n"
                f"        off += n;\n"
                f"    }}"
            )
    if scan_cycle:
        reset_lines = (
            "    /* Scan-cycle model: state persists across executions; only the\n"
            "     * input region is refreshed.  ICSQuartz's ScanCycleResetFeedback\n"
            "     * copies program_state_fresh -> program_state_start on stale state. */\n"
            "    scan_cycle++;\n"
            "    memset(semantist_fuzzer_instance, 0, SEMANTIST_INPUT_SIZE);"
        )
    else:
        reset_lines = (
            "    /* Restore the pristine (constructed) instance state every execution. */\n"
            "    memcpy(semantist_fuzzer_instance, PLC_PRG_instance, SEMANTIST_INSTANCE_SIZE);"
        )
    return _HARNESS_TEMPLATE.format(
        instance_size=instance_size,
        input_size=input_size,
        pointer_buffers="\n".join(pointer_buffers),
        reset_lines=reset_lines,
        field_writes="\n".join(field_writes),
    )


@dataclass
class Target:
    suite: str
    id: str
    kind: str
    function: str
    st_file: str
    rel_st_file: str
    compatibility_manifest: str | None
    directory: Path


def load_targets() -> list[Target]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets: list[Target] = []
    for entry in data["targets"]:
        st_file = ROOT / entry["st_file"]
        targets.append(
            Target(
                suite=entry["suite"],
                id=entry["id"],
                kind=entry["kind"],
                function=entry["function"],
                st_file=str(st_file),
                rel_st_file=entry["st_file"],
                compatibility_manifest=entry.get("compatibility_manifest"),
                directory=st_file.parent,
            )
        )
    return targets
