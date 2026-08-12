#!/usr/bin/env python3
"""C fuzz harness and seed generation."""

from __future__ import annotations

import os
import re
import sys

from fuzzer.runtime.paths import build_artifact_dir
from compiler.parser.st import DEFAULT_STRING_LENGTH, Param, TypeSpec, parse_function, parse_target

def target_name(func_name: str) -> str:
    return func_name.lower()


def c_identifier(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return f"semantist_param_{safe}"


def init_lines(spec: TypeSpec, name: str, offset: int, indent: str = "    ") -> list[str]:
    name = c_identifier(name)
    if spec.kind == "scalar":
        return [f"{indent}{spec.c_type} {name} = 0;"]

    if spec.kind == "string":
        return [
            f"{indent}{spec.c_type} {name}[{spec.length + 1}];",
            f"{indent}memset({name}, 0, sizeof({name}));",
        ]

    if spec.kind == "array":
        flat_type = spec.flat_c_type()
        flat_count = spec.flat_count()
        lines = [f"{indent}{flat_type} {name}[{flat_count}];"]
        lines.append(f"{indent}memset({name}, 0, sizeof({name}));")
        return lines

    if spec.kind == "pointer":
        lines = [f"{indent}uint8_t *{name}_storage = NULL;"]
        lines.append(f"{indent}size_t {name}_capacity = 0;")
        lines.append(f'{indent}fill_pointer_value(&{name}_storage, &{name}_capacity, "0x00");')
        lines.append(f"{indent}void *{name} = {name}_storage;")
        return lines

    return [f"{indent}uint8_t {name}[{spec.fuzz_size()}];"]


def call_arg(param: Param) -> str:
    return c_identifier(param.name)


def type_token(spec: TypeSpec) -> str:
    if spec.kind == "scalar":
        return spec.name
    if spec.kind == "string":
        return "WSTRING" if spec.c_type == "uint16_t" else "STRING"
    if spec.kind == "array":
        return "ARRAY"
    if spec.kind == "pointer":
        return "POINTER"
    return spec.name


def default_value(param: Param) -> str:
    if param.spec.kind == "string":
        return "A"
    if param.spec.kind in ("array", "pointer"):
        return "0x00"
    if param.spec.kind == "scalar" and param.spec.name in ("REAL", "LREAL"):
        return "0.0"
    return "0"


def semantic_cycle_hook_c() -> str:
    return """
__attribute__((weak)) void __semantist_semantic_cycle(uint32_t cycle) {
    (void)cycle;
}
"""


def assignment_lines(param: Param) -> list[str]:
    seed_name = param.name
    name = c_identifier(param.name)
    spec = param.spec
    value = "value"
    lines = [f'        if (strcmp(name, "{seed_name}") == 0) {{']
    if spec.kind == "scalar":
        if spec.name in ("REAL", "LREAL"):
            lines.append(f"            {name} = ({spec.c_type})parse_f64({value});")
        elif spec.c_type and spec.c_type.startswith("u"):
            lines.append(f"            {name} = ({spec.c_type})parse_u64({value});")
        else:
            lines.append(f"            {name} = ({spec.c_type})parse_i64({value});")
    elif spec.kind == "string":
        if spec.c_type == "uint16_t":
            lines.append(f"            fill_wstring_value({name}, {spec.length + 1}, {value});")
        else:
            lines.append(f"            fill_string_value({name}, {spec.length + 1}, {value});")
    elif spec.kind == "array":
        lines.append(f"            fill_hex_or_text({name}, sizeof({name}), {value});")
    elif spec.kind == "pointer":
        lines.append(f"            fill_pointer_value(&{name}_storage, &{name}_capacity, {value});")
        lines.append(f"            {name} = {name}_storage;")
    lines.append("        }")
    return lines


def fb_c_field_type(spec: TypeSpec) -> str:
    if spec.kind == "scalar":
        return spec.c_type
    if spec.kind == "string":
        return f"{spec.c_type}"
    if spec.kind == "array":
        return spec.flat_c_type()
    if spec.kind == "pointer":
        return "void *"
    return "uint8_t"


def fb_c_field_declaration(field_name: str, spec: TypeSpec, block_kind: str | None = None) -> str:
    if block_kind == "VAR_IN_OUT":
        return f"    void * {field_name};"
    c_type = fb_c_field_type(spec)
    if spec.kind == "string":
        return f"    {c_type} {field_name}[{spec.length + 1}];"
    if spec.kind == "array":
        return f"    {c_type} {field_name}[{max(1, spec.flat_count())}];"
    return f"    {c_type} {field_name};"


def fb_inout_storage_lines(param: Param, field_name: str) -> list[str]:
    spec = param.spec
    if param.block_kind != "VAR_IN_OUT":
        return []
    if spec.kind == "scalar":
        return [
            f"    {spec.c_type} {field_name}_storage = 0;",
            f"    inst.{field_name} = &{field_name}_storage;",
        ]
    if spec.kind == "string":
        return [
            f"    {spec.c_type} {field_name}_storage[{spec.length + 1}];",
            f"    memset({field_name}_storage, 0, sizeof({field_name}_storage));",
            f"    inst.{field_name} = {field_name}_storage;",
        ]
    if spec.kind == "array":
        return [
            f"    {spec.flat_c_type()} {field_name}_storage[{max(1, spec.flat_count())}];",
            f"    memset({field_name}_storage, 0, sizeof({field_name}_storage));",
            f"    inst.{field_name} = {field_name}_storage;",
        ]
    if spec.kind == "pointer":
        return [
            f"    uint8_t *{field_name}_storage = NULL;",
            f"    size_t {field_name}_capacity = 0;",
            f'    fill_pointer_value(&{field_name}_storage, &{field_name}_capacity, "0x00");',
            f"    inst.{field_name} = &{field_name}_storage;",
        ]
    return []


def fb_assignment_lines(param: Param, field_name: str) -> list[str]:
    spec = param.spec
    value = "value"
    target = f"{field_name}_storage" if param.block_kind == "VAR_IN_OUT" else f"inst.{field_name}"
    lines = [f'        if (strcmp(name, "{param.name}") == 0) {{']
    if spec.kind == "scalar":
        if spec.name in ("REAL", "LREAL"):
            lines.append(f"            {target} = ({spec.c_type})parse_f64({value});")
        elif spec.c_type and spec.c_type.startswith("u"):
            lines.append(f"            {target} = ({spec.c_type})parse_u64({value});")
        else:
            lines.append(f"            {target} = ({spec.c_type})parse_i64({value});")
    elif spec.kind == "string":
        if spec.c_type == "uint16_t":
            lines.append(f"            fill_wstring_value({target}, {spec.length + 1}, {value});")
        else:
            lines.append(f"            fill_string_value({target}, {spec.length + 1}, {value});")
    elif spec.kind == "array":
        lines.append(f"            fill_hex_or_text({target}, sizeof({target}), {value});")
    elif spec.kind == "pointer":
        lines.append(
            f"            fill_pointer_value(&{field_name}_storage, &{field_name}_capacity, {value});"
        )
        if param.block_kind == "VAR_IN_OUT":
            lines.append(f"            inst.{field_name} = &{field_name}_storage;")
        else:
            lines.append(f"            inst.{field_name} = {field_name}_storage;")
    lines.append("        }")
    return lines


def _is_persistent_fb_field(param: Param) -> bool:
    if param.constant:
        return False
    if param.block_kind in ("VAR_INPUT", "VAR_IN_OUT", "VAR_OUTPUT", "VAR_TEMP"):
        return False
    return bool(param.block_kind and param.block_kind.startswith("VAR"))


def fb_default_snapshot_fields(params, outputs, state_fields) -> list[Param]:
    snapshot: list[Param] = []
    for param in state_fields:
        if param in outputs or param.block_kind == "VAR_IN_OUT" or _is_persistent_fb_field(param):
            snapshot.append(param)
    return snapshot


def fb_snapshot_hash_lines(snapshot_fields, field_names) -> list[str]:
    lines = ["    uint64_t hash = 1469598103934665603ULL;"]
    for param in snapshot_fields:
        field_name = field_names[id(param)]
        spec = param.spec
        if param.block_kind == "VAR_IN_OUT":
            if spec.kind == "scalar":
                lines.append(f"    semantist_fb_hash_bytes(&hash, inst->{field_name}, sizeof({spec.c_type}));")
            elif spec.kind == "string":
                lines.append(
                    f"    semantist_fb_hash_bytes(&hash, inst->{field_name}, "
                    f"({spec.length + 1}) * sizeof({spec.c_type}));"
                )
            elif spec.kind == "array":
                lines.append(
                    f"    semantist_fb_hash_bytes(&hash, inst->{field_name}, "
                    f"{max(1, spec.flat_count())} * sizeof({spec.flat_c_type()}));"
                )
            elif spec.kind == "pointer":
                lines.append(f"    semantist_fb_hash_bytes(&hash, inst->{field_name}, sizeof(void *));")
            continue
        lines.append(f"    semantist_fb_hash_bytes(&hash, &inst->{field_name}, sizeof(inst->{field_name}));")
    lines.append("    return hash;")
    return lines


def generate_function_block_harness(func_name: str, params, outputs, state_fields, snapshot_fields=None) -> str:
    field_names = {id(param): f"f{idx}" for idx, param in enumerate(state_fields)}
    if snapshot_fields is None:
        snapshot_fields = fb_default_snapshot_fields(params, outputs, state_fields)
    state_decls = "\n".join(
        fb_c_field_declaration(field_names[id(param)], param.spec, param.block_kind)
        for param in state_fields
    )
    assignments = "\n".join(
        line
        for param in params
        for line in fb_assignment_lines(param, field_names[id(param)])
    )
    pointer_inits = "\n".join(
        f"    uint8_t *{field_names[id(param)]}_storage = NULL;\n"
        f"    size_t {field_names[id(param)]}_capacity = 0;\n"
        f'    fill_pointer_value(&{field_names[id(param)]}_storage, &{field_names[id(param)]}_capacity, "0x00");\n'
        f"    inst.{field_names[id(param)]} = {field_names[id(param)]}_storage;"
        for param in params
        if param.spec.kind == "pointer" and param.block_kind != "VAR_IN_OUT"
    )
    inout_inits = "\n".join(
        line
        for param in params
        for line in fb_inout_storage_lines(param, field_names[id(param)])
    )
    pointer_frees = "\n".join(
        f"    free({field_names[id(param)]}_storage);"
        for param in params
        if param.spec.kind == "pointer"
    )
    output_escapes = "\n".join(
        f"    escape_bytes(&inst->{field_names[id(param)]}, sizeof(inst->{field_names[id(param)]}));"
        for param in outputs
    ) or "    escape_bytes(inst, sizeof(*inst));"
    snapshot_hash = "\n".join(fb_snapshot_hash_lines(snapshot_fields, field_names))

    return f"""/* Auto-generated by compiler/scripts/generate_harness.py. */
#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {{
    void *__vtable;
{state_decls}
}} semantist_fb_state_t;

extern void {func_name}(semantist_fb_state_t *);
extern void {func_name}__ctor(semantist_fb_state_t *);

static const char *skip_spaces(const char *value) {{
    if (!value) {{
        return "0";
    }}
    while (*value == ' ' || *value == '\\t' || *value == '\\r') {{
        value++;
    }}
    return value;
}}

static int digit_value(char ch) {{
    if (ch >= '0' && ch <= '9') {{
        return ch - '0';
    }}
    if (ch >= 'a' && ch <= 'f') {{
        return ch - 'a' + 10;
    }}
    if (ch >= 'A' && ch <= 'F') {{
        return ch - 'A' + 10;
    }}
    return -1;
}}

static uint64_t parse_u64(const char *value) {{
    value = skip_spaces(value);
    int negative = 0;
    if (*value == '-' || *value == '+') {{
        negative = *value == '-';
        value++;
    }}
    uint64_t base = 10;
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        base = 16;
        value += 2;
    }}
    uint64_t out = 0;
    for (;;) {{
        int digit = digit_value(*value);
        if (digit < 0 || (uint64_t)digit >= base) {{
            break;
        }}
        out = out * base + (uint64_t)digit;
        value++;
    }}
    return negative ? 0 - out : out;
}}

static int64_t parse_i64(const char *value) {{
    return (int64_t)parse_u64(value);
}}

static double parse_f64(const char *value) {{
    value = skip_spaces(value);
    double sign = 1.0;
    if (*value == '-' || *value == '+') {{
        if (*value == '-') {{
            sign = -1.0;
        }}
        value++;
    }}
    double out = 0.0;
    while (*value >= '0' && *value <= '9') {{
        out = out * 10.0 + (double)(*value - '0');
        value++;
    }}
    if (*value == '.') {{
        double place = 0.1;
        value++;
        while (*value >= '0' && *value <= '9') {{
            out += (double)(*value - '0') * place;
            place *= 0.1;
            value++;
        }}
    }}
    if (*value == 'e' || *value == 'E') {{
        value++;
        int exp_sign = 1;
        if (*value == '-' || *value == '+') {{
            if (*value == '-') {{
                exp_sign = -1;
            }}
            value++;
        }}
        int exponent = 0;
        while (*value >= '0' && *value <= '9') {{
            if (exponent < 400) {{
                exponent = exponent * 10 + (*value - '0');
            }}
            value++;
        }}
        if (exponent > 400) {{
            exponent = 400;
        }}
        while (exponent-- > 0) {{
            if (exp_sign > 0) {{
                out *= 10.0;
            }} else {{
                out /= 10.0;
            }}
        }}
    }}
    return sign * out;
}}

static int hex_nibble(char ch) {{
    if (ch >= '0' && ch <= '9') {{
        return ch - '0';
    }}
    if (ch >= 'a' && ch <= 'f') {{
        return ch - 'a' + 10;
    }}
    if (ch >= 'A' && ch <= 'F') {{
        return ch - 'A' + 10;
    }}
    return -1;
}}

static size_t decoded_value_length(const char *value) {{
    if (!value) {{
        return 0;
    }}
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        value += 2;
        size_t out = 0;
        while (value[0] && value[1]) {{
            int hi = hex_nibble(value[0]);
            int lo = hex_nibble(value[1]);
            if (hi < 0 || lo < 0) {{
                break;
            }}
            out++;
            value += 2;
        }}
        return out;
    }}
    return strlen(value);
}}

static void fill_hex_or_text(void *dst, size_t dst_len, const char *value) {{
    uint8_t *out = (uint8_t *)dst;
    memset(out, 0, dst_len);
    if (!value) {{
        return;
    }}
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        value += 2;
        for (size_t i = 0; i < dst_len && value[0] && value[1]; i++, value += 2) {{
            int hi = hex_nibble(value[0]);
            int lo = hex_nibble(value[1]);
            if (hi < 0 || lo < 0) {{
                break;
            }}
            out[i] = (uint8_t)((hi << 4) | lo);
        }}
    }} else {{
        size_t len = strlen(value);
        memcpy(out, value, len < dst_len ? len : dst_len);
    }}
}}

static void fill_pointer_value(uint8_t **storage, size_t *capacity, const char *value) {{
    size_t next_capacity = decoded_value_length(value);
    if (next_capacity == 0) {{
        next_capacity = 1;
    }}
    uint8_t *next = (uint8_t *)realloc(*storage, next_capacity);
    if (!next) {{
        abort();
    }}
    *storage = next;
    *capacity = next_capacity;
    memset(*storage, 0, next_capacity);
    fill_hex_or_text(*storage, next_capacity, value);
}}

static void fill_string_value(uint8_t *dst, size_t dst_len, const char *value) {{
    memset(dst, 0, dst_len);
    if (dst_len == 0 || !value) {{
        return;
    }}
    size_t len = strlen(value);
    if (len >= dst_len) {{
        len = dst_len - 1;
    }}
    memcpy(dst, value, len);
    dst[len] = 0;
}}

static void fill_wstring_value(uint16_t *dst, size_t dst_len, const char *value) {{
    memset(dst, 0, dst_len * sizeof(uint16_t));
    if (dst_len == 0 || !value) {{
        return;
    }}
    size_t len = strlen(value);
    if (len >= dst_len) {{
        len = dst_len - 1;
    }}
    for (size_t i = 0; i < len; i++) {{
        dst[i] = (uint8_t)value[i];
    }}
    dst[len] = 0;
}}

static void escape_bytes(const void *data, size_t len) {{
    volatile const uint8_t *p = (const uint8_t *)data;
    volatile uint8_t sink = 0;
    for (size_t i = 0; i < len; i++) {{
        sink ^= p[i];
    }}
    (void)sink;
}}

typedef struct {{
    int trace_enabled;
    FILE *trace_file;
    int close_trace_file;
    uint64_t stale_threshold;
    uint64_t stale_count;
    uint64_t max_cycles;
    uint64_t cycle_count;
    int stop;
}} semantist_fb_runtime_t;

static int semantist_env_enabled(const char *name) {{
    const char *value = getenv(name);
    return value && strcmp(value, "1") == 0;
}}

static uint64_t semantist_parse_env_u64(const char *name, uint64_t default_value) {{
    const char *value = getenv(name);
    if (!value || !*value) {{
        return default_value;
    }}
    return parse_u64(value);
}}

static void semantist_fb_hash_bytes(uint64_t *hash, const void *data, size_t len) {{
    if (!data) {{
        return;
    }}
    const uint8_t *p = (const uint8_t *)data;
    for (size_t i = 0; i < len; i++) {{
        *hash ^= (uint64_t)p[i];
        *hash *= 1099511628211ULL;
    }}
}}

static uint64_t semantist_fb_snapshot_hash(const semantist_fb_state_t *inst) {{
{snapshot_hash}
}}

static void semantist_fb_runtime_init(semantist_fb_runtime_t *runtime) {{
    memset(runtime, 0, sizeof(*runtime));
    runtime->trace_enabled = semantist_env_enabled("SEMANTIST_FB_STATE_TRACE");
    runtime->stale_threshold = semantist_parse_env_u64("SEMANTIST_FB_STALE_THRESHOLD", 0);
    runtime->max_cycles = semantist_parse_env_u64("SEMANTIST_FB_MAX_CYCLES", 0);
    runtime->trace_file = stderr;
    if (runtime->trace_enabled) {{
        const char *trace_path = getenv("SEMANTIST_FB_STATE_TRACE_FILE");
        if (trace_path && *trace_path) {{
            FILE *trace = fopen(trace_path, "a");
            if (trace) {{
                runtime->trace_file = trace;
                runtime->close_trace_file = 1;
            }}
        }}
    }}
}}

static void semantist_fb_runtime_close(semantist_fb_runtime_t *runtime) {{
    if (runtime->close_trace_file && runtime->trace_file) {{
        fclose(runtime->trace_file);
    }}
    runtime->trace_file = NULL;
}}

static void semantist_fb_trace_cycle(
    semantist_fb_runtime_t *runtime,
    int cycle,
    uint64_t state_hash_before,
    uint64_t state_hash_after,
    int changed,
    int stale
) {{
    if (!runtime->trace_enabled || !runtime->trace_file) {{
        return;
    }}
    fprintf(
        runtime->trace_file,
        "cycle=%d state_hash_before=%016llx state_hash_after=%016llx changed=%d stale=%d\\n",
        cycle,
        (unsigned long long)state_hash_before,
        (unsigned long long)state_hash_after,
        changed,
        stale
    );
    fflush(runtime->trace_file);
}}

{semantic_cycle_hook_c()}

static uint8_t *read_all(FILE *fp, size_t *len_out) {{
    size_t cap = 4096;
    size_t len = 0;
    uint8_t *buf = (uint8_t *)malloc(cap + 1);
    if (!buf) {{
        return NULL;
    }}

    for (;;) {{
        if (len == cap) {{
            cap *= 2;
            uint8_t *next = (uint8_t *)realloc(buf, cap + 1);
            if (!next) {{
                free(buf);
                return NULL;
            }}
            buf = next;
        }}
        size_t n = fread(buf + len, 1, cap - len, fp);
        len += n;
        if (n == 0) {{
            break;
        }}
    }}

    *len_out = len;
    buf[len] = 0;
    return buf;
}}

static int split_cycle_name(char *raw_name, char **name_out) {{
    char *dot = strchr(raw_name, '.');
    if (!dot || dot == raw_name) {{
        *name_out = raw_name;
        return 0;
    }}
    for (char *p = raw_name; p < dot; p++) {{
        if (!isdigit((unsigned char)*p)) {{
            *name_out = raw_name;
            return 0;
        }}
    }}
    *dot = 0;
    *name_out = dot + 1;
    return atoi(raw_name);
}}

static int run_cycle(semantist_fb_state_t *inst, semantist_fb_runtime_t *runtime, int cycle) {{
    if (runtime->stop) {{
        return 0;
    }}
    if (runtime->max_cycles > 0 && runtime->cycle_count >= runtime->max_cycles) {{
        runtime->stop = 1;
        return 0;
    }}
    int observe_state = runtime->trace_enabled || runtime->stale_threshold > 0 || getenv("SEMANTIST_SEMANTIC_TRACE_FILE");
    uint64_t state_hash_before = 0;
    uint64_t state_hash_after = 0;
    if (observe_state) {{
        state_hash_before = semantist_fb_snapshot_hash(inst);
    }}
    __semantist_semantic_cycle((uint32_t)cycle);
    {func_name}(inst);
{output_escapes}
    if (observe_state) {{
        state_hash_after = semantist_fb_snapshot_hash(inst);
        int changed = state_hash_before != state_hash_after;
        int stale = !changed;
        if (stale) {{
            runtime->stale_count++;
        }} else {{
            runtime->stale_count = 0;
        }}
        semantist_fb_trace_cycle(runtime, cycle, state_hash_before, state_hash_after, changed, stale);
        if (runtime->stale_threshold > 0 && runtime->stale_count >= runtime->stale_threshold) {{
            runtime->stop = 1;
        }}
    }}
    runtime->cycle_count++;
    return !runtime->stop;
}}

int main(int argc, char **argv) {{
    FILE *fp = stdin;
    if (argc >= 2) {{
        fp = fopen(argv[1], "rb");
        if (!fp) {{
            return 1;
        }}
    }}

    size_t len = 0;
    uint8_t *data = read_all(fp, &len);
    if (argc >= 2) {{
        fclose(fp);
    }}
    if (!data) {{
        return 1;
    }}

    semantist_fb_state_t inst;
    memset(&inst, 0, sizeof(inst));
    {func_name}__ctor(&inst);
{inout_inits}
{pointer_inits}
    semantist_fb_runtime_t runtime;
    semantist_fb_runtime_init(&runtime);

    int current_cycle = -1;
    int saw_record = 0;
    char *save = NULL;
    for (char *line = strtok_r((char *)data, "\\n", &save); line; line = strtok_r(NULL, "\\n", &save)) {{
        if (runtime.stop) {{
            break;
        }}
        char *first = strchr(line, ',');
        if (!first) {{
            continue;
        }}
        *first = 0;
        char *second = strchr(first + 1, ',');
        if (!second) {{
            continue;
        }}
        *second = 0;
        char *raw_name = line;
        char *type = first + 1;
        char *value = second + 1;
        char *name = raw_name;
        int cycle = split_cycle_name(raw_name, &name);
        (void)type;
        if (!name || !value) {{
            continue;
        }}
        if (current_cycle < 0) {{
            current_cycle = cycle;
        }} else if (cycle != current_cycle) {{
            if (!run_cycle(&inst, &runtime, current_cycle)) {{
                break;
            }}
            current_cycle = cycle;
        }}
        saw_record = 1;
{assignments}
    }}
    if (!runtime.stop) {{
        if (saw_record) {{
            run_cycle(&inst, &runtime, current_cycle);
        }} else {{
            run_cycle(&inst, &runtime, 0);
        }}
    }}
    semantist_fb_runtime_close(&runtime);
{pointer_frees}
    free(data);
    return 0;
}}
"""


def generate_harness(func_name: str, ret_spec: TypeSpec, params, total_size: int) -> str:
    returns_by_ref = ret_spec.kind in ("string", "array", "pointer")
    proto_args = [p.spec.c_arg_type() for p in params]
    if returns_by_ref:
        proto_args.insert(0, "void *")
        proto = f"extern void {func_name}({', '.join(proto_args) or 'void'});"
    else:
        proto = f"extern {ret_spec.c_type} {func_name}({', '.join(proto_args) or 'void'});"

    declarations = "\n".join(
        line
        for param in params
        for line in init_lines(param.spec, param.name, param.offset)
    )
    assignments = "\n".join(
        line
        for param in params
        for line in assignment_lines(param)
    )

    # Keep the harness generic: it only parses structured seeds, calls the
    # target, and lets sanitizer/crash/timeout feedback report issues.
    oracle = ""

    args = ", ".join(call_arg(p) for p in params)
    if returns_by_ref:
        ret_type = ret_spec.flat_c_type()
        ret_count = max(1, ret_spec.flat_count())
        call = f"""    {ret_type} result[{ret_count}];
    memset(result, 0, sizeof(result));
    {func_name}(result{', ' if args else ''}{args});
    escape_bytes(result, sizeof(result));"""
    else:
        call = f"""    volatile {ret_spec.c_type} result = {func_name}({args});
    (void)result;"""
    pointer_frees = "\n".join(
        f"    free({c_identifier(p.name)}_storage);"
        for p in params
        if p.spec.kind == "pointer"
    )

    return f"""/* Auto-generated by compiler/scripts/generate_harness.py. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

{proto}

static const char *skip_spaces(const char *value) {{
    if (!value) {{
        return "0";
    }}
    while (*value == ' ' || *value == '\\t' || *value == '\\r') {{
        value++;
    }}
    return value;
}}

static int digit_value(char ch) {{
    if (ch >= '0' && ch <= '9') {{
        return ch - '0';
    }}
    if (ch >= 'a' && ch <= 'f') {{
        return ch - 'a' + 10;
    }}
    if (ch >= 'A' && ch <= 'F') {{
        return ch - 'A' + 10;
    }}
    return -1;
}}

static uint64_t parse_u64(const char *value) {{
    value = skip_spaces(value);
    int negative = 0;
    if (*value == '-' || *value == '+') {{
        negative = *value == '-';
        value++;
    }}
    uint64_t base = 10;
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        base = 16;
        value += 2;
    }}
    uint64_t out = 0;
    for (;;) {{
        int digit = digit_value(*value);
        if (digit < 0 || (uint64_t)digit >= base) {{
            break;
        }}
        out = out * base + (uint64_t)digit;
        value++;
    }}
    return negative ? 0 - out : out;
}}

static int64_t parse_i64(const char *value) {{
    return (int64_t)parse_u64(value);
}}

static double parse_f64(const char *value) {{
    value = skip_spaces(value);
    double sign = 1.0;
    if (*value == '-' || *value == '+') {{
        if (*value == '-') {{
            sign = -1.0;
        }}
        value++;
    }}
    double out = 0.0;
    while (*value >= '0' && *value <= '9') {{
        out = out * 10.0 + (double)(*value - '0');
        value++;
    }}
    if (*value == '.') {{
        double place = 0.1;
        value++;
        while (*value >= '0' && *value <= '9') {{
            out += (double)(*value - '0') * place;
            place *= 0.1;
            value++;
        }}
    }}
    if (*value == 'e' || *value == 'E') {{
        value++;
        int exp_sign = 1;
        if (*value == '-' || *value == '+') {{
            if (*value == '-') {{
                exp_sign = -1;
            }}
            value++;
        }}
        int exponent = 0;
        while (*value >= '0' && *value <= '9') {{
            if (exponent < 400) {{
                exponent = exponent * 10 + (*value - '0');
            }}
            value++;
        }}
        if (exponent > 400) {{
            exponent = 400;
        }}
        while (exponent-- > 0) {{
            if (exp_sign > 0) {{
                out *= 10.0;
            }} else {{
                out /= 10.0;
            }}
        }}
    }}
    return sign * out;
}}

static int hex_nibble(char ch) {{
    if (ch >= '0' && ch <= '9') {{
        return ch - '0';
    }}
    if (ch >= 'a' && ch <= 'f') {{
        return ch - 'a' + 10;
    }}
    if (ch >= 'A' && ch <= 'F') {{
        return ch - 'A' + 10;
    }}
    return -1;
}}

static size_t decoded_value_length(const char *value) {{
    if (!value) {{
        return 0;
    }}
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        value += 2;
        size_t out = 0;
        while (value[0] && value[1]) {{
            int hi = hex_nibble(value[0]);
            int lo = hex_nibble(value[1]);
            if (hi < 0 || lo < 0) {{
                break;
            }}
            out++;
            value += 2;
        }}
        return out;
    }}
    return strlen(value);
}}

static void fill_hex_or_text(void *dst, size_t dst_len, const char *value) {{
    uint8_t *out = (uint8_t *)dst;
    memset(out, 0, dst_len);
    if (!value) {{
        return;
    }}
    if (value[0] == '0' && (value[1] == 'x' || value[1] == 'X')) {{
        value += 2;
        for (size_t i = 0; i < dst_len && value[0] && value[1]; i++, value += 2) {{
            int hi = hex_nibble(value[0]);
            int lo = hex_nibble(value[1]);
            if (hi < 0 || lo < 0) {{
                break;
            }}
            out[i] = (uint8_t)((hi << 4) | lo);
        }}
    }} else {{
        size_t len = strlen(value);
        memcpy(out, value, len < dst_len ? len : dst_len);
    }}
}}

static void fill_pointer_value(uint8_t **storage, size_t *capacity, const char *value) {{
    size_t next_capacity = decoded_value_length(value);
    if (next_capacity == 0) {{
        next_capacity = 1;
    }}
    uint8_t *next = (uint8_t *)realloc(*storage, next_capacity);
    if (!next) {{
        abort();
    }}
    *storage = next;
    *capacity = next_capacity;
    memset(*storage, 0, next_capacity);
    fill_hex_or_text(*storage, next_capacity, value);
}}

static void fill_string_value(uint8_t *dst, size_t dst_len, const char *value) {{
    memset(dst, 0, dst_len);
    if (dst_len == 0 || !value) {{
        return;
    }}
    size_t len = strlen(value);
    if (len >= dst_len) {{
        len = dst_len - 1;
    }}
    memcpy(dst, value, len);
    dst[len] = 0;
}}

static void fill_wstring_value(uint16_t *dst, size_t dst_len, const char *value) {{
    memset(dst, 0, dst_len * sizeof(uint16_t));
    if (dst_len == 0 || !value) {{
        return;
    }}
    size_t len = strlen(value);
    if (len >= dst_len) {{
        len = dst_len - 1;
    }}
    for (size_t i = 0; i < len; i++) {{
        dst[i] = (uint8_t)value[i];
    }}
    dst[len] = 0;
}}

static void escape_bytes(const void *data, size_t len) {{
    volatile const uint8_t *p = (const uint8_t *)data;
    volatile uint8_t sink = 0;
    for (size_t i = 0; i < len; i++) {{
        sink ^= p[i];
    }}
    (void)sink;
}}

{semantic_cycle_hook_c()}

static uint8_t *read_all(FILE *fp, size_t *len_out) {{
    size_t cap = 4096;
    size_t len = 0;
    uint8_t *buf = (uint8_t *)malloc(cap + 1);
    if (!buf) {{
        return NULL;
    }}

    for (;;) {{
        if (len == cap) {{
            cap *= 2;
            uint8_t *next = (uint8_t *)realloc(buf, cap + 1);
            if (!next) {{
                free(buf);
                return NULL;
            }}
            buf = next;
        }}
        size_t n = fread(buf + len, 1, cap - len, fp);
        len += n;
        if (n == 0) {{
            break;
        }}
    }}

    *len_out = len;
    buf[len] = 0;
    return buf;
}}


int main(int argc, char **argv) {{
    FILE *fp = stdin;
    if (argc >= 2) {{
        fp = fopen(argv[1], "rb");
        if (!fp) {{
            return 1;
        }}
    }}

    size_t len = 0;
    uint8_t *data = read_all(fp, &len);
    if (argc >= 2) {{
        fclose(fp);
    }}
    if (!data) {{
        return 1;
    }}
{declarations}
    char *save = NULL;
    for (char *line = strtok_r((char *)data, "\\n", &save); line; line = strtok_r(NULL, "\\n", &save)) {{
        char *first = strchr(line, ',');
        if (!first) {{
            continue;
        }}
        *first = 0;
        char *second = strchr(first + 1, ',');
        if (!second) {{
            continue;
        }}
        *second = 0;
        char *name = line;
        char *type = first + 1;
        char *value = second + 1;
        (void)type;
        if (!name || !value) {{
            continue;
        }}
{assignments}
    }}
{oracle}
    __semantist_semantic_cycle(0);
{call}
{pointer_frees}
    free(data);
    return 0;
}}
"""


def write_seed(func_name, params, total_size):
    seed_dir = os.environ.get(
        "GENERATED_SEED_DIR",
        str(build_artifact_dir() / "corpus" / target_name(func_name) / "seeds"),
    )
    os.makedirs(seed_dir, exist_ok=True)
    lines = []
    values = {}

    names = {p.name.upper(): p for p in params}
    if all(k in names for k in ("X", "D", "L", "U")):
        values.update({"X": "0", "D": "0", "L": "1", "U": "1"})

    for p in params:
        token = type_token(p.spec)
        value = values.get(p.name.upper(), default_value(p))
        lines.append(f"{p.name},{token},{value}")

    with open(os.path.join(seed_dir, "seed1"), "wb") as f:
        f.write(("\n".join(lines) + "\n").encode("utf-8"))


def write_function_block_seed(func_name, params):
    seed_dir = os.environ.get(
        "GENERATED_SEED_DIR",
        str(build_artifact_dir() / "corpus" / target_name(func_name) / "seeds"),
    )
    os.makedirs(seed_dir, exist_ok=True)
    lines = []
    for param in params:
        if param.block_kind == "VAR_INPUT" and param.constant:
            lines.append(f"{param.name},{type_token(param.spec)},{default_value(param)}")
        elif param.block_kind == "VAR_IN_OUT":
            lines.append(f"{param.name},{type_token(param.spec)},{default_value(param)}")
    for cycle in range(2):
        for param in params:
            if not (param.block_kind == "VAR_INPUT" and not param.constant):
                continue
            value = default_value(param)
            if param.spec.kind == "scalar" and param.spec.name in ("REAL", "LREAL"):
                value = "1.0" if cycle else "0.0"
            elif param.spec.kind == "scalar" and param.spec.name == "BOOL":
                value = "1" if cycle else "0"
            lines.append(f"{cycle}.{param.name},{type_token(param.spec)},{value}")

    with open(os.path.join(seed_dir, "seed1"), "wb") as f:
        f.write(("\n".join(lines) + "\n").encode("utf-8"))


def main():
    target = parse_target(sys.argv[1], sys.argv[2])
    harness_out = os.environ.get("HARNESS_OUT", str(build_artifact_dir() / "harness" / "harness.c"))
    os.makedirs(os.path.dirname(harness_out) or ".", exist_ok=True)
    with open(harness_out, "w", encoding="utf-8") as f:
        if target.kind == "FUNCTION_BLOCK":
            f.write(
                generate_function_block_harness(
                    target.name,
                    target.params,
                    target.outputs or [],
                    target.state_fields or [],
                    target.snapshot_fields or [],
                )
            )
            write_function_block_seed(target.name, target.params)
        else:
            f.write(generate_harness(target.name, target.ret_spec, target.params, target.total_size))
            write_seed(target.name, target.params, target.total_size)

    print(
        f"generated {harness_out} for {target.kind} {target.name} "
        f"with {len(target.params)} fuzzed inputs ({target.total_size} bytes)"
    )


if __name__ == "__main__":
    main()
