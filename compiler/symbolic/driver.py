"""Generate a KLEE driver for one bounded suffix query (spec 6.3)."""

from __future__ import annotations

from typing import Any

def _c_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _action_fields(abi: dict[str, Any]) -> list[dict[str, Any]]:
    return [*abi.get("frame_fields", []), *(entry for entry in abi.get("shared_fields", [])
              if entry.get("block_kind") == "VAR_IN_OUT")]


def symbolic_scope(abi: dict[str, Any], requested: list[str] | None) -> tuple[list[str], list[str]]:
    """Name fields the current KLEE driver can actually make symbolic."""
    entries = _action_fields(abi)
    names = [entry["name"] for entry in entries] if requested is None else requested
    supported = []
    concretized = []
    by_name = {entry["name"]: entry for entry in entries}
    for name in names:
        entry = by_name.get(name)
        if entry is None:
            concretized.append(name)
            continue
        spec = entry["spec"]
        # The frame is a byte-addressable ABI boundary.  KLEE can therefore
        # make floating point, string and pointer backing bytes symbolic too;
        # the candidate decoder performs the typed conversion afterwards.
        scalar = spec.get("kind") == "scalar"
        direct = spec.get("kind") in ("string", "pointer")
        element = spec.get("element") or {}
        array = (spec.get("kind") == "array"
                 and int(spec.get("count") or 0) <= 32
                 and int(spec.get("byte_size") or spec.get("size") or 0) <= 4096)
        if scalar or direct or array:
            supported.append(name)
        else:
            concretized.append(name)
    return supported, concretized


def _records(values: dict[str, str], fields: list[dict[str, Any]]) -> str:
    return "\n".join(f"{entry['name']},{entry['spec'].get('name') or 'ARRAY'},{values[entry['name']]}"
                     for entry in fields if entry["name"] in values)


def generate_driver(
    abi: dict[str, Any],
    *,
    goal_id: int,
    dt_ns: int,
    base_frame: dict[str, str] | None = None,
    symbolic_fields: list[str] | None = None,
    shared_records: str = "",
    window: int = 1,
    prefix_frames: list[dict[str, str]] | None = None,
    dt_prefix_ns: int = 10_000_000,
    prefix_dt_ns: list[int | None] | None = None,
    state_bytes: bytes | None = None,
    relocations: list[dict[str, Any]] | None = None,
) -> str:
    frame = abi.get("frame") or {}
    size = max(1, int(frame.get("size") or 0))
    align = int(frame.get("align") or 1)
    base_frame = base_frame or {}
    all_fields = list(frame.get("fields", []))
    inout_fields = [entry for entry in abi.get("shared_fields", [])
                    if entry.get("block_kind") == "VAR_IN_OUT"]
    action_fields = _action_fields(abi)
    symbolic = set(symbolic_scope(abi, symbolic_fields)[0])
    escaped = _c_string(shared_records)

    declarations: list[str] = []
    body: list[str] = []

    def emit_frame(values: dict[str, str]) -> None:
        records = _c_string(_records(values, action_fields))
        body.append(f'    if (semantist_parse_frame("{records}", storage, 0) != 0) return 2;')

    body.append(f'    if (semantist_init("{escaped}") != 0) return 2;')
    if state_bytes:
        declarations.append(
            "static const unsigned char initial_state[] = {"
            + ",".join(f"0x{byte:02x}" for byte in state_bytes)
            + "};"
        )
        relocation_entries = relocations or []
        if relocation_entries:
            declarations.append(
                "static const struct semantist_reloc initial_relocs[] = {"
                + ",".join(
                    "{%d,%d,%d,%d}" % (
                        int(item["region"]), int(item["offset"]),
                        int(item["target_region"]), int(item["target_offset"]),
                    )
                    for item in relocation_entries
                )
                + "};"
            )
            body.append(
                "    if (semantist_import_state(initial_state, initial_relocs, "
                f"{len(relocation_entries)}UL) != 0) return 4;"
            )
        else:
            body.append("    if (semantist_import_state(initial_state, 0, 0) != 0) return 4;")
    for index, _prefix in enumerate(prefix_frames or []):
        emit_frame(_prefix)
        prefix_dt = (prefix_dt_ns[index] if prefix_dt_ns is not None and index < len(prefix_dt_ns)
                     and prefix_dt_ns[index] is not None else dt_prefix_ns)
        body.append(f"    if (semantist_step(storage, {prefix_dt}ULL) != 0) return 3;")

    for t in range(max(1, window)):
        emit_frame(base_frame)
        for entry in all_fields:
            if entry["name"] not in symbolic:
                continue
            spec = entry["spec"]
            offset = int(entry["offset"])
            prefix = f"f{t}." if window > 1 else ""
            if spec.get("kind") == "scalar":
                size_bytes = int(entry["size"])
                symbol = f"sym_{t}_{entry['c_name']}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                body.append(f"    memcpy(storage+{offset}, {symbol}, {size_bytes});")
                if spec.get("name") == "BOOL":
                    body.append(f"    klee_assume(*(uint8_t*)(storage+{offset}) <= 1);")
            elif spec.get("kind") == "string":
                size_bytes = int(entry["size"])
                symbol = f"sym_{t}_{entry['c_name']}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                width = max(1, int(spec.get("size") or 1))
                units = max(1, size_bytes // width)
                if width == 1:
                    for unit in range(max(0, units - 1)):
                        body.append(
                            f"    klee_assume({symbol}[{unit}] == 0 || "
                            f"({symbol}[{unit}] >= 32 && {symbol}[{unit}] <= 126));"
                        )
                    body.append(f"    {symbol}[{size_bytes - 1}] = 0;")
                else:
                    for unit in range(max(0, units - 1)):
                        body.append(
                            f"    klee_assume(((uint16_t *){symbol})[{unit}] == 0 || "
                            f"(((uint16_t *){symbol})[{unit}] >= 32 && ((uint16_t *){symbol})[{unit}] <= 126));"
                        )
                    body.append(f"    ((uint16_t *){symbol})[{units - 1}] = 0;")
                body.append(f"    memcpy(storage+{offset}, {symbol}, {size_bytes});")
            elif spec.get("kind") == "pointer":
                element = spec.get("element") or {}
                size_bytes = min(
                    int(entry["size"]),
                    int(element.get("byte_size") or element.get("size") or 1),
                )
                size_bytes = max(1, size_bytes)
                symbol = f"sym_{t}_{entry['c_name']}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                body.append(f"    memcpy(storage+{offset}, {symbol}, {size_bytes});")
            elif spec.get("kind") == "array":
                element = spec.get("element") or {}
                element_size = int(element.get("byte_size") or element.get("size") or 1)
                count = min(int(spec.get("count") or 0), 32)
                for element_index in range(count):
                    symbol = f"sym_{t}_{entry['c_name']}_{element_index}"
                    declarations.append(f"static unsigned char {symbol}[{element_size}];")
                    body.append(
                        f'    klee_make_symbolic({symbol}, {element_size}, '
                        f'"{prefix}{entry["name"]}[{element_index}]");'
                    )
                    body.append(
                        f"    memcpy(storage+{offset + element_index * element_size}, {symbol}, {element_size});"
                    )
                    if element.get("name") == "BOOL":
                        body.append(
                            f"    klee_assume(*(uint8_t*)(storage+{offset + element_index * element_size}) <= 1);"
                        )
        for index, entry in enumerate(inout_fields):
            if entry["name"] not in symbolic:
                continue
            spec = entry["spec"]
            prefix = f"f{t}." if window > 1 else ""
            pointer = f"inout_{t}_{index}"
            length = f"inout_len_{t}_{index}"
            body.append(f"    void *{pointer} = 0; size_t {length} = 0;")
            body.append(f'    if (semantist_inout_storage("{_c_string(entry["name"])}", &{pointer}, &{length}) != 0) return 2;')
            if spec.get("kind") == "scalar":
                size_bytes = int(spec.get("byte_size") or spec.get("size") or 0)
                symbol = f"sym_inout_{t}_{index}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f"    if ({length} < {size_bytes}) return 2;")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                body.append(f"    memcpy({pointer}, {symbol}, {size_bytes});")
                if spec.get("name") == "BOOL":
                    body.append(f"    klee_assume({symbol}[0] <= 1);")
            elif spec.get("kind") == "string":
                size_bytes = int(spec.get("byte_size") or spec.get("size") or 1)
                symbol = f"sym_inout_{t}_{index}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f"    if ({length} < {size_bytes}) return 2;")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                width = max(1, int(spec.get("size") or 1))
                units = max(1, size_bytes // width)
                if width == 1:
                    for unit in range(max(0, units - 1)):
                        body.append(
                            f"    klee_assume({symbol}[{unit}] == 0 || "
                            f"({symbol}[{unit}] >= 32 && {symbol}[{unit}] <= 126));"
                        )
                    body.append(f"    {symbol}[{size_bytes - 1}] = 0;")
                else:
                    for unit in range(max(0, units - 1)):
                        body.append(
                            f"    klee_assume(((uint16_t *){symbol})[{unit}] == 0 || "
                            f"(((uint16_t *){symbol})[{unit}] >= 32 && ((uint16_t *){symbol})[{unit}] <= 126));"
                        )
                    body.append(f"    ((uint16_t *){symbol})[{units - 1}] = 0;")
                body.append(f"    memcpy({pointer}, {symbol}, {size_bytes});")
            elif spec.get("kind") == "pointer":
                element = spec.get("element") or {}
                size_bytes = max(1, int(element.get("byte_size") or element.get("size") or 1))
                symbol = f"sym_inout_{t}_{index}"
                declarations.append(f"static unsigned char {symbol}[{size_bytes}];")
                body.append(f"    if ({length} < {size_bytes}) return 2;")
                body.append(f'    klee_make_symbolic({symbol}, {size_bytes}, "{prefix}{entry["name"]}");')
                body.append(f"    memcpy({pointer}, {symbol}, {size_bytes});")
            elif spec.get("kind") == "array":
                element = spec.get("element") or {}
                element_size = int(element.get("byte_size") or element.get("size") or 1)
                count = int(spec.get("count") or 0)
                body.append(f"    if ({length} < {element_size * count}) return 2;")
                for element_index in range(count):
                    symbol = f"sym_inout_{t}_{index}_{element_index}"
                    declarations.append(f"static unsigned char {symbol}[{element_size}];")
                    body.append(f'    klee_make_symbolic({symbol}, {element_size}, '
                                f'"{prefix}{entry["name"]}[{element_index}]");')
                    body.append(f"    memcpy((unsigned char*){pointer}+{element_index * element_size}, {symbol}, {element_size});")
                    if element.get("name") == "BOOL":
                        body.append(f"    klee_assume({symbol}[0] <= 1);")
        body.append(f"    if (semantist_step(storage, {dt_ns}ULL) != 0) return 3;")

    lines = [
        "#include <klee/klee.h>",
        "#include <stdint.h>",
        "#include <string.h>",
        "struct semantist_reloc { uint32_t region; uint64_t offset; uint32_t target_region; uint64_t target_offset; };",
        "",
        "extern int semantist_init(const char *shared_records);",
        "extern int semantist_parse_frame(const char *records, void *frame, uint64_t *dt_ns);",
        "extern int semantist_step(const void *frame, uint64_t dt_ns);",
        "extern int semantist_import_state(const unsigned char *blob, const struct semantist_reloc *relocs, unsigned long n);",
        "extern int semantist_inout_storage(const char *name, void **address, size_t *size);",
        "int __semantist_goal_id = %d;" % goal_id,
        *declarations,
        "",
        "int main(void) {",
        f"    unsigned char storage[{size}] __attribute__((aligned({max(1, align)})));",
        "    memset(storage, 0, sizeof(storage));",
        *body,
        "    return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"
