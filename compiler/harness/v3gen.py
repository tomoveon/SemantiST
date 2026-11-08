"""SemantiST v3 frame-level sequence adapter generation.

The C harness exposes frame initialization, step, and state save/restore APIs.
Persistent VAR_IN_OUT and aggregate values live in independent external regions;
the instance stores pointers and checkpoints relocate those pointers on restore.
The legacy ``semantist_run_input`` entry point is retained because existing
callers and tests refer to it; ``semantist_v3_execute`` is the implementation
used by ``semantist_harness_execute``.
"""

from __future__ import annotations

from compiler.harness.abi import Param, TargetAbi, frame_params, shared_params
from compiler.harness.contract import POINTER_STORAGE_CAPACITY, harness_fields, region_layout

_FIELD_BASE = 1
FRAME_NAME = "semantist_frame_t"
STATE_NAME = "semantist_harness_state_t"
STATE_VAR = "semantist_harness_state"


def _c_type(spec) -> str:
    return spec.c_type or "uint8_t"


def _field_name(param: Param) -> str:
    return f"f{param.index - _FIELD_BASE}"


def _storage_name(param: Param) -> str:
    return f"f{param.index - _FIELD_BASE}_storage"


def _external_storage_name(param: Param) -> str:
    return f"semantist_{_storage_name(param)}"


def _uses_external_storage(param: Param) -> bool:
    return param.block_kind == "VAR_IN_OUT" or param.spec.kind != "scalar"


def _external_params(abi: TargetAbi) -> list[Param]:
    return [param for param in _backing_params(abi) if _uses_external_storage(param)]


def _indent(lines: list[str], prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in lines)


def _scalar_assignment(spec, access: str) -> list[str]:
    """Decode one v3 scalar without narrowing or wrapping its wire value."""
    bits = spec.size * 8
    raw = [
        f"    if (semantist_raw_scalar(value, &{access}, sizeof({access}))) continue;",
    ]
    if spec.name in ("REAL", "LREAL"):
        return raw + [
            "    double parsed_value;",
            f"    if (!semantist_parse_float_checked(value, {bits}, &parsed_value)) {{ free(buffer); return 1; }}",
            f"    {access} = ({spec.c_type})parsed_value;",
        ]
    if spec.c_type.startswith("u"):
        max_value = 1 if spec.name == "BOOL" else (1 << bits) - 1
        return raw + [
            "    uint64_t parsed_value;",
            f"    if (!semantist_parse_unsigned_checked(value, {max_value}ULL, &parsed_value)) {{ free(buffer); return 1; }}",
            f"    {access} = ({spec.c_type})parsed_value;",
        ]
    min_value = -(1 << (bits - 1))
    max_value = (1 << (bits - 1)) - 1
    min_c = "INT64_MIN" if bits == 64 else str(min_value)
    return raw + [
        "    int64_t parsed_value;",
        f"    if (!semantist_parse_signed_checked(value, {min_c}, {max_value}LL, &parsed_value)) {{ free(buffer); return 1; }}",
        f"    {access} = ({spec.c_type})parsed_value;",
    ]


def frame_declarations(abi: TargetAbi) -> list[str]:
    lines: list[str] = []
    for param in frame_params(abi):
        name = _field_name(param)
        spec = param.spec
        if spec.kind == "scalar":
            lines.append(f"{spec.c_type} {name};")
        elif spec.kind == "pointer":
            lines.append(f"uint8_t {name}[{POINTER_STORAGE_CAPACITY}];")
        else:
            g = _generator()
            lines.append(f"{g.buffer_declaration(spec, name)};")
    return lines


def backing_declarations(abi: TargetAbi) -> list[str]:
    lines: list[str] = []
    for param in _backing_params(abi):
        name = _storage_name(param)
        spec = param.spec
        if spec.kind == "pointer":
            lines.append(f"uint8_t {name}[{max(POINTER_STORAGE_CAPACITY, spec.pointee_bytes())}];")
        elif spec.kind == "scalar":
            lines.append(f"{spec.c_type} {name};")
        else:
            g = _generator()
            lines.append(f"{g.buffer_declaration(spec, name)};")
    return lines


def external_backing_declarations(abi: TargetAbi) -> list[str]:
    """Declare independent objects used by the target for VAR_IN_OUT data.

    The state struct keeps a mirror for snapshots, while the target receives
    these separate objects. ASan can therefore observe an over-read/write at
    the same object boundary as the common native harness.
    """
    lines: list[str] = []
    for param in _external_params(abi):
        name = _external_storage_name(param)
        spec = param.spec
        if spec.kind == "pointer":
            lines.append(f"static uint8_t {name}[{max(POINTER_STORAGE_CAPACITY, spec.pointee_bytes())}];")
        elif spec.kind == "scalar":
            lines.append(f"static {spec.c_type} {name};")
        else:
            g = _generator()
            lines.append(f"static {g.buffer_declaration(spec, name)};")
    return lines


def _backing_params(abi: TargetAbi) -> list[Param]:
    candidates = list(abi.params)
    if abi.kind == "FUNCTION":
        candidates += [slot.param for slot in abi.call_slots if slot.param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT")]
        candidates += [param for param in abi.params if param.block_kind == "VAR_INPUT" and param.constant]
    else:
        candidates += [param for param in (abi.state_fields or []) if param not in abi.params]
    seen: set[int] = set()
    result: list[Param] = []
    for param in candidates:
        if param.index is None or param.index in seen:
            continue
        if (
            param.block_kind != "VAR_IN_OUT"
            and param.spec.kind != "pointer"
            and not (abi.kind == "FUNCTION" and param.block_kind == "VAR_OUTPUT")
            and not (abi.kind == "FUNCTION" and param.block_kind == "VAR_INPUT" and param.constant)
        ):
            continue
        seen.add(param.index)
        result.append(param)
    return result


def _result_declaration(abi: TargetAbi) -> list[str]:
    if abi.kind != "FUNCTION" or abi.ret is None:
        return []
    spec = abi.ret.spec
    if spec.kind == "scalar":
        return [f"{spec.c_type} result;"]
    if spec.kind == "pointer":
        return [f"uint8_t result[{POINTER_STORAGE_CAPACITY}];"]
    g = _generator()
    return [f"{g.buffer_declaration(spec, 'result')};"]


def state_declarations(abi: TargetAbi) -> list[str]:
    lines: list[str] = []
    if abi.kind == "FUNCTION_BLOCK":
        lines.append("semantist_fb_state_t inst;")
    lines += backing_declarations(abi)
    lines += _result_declaration(abi)
    lines += [
        "uint64_t vclock_ns;",
        "uint64_t frame_index;",
        "uint64_t loop_ticks;",
        "uint8_t initialized;",
        "uint8_t budget_exceeded;",
    ]
    return lines


def _frame_case_lines(abi: TargetAbi, target: str) -> list[str]:
    g = _generator()
    lines: list[str] = []
    for param in frame_params(abi):
        name = _field_name(param)
        spec = param.spec
        lines.append(f'if (strcmp(name, "{param.name}") == 0) {{')
        if spec.kind == "scalar":
            lines.extend(_scalar_assignment(spec, f"{target}->{name}"))
        elif spec.kind == "string":
            helper = "semantist_fill_wstring_checked" if spec.name == "WSTRING" else "semantist_fill_bytes_checked"
            args = f"{target}->{name}, sizeof({target}->{name}), value"
            if spec.name != "WSTRING":
                args += ", 1, 1"
            lines.append(f"    if (!{helper}({args})) {{ free(buffer); return 1; }}")
        else:
            lines.append(f"    if (!semantist_fill_bytes_checked({target}->{name}, sizeof({target}->{name}), value, 1, 0)) {{ free(buffer); return 1; }}")
        lines.append("    continue;")
        lines.append("}")
    return lines


def _shared_case_lines(abi: TargetAbi, target: str) -> list[str]:
    lines: list[str] = []
    for param in shared_params(abi):
        spec = param.spec
        if param.block_kind == "VAR_IN_OUT" and _uses_external_storage(param):
            access = _external_storage_name(param)
        elif spec.kind == "pointer" or (abi.kind == "FUNCTION" and _uses_external_storage(param)):
            access = _external_storage_name(param)
        elif abi.kind == "FUNCTION":
            access = f"{target}.{_storage_name(param)}"
        elif param.block_kind == "VAR_IN_OUT":
            access = f"{target}.{_storage_name(param)}"
        else:
            access = f"{target}.inst.{_field_name(param)}"
        lines.append(f'if (strcmp(name, "{param.name}") == 0) {{')
        if spec.kind == "scalar":
            lines.extend(_scalar_assignment(spec, access))
        elif spec.kind == "string":
            helper = "semantist_fill_wstring_checked" if spec.name == "WSTRING" else "semantist_fill_bytes_checked"
            args = f"{access}, sizeof({access}), value" + ("" if spec.name == "WSTRING" else ", 1, 1")
            lines.append(f"    if (!{helper}({args})) {{ free(buffer); return 1; }}")
        else:
            lines.append(f"    if (!semantist_fill_bytes_checked({access}, sizeof({access}), value, 1, 0)) {{ free(buffer); return 1; }}")
        lines.append("    continue;")
        lines.append("}")
    return lines


def _inout_frame_cases(abi: TargetAbi) -> list[str]:
    """Cycle-prefixed IN_OUT records update the shared backing storage.

    ICSQuartz-style targets drive VAR_IN_OUT values every scan; the canonical
    generator still writes their initial value as a shared record, while a
    handwritten or solver-produced frame may override it.
    """
    lines: list[str] = []
    for param in shared_params(abi):
        if param.block_kind != "VAR_IN_OUT":
            continue
        spec = param.spec
        access = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
        lines.append(f'if (strcmp(name, "{param.name}") == 0) {{')
        if spec.kind == "scalar":
            lines.extend(_scalar_assignment(spec, access))
        elif spec.kind == "string":
            helper = "semantist_fill_wstring_checked" if spec.name == "WSTRING" else "semantist_fill_bytes_checked"
            args = f"{access}, sizeof({access}), value" + ("" if spec.name == "WSTRING" else ", 1, 1")
            lines.append(f"    if (!{helper}({args})) {{ free(buffer); return 1; }}")
        else:
            lines.append(f"    if (!semantist_fill_bytes_checked({access}, sizeof({access}), value, 1, 0)) {{ free(buffer); return 1; }}")
        lines.append("    continue;")
        lines.append("}")
    return lines


def _apply_frame_lines(abi: TargetAbi) -> list[str]:
    lines: list[str] = []
    state = f"&{STATE_VAR}"
    for param in frame_params(abi):
        name = _field_name(param)
        spec = param.spec
        if spec.kind == "scalar":
            lines.append(f"{STATE_VAR}.inst.{name} = frame->{name};")
        elif spec.kind == "pointer":
            storage = _external_storage_name(param)
            lines.append(f"memset({storage}, 0, sizeof({storage}));")
            lines.append(
                f"memcpy({storage}, frame->{name}, sizeof({storage}) < sizeof(frame->{name}) ? sizeof({storage}) : sizeof(frame->{name}));"
            )
            lines.append(f"{STATE_VAR}.inst.{name} = {storage};")
        else:
            lines.append(f"memcpy(&{STATE_VAR}.inst.{name}, frame->{name}, sizeof({STATE_VAR}.inst.{name}));")
    return lines


def _pointer_init_lines(abi: TargetAbi) -> list[str]:
    lines: list[str] = []
    seen: set[int] = set()
    candidates = list(abi.params)
    if abi.kind == "FUNCTION":
        candidates += [slot.param for slot in abi.call_slots if slot.param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT")]
    else:
        candidates += [param for param in (abi.state_fields or []) if param not in abi.params]
    for param in candidates:
        if param.index is None or param.index in seen:
            continue
        if param.block_kind != "VAR_IN_OUT" and param.spec.kind != "pointer" and not (
            abi.kind == "FUNCTION" and param.block_kind == "VAR_OUTPUT"
        ):
            continue
        seen.add(param.index)
        name = _field_name(param)
        storage = _storage_name(param)
        if abi.kind == "FUNCTION_BLOCK":
            if param.block_kind == "VAR_IN_OUT" and param.spec.kind != "pointer":
                storage = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
                lines.append(f"{STATE_VAR}.inst.{name} = &{storage};")
            else:
                storage = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
                lines.append(f"{STATE_VAR}.inst.{name} = {storage};")
        else:
            lines.append(f"/* function argument {param.name} uses {storage} */")
    return lines


def _state_dump_lines(abi: TargetAbi) -> list[str]:
    lines: list[str] = ['fprintf(stream, "{\\"frame\\":%llu,\\"fields\\":{", (unsigned long long)frame_index);']
    first = True
    for param in abi.state_fields:
        name = _field_name(param)
        spec = param.spec
        if param.block_kind == "VAR_IN_OUT" or spec.kind == "pointer":
            access = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
        else:
            access = f"{STATE_VAR}.inst.{name}"
        if first:
            lines.append('fprintf(stream, "\\"%s\\":");' % param.name)
        else:
            lines.append('fprintf(stream, ",\\"%s\\":");' % param.name)
        first = False
        if spec.kind == "scalar":
            if spec.name in ("REAL", "LREAL"):
                lines.append(f"semantist_dump_float(stream, (double){access});")
            elif spec.c_type and spec.c_type.startswith("u"):
                lines.append(f'fprintf(stream, "%llu", (unsigned long long){access});')
            else:
                lines.append(f'fprintf(stream, "%lld", (long long){access});')
        elif spec.kind == "opaque":
            lines.append(f"semantist_dump_opaque(stream, &{access}, sizeof({access}));")
        else:
            lines.append(f"semantist_dump_hex(stream, &{access}, sizeof({access}));")
    lines.append('fprintf(stream, "}}\\n");')
    lines.append("fflush(stream);")
    return lines


def _storage_sync_code(abi: TargetAbi) -> tuple[str, str, str]:
    to_state: list[str] = []
    from_state: list[str] = []
    rebind = _pointer_init_lines(abi)
    for param in _external_params(abi):
        external = _external_storage_name(param)
        mirror = f"{STATE_VAR}.{_storage_name(param)}"
        external_address = f"&{external}" if param.spec.kind == "scalar" else external
        to_state.append(f"    memcpy(&{mirror}, {external_address}, sizeof({mirror}));")
        from_state.append(f"    memcpy({external_address}, &{mirror}, sizeof({mirror}));")
    return "\n".join(to_state), "\n".join(from_state), "\n".join(rebind)


def _storage_reset_code(abi: TargetAbi) -> str:
    lines = [
        f"    memset({('&' if param.spec.kind == 'scalar' else '')}{_external_storage_name(param)}, 0, sizeof({_external_storage_name(param)}));"
        for param in _external_params(abi)
    ]
    return "\n".join(lines) or "    (void)0;"


def _accessor_declarations(abi: TargetAbi) -> str:
    sync_to_state, sync_from_state, rebind = _storage_sync_code(abi)
    external_params = _external_params(abi)
    external_count = len(external_params)
    external_bases = "\n".join(
        f"    if (index == {index}) return (void *){('&' if param.spec.kind == 'scalar' else '')}{_external_storage_name(param)};"
        for index, param in enumerate(external_params)
    ) or "    (void)index;"
    external_sizes = "\n".join(
        f"    if (index == {index}) return sizeof({_external_storage_name(param)});"
        for index, param in enumerate(external_params)
    ) or "    (void)index;"
    helpers = f"""
SEMANTIST_DRIVER static void semantist_sync_storage_to_state(void) {{
{sync_to_state or "    (void)0;"}
}}

SEMANTIST_DRIVER static void semantist_sync_storage_from_state(void) {{
{sync_from_state or "    (void)0;"}
}}

SEMANTIST_DRIVER static void semantist_rebind_storage(void) {{
{rebind or "    (void)0;"}
}}

SEMANTIST_DRIVER static size_t semantist_target_region_count(void) {{
    return &__semantist_state_region_count != NULL ? (size_t)__semantist_state_region_count : 0;
}}

SEMANTIST_DRIVER static size_t semantist_external_region_count(void) {{
    return {external_count};
}}

SEMANTIST_DRIVER static void *semantist_external_region_base(size_t index) {{
{external_bases}
    return NULL;
}}

SEMANTIST_DRIVER static uint64_t semantist_external_region_size(size_t index) {{
{external_sizes}
    return 0;
}}
"""
    return helpers + r"""
struct semantist_reloc {
    uint32_t region;
    uint64_t offset;
    uint32_t target_region;
    uint64_t target_offset;
};

SEMANTIST_DRIVER size_t semantist_state_region_count(void) {
    return semantist_target_region_count() + semantist_external_region_count();
}

SEMANTIST_DRIVER void *semantist_state_region_base(size_t index) {
    size_t target_count = semantist_target_region_count();
    if (index < target_count) {
        return __semantist_state_regions[index].address;
    }
    return semantist_external_region_base(index - target_count);
}

SEMANTIST_DRIVER uint64_t semantist_state_region_size(size_t index) {
    size_t target_count = semantist_target_region_count();
    if (index < target_count) {
        return __semantist_state_regions[index].size;
    }
    return semantist_external_region_size(index - target_count);
}

SEMANTIST_DRIVER void *semantist_harness_state_base(void) {
    return (void *)&semantist_harness_state;
}

SEMANTIST_DRIVER size_t semantist_state_size(void) {
    size_t total = sizeof(semantist_harness_state_t);
    for (size_t i = 0; i < semantist_state_region_count(); i++) {
        total += (size_t)semantist_state_region_size(i);
    }
    return total;
}

SEMANTIST_DRIVER static size_t semantist_region_count(void) {
    return semantist_state_region_count();
}

SEMANTIST_DRIVER void semantist_checkpoint(uint8_t *out) {
    semantist_sync_storage_to_state();
    uint8_t *cursor = out;
    for (size_t i = 0; i < semantist_state_region_count(); i++) {
        size_t size = (size_t)semantist_state_region_size(i);
        memcpy(cursor, semantist_state_region_base(i), size);
        cursor += size;
    }
    memcpy(cursor, &semantist_harness_state, sizeof(semantist_harness_state));
}

SEMANTIST_DRIVER void semantist_restore(const uint8_t *in) {
    const uint8_t *cursor = in;
    for (size_t i = 0; i < semantist_state_region_count(); i++) {
        size_t size = (size_t)semantist_state_region_size(i);
        memcpy(semantist_state_region_base(i), cursor, size);
        cursor += size;
    }
    memcpy(&semantist_harness_state, cursor, sizeof(semantist_harness_state));
    semantist_sync_storage_from_state();
    semantist_rebind_storage();
}

SEMANTIST_DRIVER int semantist_import_state(const uint8_t *blob, const struct semantist_reloc *relocs, size_t n) {
    if (!blob) {
        return 1;
    }
    semantist_restore(blob);
    /* A checkpoint contains raw pointer bytes.  Relocations are applied only
     * after the bytes are restored, so a fresh process never dereferences an
     * address from the exporting process.  The harness state is the synthetic
     * region immediately after all target regions. */
    size_t count = semantist_region_count();
    for (size_t i = 0; i < n; i++) {
        size_t source_limit = relocs[i].region == count ? sizeof(semantist_harness_state)
                                                        : (relocs[i].region < count ? (size_t)semantist_state_region_size(relocs[i].region) : 0);
        size_t target_limit = relocs[i].target_region == count ? sizeof(semantist_harness_state)
                                                               : (relocs[i].target_region < count ? (size_t)semantist_state_region_size(relocs[i].target_region) : 0);
        if (!source_limit || !target_limit || relocs[i].offset > source_limit - sizeof(void *)
            || relocs[i].target_offset >= target_limit) {
            return 2;
        }
        uint8_t *source = relocs[i].region == count
            ? (uint8_t *)&semantist_harness_state
            : (uint8_t *)semantist_state_region_base(relocs[i].region);
        uint8_t *target = relocs[i].target_region == count
            ? (uint8_t *)&semantist_harness_state
            : (uint8_t *)semantist_state_region_base(relocs[i].target_region);
        void *address = target + relocs[i].target_offset;
        memcpy(source + relocs[i].offset, &address, sizeof(address));
    }
    semantist_rebind_storage();
    return 0;
}

SEMANTIST_DRIVER uint64_t __semantist_vclock_ns(void) {
    return semantist_harness_state.vclock_ns + (uint64_t)SEMANTIST_CLOCK_ORIGIN_NS;
}

uint8_t __semantist_timer_semantics = SEMANTIST_TIMER_SEMANTICS;

SEMANTIST_DRIVER void __semantist_loop_tick(void) {
    semantist_harness_state.loop_ticks++;
    if (semantist_harness_state.loop_ticks > (uint64_t)SEMANTIST_LOOP_BACKEDGES_PER_SCAN) {
        semantist_harness_state.budget_exceeded = 1;
        fprintf(stderr, "semantist-scan-budget-exceeded ticks=%llu limit=%llu\n",
                (unsigned long long)semantist_harness_state.loop_ticks,
                (unsigned long long)SEMANTIST_LOOP_BACKEDGES_PER_SCAN);
        fflush(stderr);
        abort();
    }
}

SEMANTIST_DRIVER static void __semantist_scan_budget_exceeded(void) {
    __semantist_loop_tick();
}

/* Text replay always executes from a cold state. The Rust engine owns its
 * in-memory prefix tree and calls semantist_checkpoint/restore directly. */
SEMANTIST_DRIVER size_t semantist_snapshot_hits(void) { return 0; }
SEMANTIST_DRIVER size_t semantist_snapshot_saved_scans(void) { return 0; }

SEMANTIST_DRIVER static void semantist_snapshot_report(void) {
    const char *path = getenv("SEMANTIST_SNAPSHOT_STATS_FILE");
    if (!path || !*path) {
        return;
    }
    FILE *out = fopen(path, "a");
    if (!out) {
        return;
    }
    fprintf(out, "{\"snapshot_hits\":%llu,\"snapshot_saved_scans\":%llu}\n",
            (unsigned long long)semantist_snapshot_hits(),
            (unsigned long long)semantist_snapshot_saved_scans());
    fclose(out);
}
"""


def _inout_accessor(abi: TargetAbi) -> str:
    cases = []
    for param in shared_params(abi):
        if param.block_kind != "VAR_IN_OUT":
            continue
        storage = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
        cases.append(
            f'    if (strcmp(name, "{param.name}") == 0) {{ '
            f'*address = &{storage}; *size = sizeof({storage}); return 0; }}'
        )
    return "\n".join([
        "SEMANTIST_DRIVER int semantist_inout_storage(const char *name, void **address, size_t *size) {",
        "    if (!name || !address || !size) return 1;",
        *cases,
        "    return 1;",
        "}",
    ])


def _dump_helpers() -> str:
    return r"""
static FILE *semantist_state_dump_stream;
static int semantist_state_dump_loaded;

SEMANTIST_DRIVER static FILE *semantist_state_dump(void) {
    if (!semantist_state_dump_loaded) {
        semantist_state_dump_loaded = 1;
        const char *path = getenv("SEMANTIST_STATE_DUMP_FILE");
        if (!path || !*path) {
            path = getenv("SEMANTIST_STATE_DUMP");
        }
        if (path && *path) {
            semantist_state_dump_stream = fopen(path, "a");
        }
    }
    return semantist_state_dump_stream;
}

SEMANTIST_DRIVER static void semantist_dump_opaque(FILE *stream, const void *data, size_t len) {
    static const char digits[] = "0123456789abcdef";
    const uint8_t *bytes = (const uint8_t *)data;
    fputc('"', stream);
    fputs("0x", stream);
    for (size_t i = 0; i < len; i++) {
        uint8_t byte = i < 8 ? 0 : bytes[i];
        fputc(digits[byte >> 4], stream);
        fputc(digits[byte & 15], stream);
    }
    fputc('"', stream);
}

SEMANTIST_DRIVER static void semantist_dump_hex(FILE *stream, const void *data, size_t len) {
    static const char digits[] = "0123456789abcdef";
    const uint8_t *bytes = (const uint8_t *)data;
    fputc('"', stream);
    fputs("0x", stream);
    for (size_t i = 0; i < len; i++) {
        fputc(digits[bytes[i] >> 4], stream);
        fputc(digits[bytes[i] & 15], stream);
    }
    fputc('"', stream);
}

SEMANTIST_DRIVER static void semantist_dump_float(FILE *stream, double value) {
    if (value != value || value > 1.7976931348623157e308 || value < -1.7976931348623157e308) {
        fputs("null", stream);
    } else {
        fprintf(stream, "%.17g", value);
    }
}
"""


def _execute_text(abi: TargetAbi) -> str:
    return r"""
SEMANTIST_DRIVER static int semantist_v3_execute(char *data) {
    size_t length = strlen(data);
    char *shared = (char *)malloc(length + 1);
    char *frames = (char *)malloc(length + 1);
    if (!shared || !frames) {
        abort();
    }
    shared[0] = 0;
    frames[0] = 0;
    char *save = NULL;
    for (char *line = strtok_r(data, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char *comma = strchr(line, ',');
        if (!comma) {
            continue;
        }
        int has_cycle = 0;
        for (char *p = line; p < comma; p++) {
            if (*p == '.') {
                has_cycle = 1;
                break;
            }
            if (!isdigit((unsigned char)*p)) {
                break;
            }
        }
        char **target = has_cycle ? &frames : &shared;
        size_t used = strlen(*target);
        size_t add = strlen(line);
        if (used + add + 2 > length + 1) {
            abort();
        }
        memcpy(*target + used, line, add);
        (*target)[used + add] = '\n';
        (*target)[used + add + 1] = 0;
    }

    if (semantist_init(shared) != 0) {
        free(shared);
        free(frames);
        return 2;
    }

    int current_cycle = -1;
    semantist_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    uint64_t dt_ns = (uint64_t)SEMANTIST_SCAN_PERIOD_NS;
    save = NULL;
    for (char *line = strtok_r(frames, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char *inspect = strdup(line);
        if (!inspect) {
            abort();
        }
        char *name = NULL;
        char *value = NULL;
        int cycle = 0;
        if (!split_record(inspect, &name, &value)) {
            free(inspect);
            continue;
        }
        char *dot = strchr(name, '.');
        if (dot) {
            *dot = 0;
            cycle = atoi(name);
            name = dot + 1;
        }
        int is_dt = strcmp(name, "__DT") == 0;
        uint64_t parsed_dt = 0;
        int valid_dt = !is_dt || semantist_parse_unsigned_checked(value, UINT64_MAX, &parsed_dt);
        free(inspect);
        if (!valid_dt) {
            free(shared);
            free(frames);
            return 2;
        }
        if (cycle != current_cycle) {
            if (current_cycle >= 0) {
                if (semantist_step(&frame, dt_ns) != 0) {
                    free(shared);
                    free(frames);
                    return 3;
                }
                if (__SEMANTIST_SINGLE_FRAME__) {
                    break;
                }
            }
            current_cycle = cycle;
            dt_ns = (uint64_t)SEMANTIST_SCAN_PERIOD_NS;
        }
        if (is_dt) {
            dt_ns = parsed_dt;
            continue;
        }
        if (semantist_parse_frame(line, &frame, &dt_ns) != 0) {
            free(shared);
            free(frames);
            return 2;
        }
    }
    if (current_cycle < 0 || !__SEMANTIST_SINGLE_FRAME__) {
        if (semantist_step(&frame, dt_ns) != 0) {
            free(shared);
            free(frames);
            return 3;
        }
    }
    semantist_trace_end();
    semantist_snapshot_report();
    free(shared);
    free(frames);
    return 0;
}
"""


def _generator():
    from compiler.harness import generator

    return generator


def _preamble(abi: TargetAbi, single_frame: bool) -> str:
    frame_decl = "\n".join(f"    {line}" for line in frame_declarations(abi)) or "    uint8_t unused;"
    state_decl = "\n".join(f"    {line}" for line in state_declarations(abi))
    return f"""
#include <errno.h>
#include <float.h>
#include <math.h>
#ifndef SEMANTIST_SCAN_PERIOD_NS
#define SEMANTIST_SCAN_PERIOD_NS 10000000ULL
#endif
#ifndef SEMANTIST_TIMER_SEMANTICS
#define SEMANTIST_TIMER_SEMANTICS 1
#endif
#ifndef SEMANTIST_CLOCK_ORIGIN_NS
#define SEMANTIST_CLOCK_ORIGIN_NS 0ULL
#endif
#ifndef SEMANTIST_LOOP_BACKEDGES_PER_SCAN
#define SEMANTIST_LOOP_BACKEDGES_PER_SCAN 1000000ULL
#endif
#define __SEMANTIST_SINGLE_FRAME__ {1 if single_frame else 0}

static int semantist_numeric_end(const char *end) {{
    while (*end == ' ' || *end == '\\t' || *end == '\\r') end++;
    return *end == 0;
}}

static int semantist_raw_scalar(const char *value, void *dst, size_t size) {{
    if (!value || value[0] != '0' || (value[1] != 'x' && value[1] != 'X')
        || strlen(value + 2) != 2 * size) return 0;
    return semantist_fill_bytes_checked(dst, size, value, 1, 0);
}}

static int semantist_parse_unsigned_checked(const char *value, uint64_t maximum, uint64_t *out) {{
    const char *start = skip_spaces(value);
    if (*start == '-' || !*start) return 0;
    if (*start == '+') start++;
    int base = start[0] == '0' && (start[1] == 'x' || start[1] == 'X') ? 16 : 10;
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(start, &end, base);
    if (errno == ERANGE || end == start || !semantist_numeric_end(end) || parsed > maximum) return 0;
    *out = (uint64_t)parsed;
    return 1;
}}

static int semantist_parse_signed_checked(const char *value, int64_t minimum, int64_t maximum, int64_t *out) {{
    const char *start = skip_spaces(value);
    if (!*start) return 0;
    const char *digits = *start == '-' || *start == '+' ? start + 1 : start;
    int base = digits[0] == '0' && (digits[1] == 'x' || digits[1] == 'X') ? 16 : 10;
    errno = 0;
    char *end = NULL;
    long long parsed = strtoll(start, &end, base);
    if (errno == ERANGE || end == start || !semantist_numeric_end(end) || parsed < minimum || parsed > maximum) return 0;
    *out = (int64_t)parsed;
    return 1;
}}

static int semantist_parse_float_checked(const char *value, int bits, double *out) {{
    const char *start = skip_spaces(value);
    if (!*start) return 0;
    errno = 0;
    char *end = NULL;
    double parsed = strtod(start, &end);
    if (errno == ERANGE || end == start || !semantist_numeric_end(end) || !isfinite(parsed)) return 0;
    if (bits == 32 && (parsed > FLT_MAX || parsed < -FLT_MAX)) return 0;
    *out = parsed;
    return 1;
}}

typedef struct {{
{frame_decl}
}} semantist_frame_t;

typedef struct {{
{state_decl}
}} semantist_harness_state_t;

int semantist_init(const char *shared_records);
int semantist_step(const semantist_frame_t *frame, uint64_t dt_ns);
int semantist_parse_frame(const char *frame_records, semantist_frame_t *out, uint64_t *dt_ns);
int semantist_apply_raw_field(const char *name, const uint8_t *bytes, size_t byte_len,
                              semantist_frame_t *out);
static semantist_harness_state_t semantist_harness_state;
{_dump_helpers()}
"""


def _parse_frame_function(abi: TargetAbi) -> str:
    cases = "\n    ".join(_frame_case_lines(abi, "out") + _inout_frame_cases(abi))
    return f"""
SEMANTIST_DRIVER int semantist_parse_frame(const char *frame_records, semantist_frame_t *out, uint64_t *dt_ns) {{
    if (!frame_records || !out) {{
        return 1;
    }}
    char *buffer = strdup(frame_records);
    if (!buffer) {{
        abort();
    }}
    char *save = NULL;
    for (char *line = strtok_r(buffer, "\\n", &save); line; line = strtok_r(NULL, "\\n", &save)) {{
        char *name = NULL;
        char *value = NULL;
        if (!split_record(line, &name, &value)) {{
            continue;
        }}
        char *dot = strchr(name, '.');
        if (dot) {{
            *dot = 0;
            name = dot + 1;
        }}
        if (strcmp(name, "__DT") == 0) {{
            uint64_t parsed_dt;
            if (!semantist_parse_unsigned_checked(value, UINT64_MAX, &parsed_dt)) {{ free(buffer); return 1; }}
            if (dt_ns) *dt_ns = parsed_dt;
            continue;
        }}
        {cases}
    }}
    free(buffer);
    return 0;
}}
"""


def _raw_copy_lines(spec, access: str) -> list[str]:
    if spec.kind == "scalar":
        return [
            f"    if (byte_len != sizeof({access})) return 1;",
            f"    memcpy(&({access}), bytes, byte_len);",
            "    return 0;",
        ]
    unit = 2 if spec.kind == "string" and spec.name == "WSTRING" else 1
    terminate = 1 if spec.kind == "string" else 0
    return [
        f"    if (!semantist_copy_raw_bytes(({access}), sizeof({access}), bytes, byte_len, {unit}, {terminate})) return 1;",
        "    return 0;",
    ]


def _raw_field_function(abi: TargetAbi) -> str:
    cases: list[str] = []
    for param in frame_params(abi):
        access = f"out->{_field_name(param)}"
        cases.append(f'    if (strcmp(name, "{param.name}") == 0) {{')
        cases.extend(_raw_copy_lines(param.spec, access))
        cases.append("    }")
    for param in shared_params(abi):
        if param.block_kind != "VAR_IN_OUT":
            continue
        access = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
        cases.append(f'    if (strcmp(name, "{param.name}") == 0) {{')
        cases.extend(_raw_copy_lines(param.spec, access))
        cases.append("    }")
    return f"""
static int semantist_copy_raw_bytes(void *dst, size_t dst_len, const uint8_t *bytes,
                                    size_t byte_len, size_t unit, int terminate) {{
    if (!dst || (!bytes && byte_len) || unit == 0) return 0;
    size_t limit = dst_len;
    if (terminate) {{
        if (dst_len < unit) return 0;
        limit -= unit;
    }}
    if (byte_len > limit || byte_len % unit != 0) return 0;
    memset(dst, 0, dst_len);
    if (byte_len) memcpy(dst, bytes, byte_len);
    return 1;
}}

SEMANTIST_DRIVER int semantist_apply_raw_field(const char *name, const uint8_t *bytes,
                                               size_t byte_len, semantist_frame_t *out) {{
    if (!name || !out || (!bytes && byte_len)) return 1;
{chr(10).join(cases)}
    return 1;
}}
"""


def _raw_shared_function(abi: TargetAbi) -> str:
    """Expose the same raw path for shared VAR_IN_OUT backing objects."""
    cases: list[str] = []
    for param in shared_params(abi):
        if param.block_kind != "VAR_IN_OUT":
            continue
        access = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
        cases.append(f'    if (strcmp(name, "{param.name}") == 0) {{')
        if param.spec.kind == "scalar":
            cases.extend(_raw_copy_lines(param.spec, f"{access}"))
        else:
            unit = 2 if param.spec.kind == "string" and param.spec.name == "WSTRING" else 1
            terminate = 1 if param.spec.kind == "string" else 0
            cases.extend([
                f"    if (!semantist_copy_raw_bytes({access}, sizeof({access}), bytes, byte_len, {unit}, {terminate})) return 1;",
                "    return 0;",
            ])
        cases.append("    }")
    return f"""
SEMANTIST_DRIVER int semantist_apply_raw_shared_field(const char *name,
                                                     const uint8_t *bytes,
                                                     size_t byte_len) {{
    if (!name || (!bytes && byte_len)) return 1;
{chr(10).join(cases)}
    return 1;
}}
"""


def generate_function_block_harness(abi: TargetAbi) -> str:
    global _FIELD_BASE
    _FIELD_BASE = 1
    g = _generator()
    name = abi.name
    fields = abi.state_fields
    state_decls = "\n".join(g.fb_field_declaration(param) for param in fields)
    layout_asserts = "\n".join(
        [f'_Static_assert(sizeof(semantist_fb_state_t) == {abi.instance_size}, "{name} instance size");']
        + [
            f'_Static_assert(offsetof(semantist_fb_state_t, {g.field_name(param)}) == {param.offset}, '
            f'"{name}.{param.name} offset");'
            for param in fields
        ]
    )
    escape_lines = [
        f"    escape_bytes(&{STATE_VAR}.inst.{g.field_name(param)}, sizeof({STATE_VAR}.inst.{g.field_name(param)}));"
        for param in abi.outputs
    ] or [f"    escape_bytes(&{STATE_VAR}.inst, sizeof({STATE_VAR}.inst));"]
    trace_lines = g.fb_output_trace_lines(fields)
    trace_code = "\n".join(line.replace("inst->", f"{STATE_VAR}.inst.") for line in trace_lines)
    apply_lines = "\n    ".join(_apply_frame_lines(abi)) or "    (void)frame;"
    pointer_lines = "\n    ".join(_pointer_init_lines(abi))
    shared_cases = "\n    ".join(_shared_case_lines(abi, "semantist_harness_state"))
    dump_lines = "\n    ".join(_state_dump_lines(abi))
    abstract_lines = "\n".join(
        line.replace("inst->", f"{STATE_VAR}.inst.") for line in g.fb_abstract_state_lines(abi.snapshot_fields)
    )
    ctor = f"{name}__ctor(&{STATE_VAR}.inst);"
    target = f"{name}(&{STATE_VAR}.inst);"
    return f"""{g.HEADER}
{g.INCLUDES}
{g.COMMON_C}{g.FB_RUNTIME_C}
typedef struct {{
    void *__vtable;
{state_decls}
}} semantist_fb_state_t;
{layout_asserts}

extern void {name}(semantist_fb_state_t *);
extern void {name}__ctor(semantist_fb_state_t *);
{_preamble(abi, False)}
{chr(10).join(external_backing_declarations(abi))}
{_accessor_declarations(abi)}
{_inout_accessor(abi)}
{_parse_frame_function(abi)}
{_raw_field_function(abi)}
{_raw_shared_function(abi)}

static uint64_t semantist_fb_abstract_state(const semantist_fb_state_t *inst) {{
{abstract_lines}
}}

SEMANTIST_DRIVER static int semantist_apply_shared(const char *shared_records) {{
    if (!shared_records || !*shared_records) {{
        return 0;
    }}
    char *buffer = strdup(shared_records);
    if (!buffer) {{
        abort();
    }}
    char *save = NULL;
    for (char *line = strtok_r(buffer, "\\n", &save); line; line = strtok_r(NULL, "\\n", &save)) {{
        char *name = NULL;
        char *value = NULL;
        if (!split_record(line, &name, &value)) {{
            continue;
        }}
        char *dot = strchr(name, '.');
        if (dot) {{
            name = dot + 1;
        }}
        if (strcmp(name, "__INIT") == 0) {{
            if (strcmp(value, "cold") != 0) {{
                free(buffer);
                return 1;
            }}
            continue;
        }}
        {shared_cases}
    }}
    free(buffer);
    return 0;
}}

SEMANTIST_DRIVER int semantist_init(const char *shared_records) {{
    semantist_state_reset();
    memset(&{STATE_VAR}, 0, sizeof({STATE_VAR}));
{_storage_reset_code(abi)}
{pointer_lines}
    {ctor}
    if (semantist_apply_shared(shared_records) != 0) {{
        return 2;
    }}
    {STATE_VAR}.initialized = 1;
    {STATE_VAR}.vclock_ns = 0;
    {STATE_VAR}.frame_index = 0;
    return 0;
}}
{_execute_text(abi)}

SEMANTIST_DRIVER int semantist_step(const semantist_frame_t *frame, uint64_t dt_ns) {{
    if (!{STATE_VAR}.initialized) {{
        return 2;
    }}
    {STATE_VAR}.vclock_ns += dt_ns;
    {STATE_VAR}.frame_index++;
    {STATE_VAR}.loop_ticks = 0;
    {apply_lines}
    uint64_t abstract_before = semantist_fb_abstract_state(&{STATE_VAR}.inst);
    __semantist_semantic_cycle((uint32_t)({STATE_VAR}.frame_index - 1));
    SEMANTIST_TIMING_BEGIN();
    {target}
    SEMANTIST_TIMING_END(semantist_timing_call_ns);
    __semantist_semantic_state(abstract_before, semantist_fb_abstract_state(&{STATE_VAR}.inst));
{chr(10).join(escape_lines)}
#ifndef SEMANTIST_KLEE
    {trace_code}
    {{
        FILE *stream = semantist_state_dump();
        if (stream) {{
            uint64_t frame_index = {STATE_VAR}.frame_index - 1;
            {dump_lines}
        }}
    }}
#endif
    return 0;
}}

SEMANTIST_INPUT static int semantist_run_input(char *data) {{
    semantist_state_reset();
    return semantist_v3_execute(data);
}}
{g.DRIVER_C}"""


def generate_function_harness(abi: TargetAbi) -> str:
    global _FIELD_BASE
    _FIELD_BASE = 0
    g = _generator()
    name = abi.name
    ret_c = "void" if abi.ret_by_value is None else abi.ret.spec.c_type
    proto_args = ", ".join(g.c_argument_type(slot) for slot in abi.call_slots) or "void"
    frame_call = _function_frame_call(abi)
    shared_cases = "\n    ".join(_shared_case_lines(abi, "semantist_harness_state"))
    dump_lines = _function_state_dump_lines(abi)
    return f"""{g.HEADER}
{g.INCLUDES}
{g.COMMON_C}
{_preamble(abi, False)}
{chr(10).join(external_backing_declarations(abi))}
{_accessor_declarations(abi)}
{_inout_accessor(abi)}
{_parse_frame_function(abi)}
{_raw_field_function(abi)}
{_raw_shared_function(abi)}

extern {ret_c} {name}({proto_args});

SEMANTIST_DRIVER static int semantist_v3_execute(char *data);

SEMANTIST_INPUT static int semantist_run_input(char *data) {{
    semantist_state_reset();
    return semantist_v3_execute(data);
}}

{frame_call}

SEMANTIST_DRIVER static int semantist_apply_shared(const char *shared_records) {{
    if (!shared_records || !*shared_records) {{
        return 0;
    }}
    char *buffer = strdup(shared_records);
    if (!buffer) {{
        abort();
    }}
    char *save = NULL;
    for (char *line = strtok_r(buffer, "\\n", &save); line; line = strtok_r(NULL, "\\n", &save)) {{
        char *name = NULL;
        char *value = NULL;
        if (!split_record(line, &name, &value)) {{
            continue;
        }}
        char *dot = strchr(name, '.');
        if (dot) {{
            name = dot + 1;
        }}
        if (strcmp(name, "__INIT") == 0) {{
            if (strcmp(value, "cold") != 0) {{
                free(buffer);
                return 1;
            }}
            continue;
        }}
        {shared_cases}
    }}
    free(buffer);
    return 0;
}}

SEMANTIST_DRIVER int semantist_init(const char *shared_records) {{
    semantist_state_reset();
    memset(&{STATE_VAR}, 0, sizeof({STATE_VAR}));
{_storage_reset_code(abi)}
    if (semantist_apply_shared(shared_records) != 0) {{
        return 2;
    }}
    {STATE_VAR}.initialized = 1;
    {STATE_VAR}.vclock_ns = 0;
    {STATE_VAR}.frame_index = 0;
    return 0;
}}
{_execute_text(abi)}

SEMANTIST_DRIVER int semantist_step(const semantist_frame_t *frame, uint64_t dt_ns) {{
    if (!{STATE_VAR}.initialized) {{
        return 2;
    }}
    {STATE_VAR}.vclock_ns += dt_ns;
    {STATE_VAR}.frame_index++;
    {STATE_VAR}.loop_ticks = 0;
    semantist_frame_t local = *frame;
    __semantist_semantic_cycle((uint32_t)({STATE_VAR}.frame_index - 1));
    SEMANTIST_TIMING_BEGIN();
    semantist_function_call(&local);
    SEMANTIST_TIMING_END(semantist_timing_call_ns);
#ifndef SEMANTIST_KLEE
    semantist_trace_mark('|');
    {{
        FILE *stream = semantist_state_dump();
        if (stream) {{
            uint64_t frame_index = {STATE_VAR}.frame_index - 1;
            {dump_lines}
        }}
    }}
#endif
    return 0;
}}

{g.DRIVER_C}"""


def _function_frame_call(abi: TargetAbi) -> str:
    g = _generator()
    ret_c = "void" if abi.ret_by_value is None else abi.ret.spec.c_type
    arguments: list[str] = []
    for slot in abi.call_slots:
        param = slot.param
        spec = param.spec
        if param.block_kind == "RETURN":
            if spec.kind == "scalar":
                arguments.append(f"&{STATE_VAR}.result")
            else:
                arguments.append(f"{STATE_VAR}.result")
        elif spec.kind == "pointer":
            arguments.append(_external_storage_name(param))
        elif param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT"):
            if spec.kind == "scalar":
                storage = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
                arguments.append(f"&{storage}")
            else:
                arguments.append(_external_storage_name(param))
        elif param.block_kind == "VAR_INPUT" and param.constant:
            if spec.kind == "scalar":
                arguments.append(f"{STATE_VAR}.{_storage_name(param)}")
            else:
                arguments.append(_external_storage_name(param))
        else:
            arguments.append(f"local->{_field_name(param)}")
    call_args = ", ".join(arguments)
    if abi.ret_by_value is not None:
        call = f"    {STATE_VAR}.result = {abi.name}({call_args});"
        if abi.ret.spec.kind == "scalar":
            # result already stored; scale for the dump is the same field.
            pass
    else:
        call = f"    {abi.name}({call_args});"
    pointer_setup = []
    for slot in abi.call_slots:
        param = slot.param
        if param.block_kind == "VAR_INPUT" and param.spec.kind == "pointer":
            storage = _external_storage_name(param)
            field = f"local->{_field_name(param)}"
            # Pointer frame fields are bounded backing bytes.  Copy them into
            # the persistent storage object before passing the real pointer to
            # the target function; never treat the testcase bytes as a process
            # address.
            pointer_setup.extend([
                f"    memset({storage}, 0, sizeof({storage}));",
                f"    memcpy({storage}, {field}, sizeof({storage}) < sizeof({field}) ? sizeof({storage}) : sizeof({field}));",
            ])
    if pointer_setup:
        call = "\n".join(pointer_setup) + "\n" + call
    escapes = []
    for slot in abi.call_slots:
        param = slot.param
        if param.block_kind not in ("RETURN", "VAR_OUTPUT", "VAR_IN_OUT"):
            continue
        if param.spec.kind == "pointer":
            continue
        if param.block_kind == "RETURN":
            escapes.append(f"    escape_bytes(&{STATE_VAR}.result, sizeof({STATE_VAR}.result));")
        elif param.spec.kind == "scalar":
            escapes.append(f"    escape_bytes(&{STATE_VAR}.{_storage_name(param)}, sizeof({STATE_VAR}.{_storage_name(param)}));")
        else:
            storage = _external_storage_name(param)
            escapes.append(f"    escape_bytes({storage}, sizeof({storage}));")
    traces = []
    for slot in abi.call_slots:
        param = slot.param
        if not g.traceable(param.spec):
            continue
        if param.block_kind == "RETURN":
            traces.append(f"    semantist_trace_bytes(&{STATE_VAR}.result, sizeof({STATE_VAR}.result));")
        elif param.block_kind in ("VAR_OUTPUT", "VAR_IN_OUT"):
            if param.spec.kind == "scalar":
                traces.append(f"    semantist_trace_bytes(&{STATE_VAR}.{_storage_name(param)}, sizeof({STATE_VAR}.{_storage_name(param)}));")
            else:
                storage = _external_storage_name(param)
                traces.append(f"    semantist_trace_bytes({storage}, sizeof({storage}));")
    body = "\n".join(escapes) + "\n" + "\n".join(traces)
    return f"""
SEMANTIST_DRIVER static void semantist_function_call(semantist_frame_t *local) {{
{call}
{body}
}}
"""


def _function_state_dump_lines(abi: TargetAbi) -> str:
    lines: list[str] = []
    for param in list(abi.state_fields or []) + list(abi.params):
        if param.block_kind == "VAR_IN_OUT":
            access = _external_storage_name(param) if _uses_external_storage(param) else f"{STATE_VAR}.{_storage_name(param)}"
            lines.append(f'fprintf(stream, "\\"%s\\":");' % param.name)
            lines.append(f'fprintf(stream, "%lld", (long long){access});')
    if abi.ret is not None:
        spec = abi.ret.spec
        lines.append('fprintf(stream, "\\"__result\\":");')
        if spec.kind == "scalar":
            if spec.c_type and spec.c_type.startswith("u"):
                lines.append(f'fprintf(stream, "%llu", (unsigned long long){STATE_VAR}.result);')
            else:
                lines.append(f'fprintf(stream, "%lld", (long long){STATE_VAR}.result);')
        else:
            lines.append(f"semantist_dump_hex(stream, &{STATE_VAR}.result, sizeof({STATE_VAR}.result));")
    return "\n            ".join(lines)


def frame_header(abi: TargetAbi) -> str:
    """Header shared by the native harness and the KLEE driver."""
    frame_decl = "\n".join(f"    {line}" for line in frame_declarations(abi)) or "    uint8_t unused;"
    guard = f"SEMANTIST_FRAME_{abi.name.upper()}_H"
    return f"""/* Generated by compiler/harness/v3gen.py. Do not edit. */
#ifndef {guard}
#define {guard}
#include <stddef.h>
#include <stdint.h>

typedef struct {{
{frame_decl}
}} semantist_frame_t;

struct semantist_reloc {{
    uint32_t region;
    uint64_t offset;
    uint32_t target_region;
    uint64_t target_offset;
}};

void semantist_init(const char *shared_records);
int semantist_step(const semantist_frame_t *frame, uint64_t dt_ns);
int semantist_parse_frame(const char *frame_records, semantist_frame_t *out, uint64_t *dt_ns);
int semantist_apply_raw_field(const char *name, const uint8_t *bytes, size_t byte_len,
                              semantist_frame_t *out);
int semantist_apply_raw_shared_field(const char *name, const uint8_t *bytes, size_t byte_len);
size_t semantist_state_size(void);
void *semantist_harness_state_base(void);
void semantist_checkpoint(uint8_t *out);
void semantist_restore(const uint8_t *in);
int semantist_import_state(const uint8_t *blob, const struct semantist_reloc *relocs, size_t n);
uint64_t __semantist_vclock_ns(void);

#endif
"""
