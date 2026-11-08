"""Generate the neutral Common PLC Harness.

The common harness is deliberately smaller than the SemantiST harness.  It
accepts one fixed-size binary ABI record, copies the record into typed locals
and invokes the selected POU once.  There is no text protocol, cycle prefix,
semantic callback, solver integration, or search policy in this module.

The ABI is still obtained from the compiler's STG model and LLVM layout.  The
resulting C file is suitable for AFL++ and for an ICSQuartz bridge; a driver
owns any multi-cycle scheduling around ``common_harness_execute``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from compiler.harness.abi import (
    AbiError,
    Param,
    TargetAbi,
    TypeSpec,
    frame_params,
    load_target_abi,
    shared_params,
)
from compiler.harness.contract import FieldLayout, POINTER_STORAGE_CAPACITY, region_layout


def _identifier(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return f"common_{value.lower()}"


def _field_name(param: Param) -> str:
    return f"f{param.index - 1}" if param.index is not None else _identifier(param.name)


def _value_name(param: Param) -> str:
    return _identifier(param.name)


def _decl(spec: TypeSpec, name: str, *, pointer_field: bool = False) -> str:
    if pointer_field or spec.kind == "pointer":
        return f"void *{name};"
    if spec.kind == "scalar":
        return f"{spec.c_type} {name};"
    if spec.kind == "string":
        return f"{spec.c_type} {name}[{spec.length + 1}];"
    if spec.kind == "array":
        alignment = (f" __attribute__((aligned({spec.align})))"
                     if spec.align > 1 and spec.flat_c_type() == "uint8_t" else "")
        return f"{spec.flat_c_type()} {name}[{max(1, spec.flat_count())}]{alignment};"
    alignment = f" __attribute__((aligned({spec.align})))" if spec.align > 1 else ""
    return f"uint8_t {name}[{max(1, spec.byte_size())}]{alignment};"


def _local_decl(spec: TypeSpec, name: str) -> list[str]:
    if spec.kind == "pointer":
        return [f"    uint8_t {name}_storage[{max(POINTER_STORAGE_CAPACITY, spec.pointee_bytes())}] = {{0}};"]
    if spec.kind == "scalar":
        return [f"    {spec.c_type} {name} = 0;"]
    if spec.kind == "string":
        return [f"    {spec.c_type} {name}[{spec.length + 1}] = {{0}};"]
    return [f"    {spec.flat_c_type()} {name}[{max(1, spec.flat_count())}] = {{0}};"]


def _raw_decl(spec: TypeSpec, name: str) -> str:
    """Declare one field of the fixed raw input record."""
    if spec.kind == "pointer":
        return f"    uint8_t {name}[{POINTER_STORAGE_CAPACITY}];"
    return "    " + _decl(spec, name)


@dataclass(frozen=True)
class InputField:
    param: Param
    offset: int
    size: int
    align: int


def input_params(abi: TargetAbi) -> list[Param]:
    """Return raw fields in a stable, tool-neutral order."""
    if abi.kind == "FUNCTION":
        return [param for param in abi.params
                if param.block_kind in ("VAR_INPUT", "VAR_IN_OUT")]
    fields: list[Param] = []
    seen: set[str] = set()
    for param in [*frame_params(abi), *shared_params(abi)]:
        key = param.name.upper()
        if key not in seen:
            fields.append(param)
            seen.add(key)
    return fields


def _field_size_align(param: Param) -> tuple[int, int]:
    if param.spec.kind == "pointer":
        return POINTER_STORAGE_CAPACITY, 1
    return param.spec.byte_size(), max(1, param.spec.align)


def input_layout(abi: TargetAbi) -> tuple[int, int, list[InputField]]:
    fields = []
    for param in input_params(abi):
        size, align = _field_size_align(param)
        fields.append((param, size, align))
    total, alignment, layout = region_layout(
        [FieldLayout(param.name, size, align) for param, size, align in fields]
    )
    # Keep a one-byte record for no-input POUs so AFL++ and LibAFL always have
    # a legal corpus file and the generated C struct is never zero-sized.
    total = max(1, total)
    return total, alignment, [InputField(param, entry["offset"], size, align)
                              for (param, size, align), entry in zip(fields, layout)]


def _copy_from_input(field: InputField, destination: str) -> str:
    return f"    memcpy(&{destination}, input + {field.offset}, {field.size});"


def _input_struct(fields: list[InputField], size: int) -> str:
    lines = ["typedef struct {"]
    cursor = 0
    padding = 0
    for field in fields:
        if field.offset > cursor:
            lines.append(f"    uint8_t common_padding_{padding}[{field.offset - cursor}];")
            padding += 1
        lines.append(_raw_decl(field.param.spec, _identifier(field.param.name)))
        cursor = field.offset + field.size
    if cursor < size:
        lines.append(f"    uint8_t common_padding_{padding}[{size - cursor}];")
    lines.append("} common_input_t;")
    lines.append(f"_Static_assert(sizeof(common_input_t) == {size}, \"common input size\");")
    for field in fields:
        lines.append(
            f"_Static_assert(offsetof(common_input_t, {_identifier(field.param.name)}) == {field.offset}, "
            f"\"common input {field.param.name} offset\");"
        )
    return "\n".join(lines)


COMMON_C = r'''
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define COMMON_DRIVER __attribute__((no_sanitize("coverage"), noinline))
#define COMMON_INPUT __attribute__((noinline))
#ifndef COMMON_LOOP_BACKEDGES_PER_SCAN
#define COMMON_LOOP_BACKEDGES_PER_SCAN 1000000ULL
#endif
static uint64_t common_loop_ticks;

void __common_loop_tick(void) {
    if (++common_loop_ticks > (uint64_t)COMMON_LOOP_BACKEDGES_PER_SCAN) abort();
}

static void common_escape_bytes(const void *data, size_t size) {
    volatile const uint8_t *bytes = (const uint8_t *)data;
    volatile uint8_t sink = 0;
    for (size_t index = 0; index < size; index++) sink ^= bytes[index];
    (void)sink;
}

static uint8_t *common_read_all(FILE *stream, size_t *length) {
    size_t capacity = 4096;
    size_t used = 0;
    uint8_t *buffer = (uint8_t *)malloc(capacity);
    if (!buffer) return NULL;
    for (;;) {
        if (used == capacity) {
            capacity *= 2;
            uint8_t *next = (uint8_t *)realloc(buffer, capacity);
            if (!next) { free(buffer); return NULL; }
            buffer = next;
        }
        size_t count = fread(buffer + used, 1, capacity - used, stream);
        used += count;
        if (count == 0) break;
    }
    *length = used;
    return buffer;
}
'''


def _function_harness(abi: TargetAbi, size: int, fields: list[InputField]) -> str:
    name = abi.name
    field_by_name = {field.param.name.upper(): field for field in fields}
    declarations: list[str] = []
    arguments: list[str] = []
    loads: list[str] = []
    escapes: list[str] = []
    for slot in abi.call_slots:
        param = slot.param
        value = _value_name(param)
        declarations.extend(_local_decl(param.spec, value))
        if param.spec.kind == "pointer":
            argument = f"{value}_storage" if param.block_kind == "VAR_INPUT" else f"&{value}_storage"
            storage = f"{value}_storage"
            if param.name.upper() in field_by_name:
                field = field_by_name[param.name.upper()]
                loads.append(f"    memcpy({storage}, input + {field.offset}, {min(field.size, POINTER_STORAGE_CAPACITY)});")
        elif param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT"):
            argument = f"&{value}"
            field = field_by_name.get(param.name.upper())
            if field is not None:
                loads.append(_copy_from_input(field, value))
        else:
            argument = value
            field = field_by_name.get(param.name.upper())
            if field is not None:
                loads.append(_copy_from_input(field, value))
        arguments.append(argument)
        if param.block_kind in ("VAR_OUTPUT", "VAR_IN_OUT") and param.spec.kind != "pointer":
            escapes.append(f"    common_escape_bytes(&{value}, sizeof({value}));")

    ret_type = "void" if abi.ret_by_value is None else abi.ret.spec.c_type
    proto = ", ".join("void *" if slot.ir_type.kind == "ptr" else slot.param.spec.c_type
                       for slot in abi.call_slots) or "void"
    call_args = ", ".join(arguments)
    if abi.ret_by_value is not None:
        call = f"    volatile {ret_type} common_result = {name}({call_args});\n    common_escape_bytes(&common_result, sizeof(common_result));"
    else:
        call = f"    {name}({call_args});"
    free_pointer = "\n".join(
        f"    (void){_value_name(slot.param)}_storage;"
        for slot in abi.call_slots if slot.param.spec.kind == "pointer"
    )
    return f'''\
extern {ret_type} {name}({proto});

COMMON_INPUT static int common_run_record(const uint8_t *input) {{
{chr(10).join(declarations)}
{chr(10).join(loads)}
{call}
{chr(10).join(escapes)}
{free_pointer}
    return 0;
}}
'''


def _fb_harness(abi: TargetAbi, size: int, fields: list[InputField]) -> str:
    name = abi.name
    declarations = []
    for param in abi.state_fields:
        declarations.append("    " + _decl(
            param.spec, _field_name(param),
            pointer_field=param.block_kind == "VAR_IN_OUT" or param.spec.kind == "pointer"))
    storage: list[str] = []
    setup: list[str] = []
    reset_storage: list[str] = []
    field_by_name = {field.param.name.upper(): field for field in fields}
    for param in abi.params:
        if param.block_kind != "VAR_IN_OUT" and param.spec.kind != "pointer":
            continue
        value = _field_name(param)
        if param.spec.kind == "pointer":
            storage.append(f"static uint8_t common_{value}_storage[{max(POINTER_STORAGE_CAPACITY, param.spec.pointee_bytes())}];")
            reset_storage.append(f"    memset(common_{value}_storage, 0, sizeof(common_{value}_storage));")
        elif param.spec.kind == "scalar":
            storage.append(f"static {param.spec.c_type} common_{value}_storage;")
            reset_storage.append(f"    memset(&common_{value}_storage, 0, sizeof(common_{value}_storage));")
        elif param.spec.kind == "string":
            storage.append(f"static {param.spec.c_type} common_{value}_storage[{param.spec.length + 1}];")
            reset_storage.append(f"    memset(common_{value}_storage, 0, sizeof(common_{value}_storage));")
        else:
            storage.append(f"static {param.spec.flat_c_type()} common_{value}_storage[{max(1, param.spec.flat_count())}];")
            reset_storage.append(f"    memset(common_{value}_storage, 0, sizeof(common_{value}_storage));")
        storage_expr = (f"&common_{value}_storage"
                        if param.block_kind == "VAR_IN_OUT" and param.spec.kind == "scalar"
                        else f"common_{value}_storage")
        setup.append(f"    common_fb_instance.{value} = {storage_expr};")
        field = field_by_name.get(param.name.upper())
        if field is not None:
            destination = (f"&common_{value}_storage"
                           if param.spec.kind == "scalar" else f"common_{value}_storage")
            if param.spec.kind == "pointer":
                setup.append(f"    memset({destination}, 0, sizeof({destination}));")
            setup.append(f"    memcpy({destination}, input + {field.offset}, {min(field.size, POINTER_STORAGE_CAPACITY) if param.spec.kind == 'pointer' else field.size});")
    for field in fields:
        param = field.param
        if param.block_kind == "VAR_IN_OUT" or param.spec.kind == "pointer":
            continue
        setup.append(_copy_from_input(field, f"common_fb_instance.{_field_name(param)}"))
    output_escapes = "\n".join(
        f"    common_escape_bytes(&common_fb_instance.{_field_name(param)}, sizeof(common_fb_instance.{_field_name(param)}));"
        for param in abi.outputs
    ) or "    common_escape_bytes(&common_fb_instance, sizeof(common_fb_instance));"
    return f'''\
typedef struct {{
    void *__vtable;
{chr(10).join(declarations)}
}} common_fb_state_t;
_Static_assert(sizeof(common_fb_state_t) == {abi.instance_size}, "{name} instance size");
{chr(10).join(f'_Static_assert(offsetof(common_fb_state_t, {_field_name(param)}) == {param.offset}, "{name}.{param.name} offset");' for param in abi.state_fields)}

extern void {name}(common_fb_state_t *);
extern void {name}__ctor(common_fb_state_t *);
common_fb_state_t common_fb_instance;
size_t common_harness_state_size = sizeof(common_fb_instance);
{chr(10).join(storage)}
static int common_fb_initialized;

COMMON_DRIVER void common_harness_get_state(uint8_t *output) {{
    memcpy(output, &common_fb_instance, sizeof(common_fb_instance));
}}

COMMON_DRIVER void common_harness_set_state(const uint8_t *input) {{
    memcpy(&common_fb_instance, input, sizeof(common_fb_instance));
    common_fb_initialized = 1;
}}

COMMON_DRIVER void common_harness_reset(void) {{
    common_loop_ticks = 0;
    memset(&common_fb_instance, 0, sizeof(common_fb_instance));
{chr(10).join(reset_storage)}
    {name}__ctor(&common_fb_instance);
    common_fb_initialized = 1;
}}

COMMON_INPUT static int common_run_record(const uint8_t *input) {{
    if (!common_fb_initialized) common_harness_reset();
{chr(10).join(setup)}
    {name}(&common_fb_instance);
{output_escapes}
    return 0;
}}
'''


def generate_harness(abi: TargetAbi) -> tuple[str, dict]:
    size, alignment, fields = input_layout(abi)
    field_document = [{"name": field.param.name, "offset": field.offset,
                       "size": field.size, "align": field.align,
                       "block_kind": field.param.block_kind,
                       "spec": field.param.spec.name}
                      for field in fields]
    body = _function_harness(abi, size, fields) if abi.kind == "FUNCTION" else _fb_harness(abi, size, fields)
    reset = "" if abi.kind == "FUNCTION" else "extern void common_harness_reset(void);"
    function_reset = ("" if abi.kind == "FUNCTION_BLOCK" else
                      "COMMON_DRIVER void common_harness_reset(void) { common_loop_ticks = 0; }")
    driver = f'''\
size_t common_input_size = {size};
{reset}
{function_reset}

COMMON_DRIVER int common_harness_execute(const uint8_t *data, size_t length) {{
    common_loop_ticks = 0;
    common_input_t input = {{0}};
    if (data && length) memcpy(&input, data, length < sizeof(input) ? length : sizeof(input));
    int result = common_run_record((const uint8_t *)&input);
    return result;
}}

#ifndef COMMON_IN_PROCESS
extern unsigned char *__afl_fuzz_ptr __attribute__((weak));
extern unsigned int *__afl_fuzz_len __attribute__((weak));
extern unsigned char *__afl_area_ptr __attribute__((weak));
int __afl_persistent_loop(unsigned int max_cnt) __attribute__((weak));

COMMON_DRIVER int main(int argc, char **argv) {{
    if (argc < 2 && &__afl_fuzz_ptr && __afl_fuzz_ptr) {{
        unsigned int iterations = 10000;
        if (__afl_persistent_loop && getenv("__AFL_PERSISTENT")) {{
            while (__afl_persistent_loop(iterations)) {{
                __afl_area_ptr[0] = 0;
                common_harness_reset();
                common_harness_execute(__afl_fuzz_ptr, *__afl_fuzz_len);
            }}
            return 0;
        }}
        common_harness_reset();
        return common_harness_execute(__afl_fuzz_ptr, *__afl_fuzz_len);
    }}
    FILE *stream = stdin;
    if (argc >= 2) {{ stream = fopen(argv[1], "rb"); if (!stream) return 1; }}
    size_t length = 0;
    uint8_t *data = common_read_all(stream, &length);
    if (argc >= 2) fclose(stream);
    if (!data) return 1;
    common_harness_reset();
    int result = common_harness_execute(data, length);
    free(data);
    return result;
}}
#endif
'''
    header = "/* Auto-generated Common PLC Harness from ABI and LLVM layout. */\n"
    return header + COMMON_C + _input_struct(fields, size) + "\n" + body + driver, {"size": size, "input_size": size, "align": alignment, "fields": field_document,
                                               "target": abi.name, "kind": abi.kind}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function")
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--stg-model", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--layout-out", type=Path)
    args = parser.parse_args(argv)
    try:
        abi = load_target_abi(args.stg_model, args.ir, args.function)
    except AbiError as error:
        print(f"[common-harness] {args.function}: {error}", file=sys.stderr)
        return 1
    harness, layout = generate_harness(abi)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(harness, encoding="utf-8", newline="\n")
    if args.layout_out is not None:
        args.layout_out.parent.mkdir(parents=True, exist_ok=True)
        args.layout_out.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[common-harness] wrote {args.out} ({layout['kind']} {layout['target']}, {layout['size']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
