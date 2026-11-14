"""ExecutionBundle documents: abi.json, state-layout.json, execution-contract.

The ABI is exported from the same ``TargetAbi`` the C harness is generated
from.  Layouts are computed with the C ABI rules used by the generated structs;
the target's writable-global regions come from the prepared IR that also gets
the ``__semantist_state_regions`` table.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compiler.harness.abi import Param, TargetAbi, TypeSpec, frame_params, shared_params
from compiler.harness.llvm_ir import (
    LLVMType,
    parse_named_definitions,
    parse_type,
    size_align,
    struct_field_layout,
)
from compiler.harness.state_table import state_table_globals

ABI_SCHEMA = "semantist.abi/1.0.0"
LAYOUT_SCHEMA = "semantist.state-layout/1.0.0"
CONTRACT_SCHEMA = "semantist.execution-contract/1.0.0"
SNAPSHOT_SCHEMA_VERSION = 1
POINTER_STORAGE_CAPACITY = 4096


def spec_json(spec: TypeSpec) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": spec.kind,
        "name": spec.name,
        "c_type": spec.c_type,
        "size": spec.size,
        "byte_size": spec.byte_size(),
        "align": spec.align,
        "length": spec.length,
        "count": spec.count,
    }
    if spec.element is not None:
        value["element"] = spec_json(spec.element)
    if spec.members:
        value["members"] = [
            {
                "name": member.name,
                "offset": member.offset,
                "size": member.size,
                "spec": spec_json(member.spec),
            }
            for member in spec.members
        ]
    return value


def param_json(param: Param) -> dict[str, Any]:
    return {
        "name": param.name,
        "block_kind": param.block_kind,
        "constant": param.constant,
        "offset": param.offset,
        "index": param.index,
        "spec": spec_json(param.spec),
    }


def frame_layout(abi: TargetAbi) -> dict[str, Any]:
    entries = frame_params(abi)
    fields: list[FieldLayout] = []
    for param in entries:
        # The wire frame stores pointer inputs as their bounded backing object,
        # matching ``frame_declarations`` in v3gen.py.  The logical ABI spec
        # still reports an 8-byte pointer value for state/layout purposes, but
        # the frame region must describe the bytes the parser actually writes.
        if param.spec.kind == "pointer":
            size, align = POINTER_STORAGE_CAPACITY, 1
        else:
            size, align = _spec_size_align(param.spec)
        fields.append(FieldLayout(param.name, size, max(1, align)))
    size, align, layout = region_layout(fields)
    return {
        "size": size,
        "align": align,
        "fields": [
            {
                "name": param.name,
                "c_name": f"f{param.index - 1}" if abi.kind == "FUNCTION_BLOCK" else f"f{param.index}",
                "offset": entry["offset"],
                "size": entry["size"],
                "align": entry["align"],
                "block_kind": param.block_kind,
                "spec": spec_json(param.spec),
            }
            for param, entry in zip(entries, layout)
        ],
    }


def abi_json(abi: TargetAbi) -> dict[str, Any]:
    return {
        "schema_version": ABI_SCHEMA,
        "target": {
            "name": abi.name,
            "kind": abi.kind,
            "instance_size": abi.instance_size,
            "instance_align": abi.instance_align,
        },
        "frame": frame_layout(abi),
        "frame_fields": [param_json(param) for param in frame_params(abi)],
        "shared_fields": [param_json(param) for param in shared_params(abi)],
        "state_fields": [param_json(param) for param in abi.state_fields],
        "outputs": [param_json(param) for param in abi.outputs],
        "snapshot_fields": [param_json(param) for param in abi.snapshot_fields],
        "return": param_json(abi.ret) if abi.ret is not None else None,
    }


# ---------------------------------------------------------------------------
# C layout of the generator's semantist_harness_state_t
# ---------------------------------------------------------------------------


@dataclass
class FieldLayout:
    name: str
    size: int
    align: int
    kind: str = "value"
    pointee: int = 0


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def _spec_size_align(spec: TypeSpec) -> tuple[int, int]:
    return spec.byte_size(), max(1, spec.align)


def harness_fields(abi: TargetAbi) -> list[FieldLayout]:
    """Fields of ``semantist_harness_state_t`` in declaration order."""
    fields: list[FieldLayout] = []
    if abi.kind == "FUNCTION_BLOCK":
        fields.append(FieldLayout("inst", abi.instance_size, max(1, abi.instance_align), "fb_instance"))
    candidates: list[Param] = list(abi.params)
    if abi.kind == "FUNCTION":
        candidates += [slot.param for slot in abi.call_slots if slot.param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT")]
        candidates += [param for param in abi.params if param.block_kind == "VAR_INPUT" and param.constant]
    else:
        candidates += [param for param in (abi.state_fields or []) if param not in abi.params]
    seen: set[int] = set()
    for param in candidates:
        if param.index is None or param.index in seen:
            continue
        if abi.kind == "FUNCTION":
            needs_backing = (
                param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT")
                or param.spec.kind == "pointer"
                or (param.block_kind == "VAR_INPUT" and param.constant)
            )
        else:
            needs_backing = param.block_kind == "VAR_IN_OUT" or param.spec.kind == "pointer"
        if not needs_backing:
            continue
        seen.add(param.index)
        if param.spec.kind == "pointer":
            fields.append(
                FieldLayout(
                    f"backing_{param.index}", max(POINTER_STORAGE_CAPACITY, param.spec.pointee_bytes()),
                    8, "backing", param.spec.pointee_bytes()
                )
            )
        else:
            size, align = _spec_size_align(param.spec)
            fields.append(FieldLayout(f"backing_{param.index}", size, align, "backing"))
    if abi.kind == "FUNCTION" and abi.ret is not None:
        size, align = _spec_size_align(abi.ret.spec)
        fields.append(FieldLayout("result", size, align, "result"))
    fields += [
        FieldLayout("vclock_ns", 8, 8),
        FieldLayout("frame_index", 8, 8),
        FieldLayout("loop_ticks", 8, 8),
        FieldLayout("initialized", 1, 1),
        FieldLayout("budget_exceeded", 1, 1),
        FieldLayout("oracle_previous_valid", 1, 1),
    ]
    from compiler.harness.oracle import history_fields

    fields.extend(FieldLayout(field.state_name, 16, 16) for field in history_fields(abi))
    return fields


def region_layout(fields: list[FieldLayout], base: int = 0) -> tuple[int, int, list[dict[str, Any]]]:
    offset = base
    max_align = 1
    regions: list[dict[str, Any]] = []
    for field in fields:
        offset = align_up(offset, field.align)
        regions.append({"name": field.name, "offset": offset, "size": field.size, "align": field.align,
                        "kind": field.kind, "pointee": field.pointee})
        offset += field.size
        max_align = max(max_align, field.align)
    return align_up(offset, max_align) - base, max_align, regions


def param_pointer_offsets(abi: TargetAbi, base: int = 0) -> list[int]:
    offsets: list[int] = []
    if abi.kind != "FUNCTION_BLOCK":
        return offsets
    # The vtable is field 0 of every FB instance.
    offsets.append(base)
    for param in abi.state_fields:
        if param.offset is None:
            continue
        if param.spec.kind == "pointer" or param.block_kind == "VAR_IN_OUT":
            offsets.append(base + param.offset)
    return sorted(set(offsets))


def harness_state_layout(abi: TargetAbi) -> dict[str, Any]:
    fields = harness_fields(abi)
    size, align, field_layouts = region_layout(fields)
    pointers = []
    if abi.kind == "FUNCTION_BLOCK":
        pointers.extend(param_pointer_offsets(abi, 0))
    for entry in field_layouts:
        if entry["kind"] == "backing" and entry["pointee"]:
            # The backing array itself is not a pointer; nothing to relocate.
            pass
    return {"name": "semantist_harness_state", "size": size, "align": align,
            "fields": field_layouts, "pointer_offsets": pointers}


def _pointer_offsets(type_text: str, definitions: dict[str, LLVMType], base: int = 0) -> list[int]:
    try:
        ty = parse_type(type_text)
    except (AssertionError, ValueError):
        return []
    return _offsets_of(ty, definitions, base)


def _offsets_of(ty: LLVMType, definitions: dict[str, LLVMType], base: int) -> list[int]:
    if ty.kind == "ptr":
        return [base]
    if ty.kind in ("array", "vector"):
        if ty.element.kind == "ptr":
            return [base + index * 8 for index in range(ty.count)]
        result: list[int] = []
        size, _ = size_align(ty.element, definitions)
        for index in range(ty.count):
            result.extend(_offsets_of(ty.element, definitions, base + index * size))
        return result
    if ty.kind == "struct":
        result = []
        for offset, _size, field in struct_field_layout(ty.fields, definitions):
            result.extend(_offsets_of(field, definitions, base + offset))
        return result
    if ty.kind == "named":
        definition = definitions.get(ty.name)
        if definition is None:
            return []
        return _offsets_of(definition, definitions, base)
    return []


def target_regions(state_ir: Path) -> list[dict[str, Any]]:
    source = Path(state_ir).read_text(encoding="utf-8", errors="replace")
    definitions = parse_named_definitions(source)
    regions = []
    for index, region in enumerate(state_table_globals(source)):
        name = region.name[1:].strip('"')
        try:
            size = size_align(parse_type(region.llvm_type), definitions)[0]
        except (AssertionError, ValueError):
            size = 0
        regions.append(
            {
                "index": index,
                "name": name,
                "source": "target",
                "size": size,
                "llvm_type": region.llvm_type,
                "pointer_offsets": _pointer_offsets(region.llvm_type, definitions),
            }
        )
    return regions


def state_layout_json(abi: TargetAbi, state_ir: Path) -> dict[str, Any]:
    regions = target_regions(state_ir)
    # Aggregate backing objects are separate C objects so native sanitizers
    # retain their true boundaries. Include them in the snapshot stream before
    # the synthetic state mirror so pointer relocations can name their region.
    backing_params: list[Param] = list(abi.params)
    if abi.kind == "FUNCTION":
        backing_params += [slot.param for slot in abi.call_slots
                           if slot.param.block_kind in ("VAR_IN_OUT", "VAR_OUTPUT")]
        backing_params += [param for param in abi.params
                           if param.block_kind == "VAR_INPUT" and param.constant]
    else:
        backing_params += [param for param in (abi.state_fields or []) if param not in abi.params]
    seen: set[int] = set()
    for param in backing_params:
        if (param.index is None or param.index in seen
                or (param.spec.kind == "scalar" and param.block_kind != "VAR_IN_OUT")):
            continue
        needs_backing = (
            param.block_kind == "VAR_IN_OUT"
            or param.spec.kind == "pointer"
            or (abi.kind == "FUNCTION" and param.block_kind == "VAR_OUTPUT")
            or (abi.kind == "FUNCTION" and param.block_kind == "VAR_INPUT" and param.constant)
        )
        if not needs_backing:
            continue
        seen.add(param.index)
        size = POINTER_STORAGE_CAPACITY if param.spec.kind == "pointer" else param.spec.byte_size()
        if param.spec.kind == "pointer":
            size = max(POINTER_STORAGE_CAPACITY, param.spec.pointee_bytes())
        regions.append({
            "index": len(regions),
            "name": f"semantist_f{param.index - (1 if abi.kind == 'FUNCTION_BLOCK' else 0)}_storage",
            "source": "harness_storage",
            "size": size,
            "llvm_type": None,
            "pointer_offsets": [],
        })
    harness = harness_state_layout(abi)
    harness_entry = {
        "index": len(regions),
        "name": harness["name"],
        "source": "harness",
        "size": harness["size"],
        "llvm_type": None,
        "pointer_offsets": harness["pointer_offsets"],
        "fields": harness["fields"],
    }
    regions.append(harness_entry)
    total = sum(region["size"] for region in regions)
    return {
        "schema_version": LAYOUT_SCHEMA,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "total_size": total,
        "harness_region_index": harness_entry["index"],
        "regions": regions,
    }


# ---------------------------------------------------------------------------
# execution-contract.json
# ---------------------------------------------------------------------------


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rusty_commit() -> str | None:
    directory = os.environ.get("SEMANTIST_RUSTY_SEMANTIC_DIR", "/opt/rusty-semantic")
    try:
        return subprocess.check_output(
            ["git", "-C", directory, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _llvm_version() -> str:
    binary = Path(os.environ.get("SEMANTIST_LLVM_BIN", "/usr/lib/llvm-21/bin")) / "llvm-config"
    try:
        text = subprocess.check_output([str(binary), "--version"], text=True, stderr=subprocess.DEVNULL)
        return text.strip().splitlines()[0].split()[-1]
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_contract(
    *,
    st_file: Path,
    function: str,
    abi: TargetAbi,
    abi_path: Path,
    state_layout_path: Path,
    target_binary: Path,
    compatibility_manifest: Path | None = None,
    scan_period_ns: int = 10_000_000,
    dt_range_ns: tuple[int, int] | None = None,
    timer_semantics: str = "iec_ge",
    loop_backedges_per_scan: int = 1_000_000,
    clock_origin_ns: int = 0,
    symbolic: str = "unsupported",
    symbolic_reason: str | None = "not built",
) -> dict[str, Any]:
    compatibility = {}
    if compatibility_manifest is not None:
        try:
            compatibility = json.loads(Path(compatibility_manifest).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            compatibility = {}
    stub_used = list(compatibility.get("stub_used") or []) if isinstance(compatibility, dict) else []
    selected_stubs = os.environ.get("SEMANTIST_LIBRARY_STUBS")
    if selected_stubs and selected_stubs not in stub_used:
        stub_used.append(selected_stubs)
    default_fidelity = "approximate" if compatibility_manifest is not None or stub_used else "semantic"
    semantic_fidelity = (compatibility.get("semantic_fidelity") or default_fidelity
                         if isinstance(compatibility, dict) else default_fidelity)
    if stub_used:
        semantic_fidelity = "approximate"
    support_status = (compatibility.get("support_status") or semantic_fidelity
                      if isinstance(compatibility, dict) else semantic_fidelity)
    if stub_used:
        support_status = "approximate"
    if symbolic != "supported" and support_status in {"semantic", "approximate"}:
        support_status = "native_only"
    symbolic_manifest = abi_path.parent / "klee" / "manifest.json"
    try:
        symbolic_model = json.loads(symbolic_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        symbolic_model = {}
    model_scope = {"symbolic_status": symbolic,
                   "symbolic_reason": symbolic_reason,
                   "compatibility": compatibility.get("model_scope", {})
                   if isinstance(compatibility, dict) else {},
                   "model_classes": symbolic_model.get("model_classes", {}),
                   "imprecise": symbolic_model.get("imprecise", [])}
    try:
        layout_document = json.loads(Path(state_layout_path).read_text(encoding="utf-8"))
        import_supported = not any(region.get("pointer_offsets")
                                   for region in layout_document.get("regions", []))
    except (OSError, UnicodeError, json.JSONDecodeError):
        import_supported = False
    # These digests are diagnostic provenance.  Replay and scheduling must not
    # use them as source/binary verification gates or seed/prefix identities.
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA,
        "contract_id": "",
        "support_status": support_status,
        "execution_status": {"build": "completed", "native": "ready", "symbolic": symbolic},
        "semantic_fidelity": semantic_fidelity,
        "stub_used": stub_used,
        "model_scope": model_scope,
        "target": {
            "st_file": str(st_file),
            "function": function,
            "kind": abi.kind,
            "source_sha256": sha256_file(st_file),
            "compatibility_manifest": str(compatibility_manifest) if compatibility_manifest else None,
            "compatibility_sha256": sha256_file(compatibility_manifest) if compatibility_manifest else None,
        },
        "toolchain": {
            "rusty_commit": _rusty_commit(),
            "rusty_patches": _patch_names(),
            "plc_sha256": _optional_sha(os.environ.get("RUSTY_COMPILER")),
            "stdlib_vclock_sha256": _optional_sha(os.environ.get("SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB")),
            "llvm": _llvm_version(),
            "compile_flags": os.environ.get("SEMANTIST_TARGET_SANITIZERS", "").split(),
        },
        "abi_ref": {"path": abi_path.name, "sha256": sha256_file(abi_path)},
        "inputs": {
            "frame_fields": [param.name for param in frame_params(abi)],
            "shared_fields": [param.name for param in shared_params(abi)],
            "fixed_fields": [],
        },
        "init": {"profiles": ["cold"], "default": "cold"},
        "time": {
            "scan_period_ns": scan_period_ns,
            "dt_range_ns": list(dt_range_ns) if dt_range_ns else None,
            "clock_origin_ns": clock_origin_ns,
            "read_semantics": "constant_within_scan",
            "timer_semantics": timer_semantics,
        },
        "numeric": {"integer_overflow": "wrap_explored_not_defect", "float_in_symbolic": "raw_abi_bytes"},
        "libraries": [
            {
                "name": "iec61131std",
                "implementation": "rust_stdlib_vclock",
                "symbolic_model": "compiler/symbolic/models/stdlib.c",
            }
        ],
        "oracles": [
            "array_access",
            "null_dereference",
            "division_by_zero",
            "conversion_out_of_range",
            "scan_budget",
            "crash",
        ],
        "scan_budget": {"loop_backedges_per_scan": loop_backedges_per_scan},
        "capabilities": {
            # In-process snapshots are always available.  Cross-process import
            # is advertised only for layouts whose state has no pointer bytes;
            # pointer-bearing layouts are handled by solver fallback replay.
            "snapshot": {"local": True, "cross_process_state_import": import_supported},
            "local_snapshot": True,
            "cross_process_state_import": import_supported,
            "symbolic": symbolic,
            "symbolic_reason": symbolic_reason,
            "target_binary_sha256": sha256_file(target_binary) if Path(target_binary).exists() else None,
            "state_layout": state_layout_path.name,
        },
    }
    payload = json.dumps({**contract, "contract_id": ""}, sort_keys=True, separators=(",", ":"))
    # Keep the historical digest-shaped field for report compatibility only;
    # consumers treat it as opaque provenance and do not compare it.
    contract["contract_id"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return contract


def _optional_sha(path: str | None) -> str | None:
    if not path or not Path(path).exists():
        return None
    return sha256_file(path)


def _patch_names() -> list[str]:
    names: list[str] = []
    for directory in (
        Path(os.environ.get("SEMANTIST_RUSTY_PATCH_DIR", "compiler/toolchain/rusty-patches")),
        Path("compiler/toolchain/stdlib-patches"),
    ):
        if directory.is_dir():
            names.extend(sorted(path.name for path in directory.glob("*.patch")))
    return names


def write_bundle_documents(
    out_dir: Path,
    abi: TargetAbi,
    state_ir: Path,
    **contract_args: Any,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    abi_path = out_dir / "abi.json"
    layout_path = out_dir / "state-layout.json"
    abi_path.write_text(json.dumps(abi_json(abi), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    layout = state_layout_json(abi, state_ir)
    layout_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    contract = build_contract(
        abi=abi,
        abi_path=abi_path,
        state_layout_path=layout_path,
        **contract_args,
    )
    contract_path = out_dir / "execution-contract.json"
    contract_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"abi": abi_path, "state_layout": layout_path, "contract": contract_path}
