"""Compile a selected POU from a one-time-normalized ST library project."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from compiler.stg.effects import resolve_effect_model
from compiler.toolchain.compatibility import load_project
from fuzzer.runtime.paths import build_artifact_dir


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLC = ROOT / "artifacts" / "rusty-semantic" / "target" / "release" / "plc"
DEFAULT_STDLIB_GLOB = str(
    ROOT / "artifacts" / "rusty-semantic" / "libs" / "stdlib" / "iec61131-st" / "*.st"
)
DEFAULT_LIBRARY = ROOT / "benchmarks" / "oscat_basic" / "source" / "oscat.st"
DEFAULT_STUBS = ROOT / "benchmarks" / "oscat_basic" / "source" / "stubs.st"
DEFAULT_LLVM_BIN = Path("/usr/lib/llvm-21/bin")


def target_name(function: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", function.lower()).strip("_") or "target"


def function_defined(paths: list[Path] | tuple[Path, ...], function: str) -> bool:
    pattern = re.compile(rf"(?im)^\s*(FUNCTION|FUNCTION_BLOCK|PROGRAM)\s+{re.escape(function)}\b")
    return any(
        path.is_file() and pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
        for path in paths
    )


def existing_path(value: str, description: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"[compile_st_project] missing {description}: {path}")
    return path


def build_config(
    *,
    st_file: Path,
    function: str,
    out_ll: Path,
    work_dir: Path,
    st_sources: list[Path],
    stdlib_glob: str,
) -> Path:
    files = [stdlib_glob, *(str(path) for path in st_sources)]
    if not function_defined(st_sources, function):
        files.append(str(st_file))
        print(f"[compile_st_project] appending target source for wrapper: {st_file}")
    else:
        print(f"[compile_st_project] using library definition for {function}; target source is harness-only")

    config = {
        "name": f"semantist_{target_name(function)}",
        "files": files,
        "compile_type": "IR",
        "output": out_ll.name,
        "libraries": [],
    }
    config_path = work_dir / "plc.json"
    config_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    return config_path


def write_call_effect_model(
    *, config_path: Path, stg_output: Path, function: str,
    project_root: Path, budget_s: float,
    executable: Path, env: dict[str, str] | None = None,
) -> dict:
    target_model = json.loads((stg_output / "stg-model.json").read_text(encoding="utf-8"))
    deadline = time.monotonic() + max(0.0, budget_s)
    with tempfile.TemporaryDirectory(prefix="effect-closure-", dir=stg_output) as temporary:
        effect_output = Path(temporary)

        def extract_effects(names: list[str]) -> dict:
            command = [str(executable), "--project-root", str(project_root),
                       "--output", str(effect_output), "--pretty", "false",
                       "--pou", function]
            for name in names:
                command.extend(["--pou", name])
            command.append(str(config_path))
            try:
                subprocess.run(command, cwd=project_root, env=env, check=True,
                               timeout=max(0.001, deadline - time.monotonic()),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(f"effect extraction {type(error).__name__}") from error
            return json.loads((effect_output / "stg-model.json").read_text(encoding="utf-8"))

        effect_model = resolve_effect_model(target_model, extract_effects,
                                            deadline=deadline)
    (stg_output / "stg-effect-model.json").write_text(
        json.dumps(effect_model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return effect_model


def compile_st_project_main() -> int:
    parser = argparse.ArgumentParser(description="Compile a POU from a normalized RuSTy ST library")
    parser.add_argument("st_file", help="Target ST file used for wrappers and harness signatures")
    parser.add_argument("function", help="Function under test")
    parser.add_argument("--out-ll", default=os.environ.get("OUT_LL", str(build_artifact_dir() / "ir" / "target.ll")))
    parser.add_argument("--plc", default=os.environ.get("RUSTY_COMPILER", str(DEFAULT_PLC)))
    parser.add_argument("--stdlib-glob", default=os.environ.get("SEMANTIST_RUSTY_STDLIB_GLOB", DEFAULT_STDLIB_GLOB))
    parser.add_argument(
        "--compatibility-manifest",
        default=os.environ.get("SEMANTIST_COMPATIBILITY_MANIFEST"),
        help="library compatibility manifest shared with native linking",
    )
    parser.add_argument(
        "--library",
        default=None,
        help="one-time-normalized ST library containing selectable POUs",
    )
    parser.add_argument(
        "--stubs",
        default=None,
        help="library-specific declarations/adapters/environment models",
    )
    parser.add_argument("--work-dir", default=os.environ.get("SEMANTIST_PROJECT_BUILD_DIR"))
    parser.add_argument(
        "--semantic-ir",
        action="store_true",
        default=os.environ.get("SEMANTIST_SEMANTIC_IR", "0") == "1",
        help="generate STG metadata and instrument compiler LLVM IR",
    )
    parser.add_argument("--stg-output", default=os.environ.get("SEMANTIST_STG_DIR"))
    parser.add_argument("--ir-mapping", default=os.environ.get("SEMANTIST_IR_MAPPING"))
    parser.add_argument("--ir-diagnostics", default=os.environ.get("SEMANTIST_IR_DIAGNOSTICS"))
    args = parser.parse_args()

    st_file = existing_path(args.st_file, "target ST file")
    plc = existing_path(args.plc, "RuSTy plc compiler")
    compatibility_manifest: Path | None = None
    dialect_compiler_args: list[str] = []
    if args.compatibility_manifest:
        compatibility_manifest = existing_path(args.compatibility_manifest, "compatibility manifest")
        try:
            project = load_project(compatibility_manifest)
        except ValueError as error:
            raise SystemExit(f"[compile_st_project] {error}") from error
        # A manifest supplies its dependency closure, but an explicit library
        # or stub is still part of this build. The same IR then feeds native
        # linking and KLEE, so there is no separate source-selection path.
        st_sources = list(project.st_sources)
        for value, label in ((args.library or os.environ.get("SEMANTIST_LIBRARY_FILE"), "compatible ST library"),
                             (args.stubs or os.environ.get("SEMANTIST_LIBRARY_STUBS"), "library stubs")):
            if value is not None:
                path = existing_path(value, label)
                if path not in st_sources:
                    st_sources.append(path)
        # Dialect switches such as --byte-pointer-arithmetic (CODESYS family).
        dialect_compiler_args = list(project.compiler_args)
        if project.stdlib_glob and args.stdlib_glob == DEFAULT_STDLIB_GLOB:
            args.stdlib_glob = project.stdlib_glob
    else:
        library = existing_path(args.library or os.environ.get("SEMANTIST_LIBRARY_FILE") or
                                os.environ.get("SEMANTIST_OSCAT_FILE") or DEFAULT_LIBRARY,
                                "compatible ST library")
        stubs = existing_path(args.stubs or os.environ.get("SEMANTIST_LIBRARY_STUBS") or
                              os.environ.get("SEMANTIST_OSCAT_STUBS") or DEFAULT_STUBS,
                              "library stubs")
        st_sources = [library, stubs]

    out_ll = Path(args.out_ll)
    if not out_ll.is_absolute():
        out_ll = ROOT / out_ll
    out_ll.parent.mkdir(parents=True, exist_ok=True)

    work_dir = (
        Path(args.work_dir)
        if args.work_dir
        else build_artifact_dir() / "st-projects" / target_name(args.function)
    )
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    config_path = build_config(
        st_file=st_file,
        function=args.function,
        out_ll=out_ll,
        work_dir=work_dir,
        st_sources=st_sources,
        stdlib_glob=args.stdlib_glob,
    )

    compiler_output = out_ll
    compiler_env = os.environ.copy()
    stg_output: Path | None = None
    ir_mapping: Path | None = None
    ir_diagnostics: Path | None = None
    emit_debug_sidecars = os.environ.get("SEMANTIST_STG_DEBUG_SIDECARS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if args.semantic_ir:
        stg_output = Path(args.stg_output) if args.stg_output else work_dir / "stg"
        if not stg_output.is_absolute():
            stg_output = ROOT / stg_output
        stg_output.mkdir(parents=True, exist_ok=True)
        stg_command = [
            str(ROOT / "compiler" / "scripts" / "generate_stg.sh"),
            "--project-root",
            str(ROOT),
            "--output",
            str(stg_output),
            "--pou",
            args.function,
        ]
        if os.environ.get("SEMANTIST_STG_PRETTY", "").lower() not in {"1", "true", "yes"}:
            stg_command.extend(["--pretty", "false"])
        if os.environ.get("SEMANTIST_STG_DOT", "").lower() in {"1", "true", "yes"}:
            stg_command.extend(["--dot", "true"])
        if emit_debug_sidecars:
            stg_command.extend(["--debug-sidecars", "true"])
        stg_command.append(str(config_path))
        print("+", " ".join(stg_command))
        subprocess.run(stg_command, cwd=ROOT, env=compiler_env, check=True)
        effect_budget_s = max(0.0, float(os.environ.get("SEMANTIST_EFFECT_CLOSURE_BUDGET_S", "10")))
        build_deadline_ns = os.environ.get("SEMANTIST_BUILD_DEADLINE_EPOCH_NS")
        if build_deadline_ns:
            remaining_build_s = (int(build_deadline_ns) - time.time_ns()) / 1_000_000_000
            effect_budget_s = min(effect_budget_s, max(0.0, remaining_build_s - 45.0))
        write_call_effect_model(
            config_path=config_path, stg_output=stg_output, function=args.function,
            project_root=ROOT, budget_s=effect_budget_s,
            executable=ROOT / "compiler" / "scripts" / "generate_stg.sh",
            env=compiler_env,
        )
        codegen_map = stg_output / "stg-codegen-map.json"
        runtime_ids = stg_output / "stg-runtime-ids.json"
        if not codegen_map.exists() or not runtime_ids.exists():
            raise SystemExit("[compile_st_project] STG did not produce instrumentation sidecars")
        compiler_env["SEMANTIST_STG_CODEGEN_MAP"] = str(codegen_map)
        compiler_output = work_dir / f"{out_ll.stem}.compiler.ll"
        ir_mapping = (
            Path(args.ir_mapping)
            if args.ir_mapping
            else stg_output / "stg-ir-mapping.json"
        )
        if args.ir_diagnostics:
            ir_diagnostics = Path(args.ir_diagnostics)
        elif emit_debug_sidecars:
            ir_diagnostics = stg_output / "stg-ir-diagnostics.json"
        if not ir_mapping.is_absolute():
            ir_mapping = ROOT / ir_mapping
        if ir_diagnostics is not None and not ir_diagnostics.is_absolute():
            ir_diagnostics = ROOT / ir_diagnostics

    command = [
        str(plc),
        "build",
        str(config_path),
        "--ir",
        "--single-module",
        "--error-format",
        "none",
        "--build-location",
        str(work_dir / "build"),
        "-o",
        str(compiler_output),
        *dialect_compiler_args,
    ]
    print("[compile_st_project] using compiler:", plc)
    print("[compile_st_project] stdlib:", args.stdlib_glob)
    if compatibility_manifest:
        print("[compile_st_project] compatibility manifest:", compatibility_manifest)
    print("[compile_st_project] ST sources:", ", ".join(str(path) for path in st_sources))
    print("[compile_st_project] plc.json:", config_path)
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, env=compiler_env, check=True)
    if not compiler_output.exists():
        raise SystemExit(f"[compile_st_project] compiler did not produce {compiler_output}")

    if args.semantic_ir:
        assert stg_output is not None
        assert ir_mapping is not None
        instrument_command = [
            str(ROOT / "compiler" / "scripts" / "instrument_stg_ir.sh"),
            str(compiler_output),
            "--output",
            str(out_ll),
            "--runtime-ids",
            str(stg_output / "stg-runtime-ids.json"),
            "--mapping",
            str(ir_mapping),
        ]
        if ir_diagnostics is not None:
            instrument_command.extend(["--diagnostics", str(ir_diagnostics)])
        print("+", " ".join(instrument_command))
        subprocess.run(instrument_command, cwd=ROOT, env=compiler_env, check=True)
        if emit_debug_sidecars:
            build_manifest = {
                "schema_version": "semantist.semantic-ir-build/1.0.0",
                "mode": "semantic-ir",
                "compiler": str(plc),
                "project": str(config_path),
                "compiler_ir": str(compiler_output),
                "instrumented_ir": str(out_ll),
                "stg": str(stg_output / "stg-model.json"),
                "codegen_map": str(stg_output / "stg-codegen-map.json"),
                "runtime_ids": str(stg_output / "stg-runtime-ids.json"),
                "ir_mapping": str(ir_mapping),
                "ir_diagnostics": str(ir_diagnostics) if ir_diagnostics else None,
            }
            (stg_output / "semantic-ir-build.json").write_text(
                json.dumps(build_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    elif compiler_output != out_ll:
        shutil.copyfile(compiler_output, out_ll)

    if not out_ll.exists():
        raise SystemExit(f"[compile_st_project] compiler did not produce {out_ll}")
    print(f"[compile_st_project] wrote {out_ll}")
    return 0




# LLVM IR preparation helpers from compiler/scripts/prepare_target_ir.py.
def ir_tool(bin_dir: Path, name: str) -> str:
    candidate = bin_dir / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise SystemExit(f"[prepare_target_ir] missing LLVM tool: {name}")


def make_llvm16_compatible(text: str) -> str:
    text = text.replace("getelementptr inbounds nuw", "getelementptr inbounds")
    text = re.sub(r"\s+captures\(none\)", "", text)
    return text


def pad_rusty_string_storage(text: str) -> str:
    """Keep RuSTy's fixed-width string reads inside short LLVM storage."""
    capacity = 256
    literal = re.compile(
        r'(?m)^(@utf08_literal_[\w.]+\s*=\s*[^\n]*?constant )\[(\d+) x i8\]( c"([^"\n]*)")'
    )

    def expand_literal(match: re.Match[str]) -> str:
        size = int(match.group(2))
        if size >= capacity:
            return match.group(0)
        return (f'{match.group(1)}[{capacity} x i8] c"'
                f'{match.group(4)}' + r'\00' * (capacity - size) + '"')

    text = literal.sub(expand_literal, text)
    alloca = re.compile(r'(?m)^(\s*)(%[-\w.$]+) = alloca \[(\d+) x i8\](, align \d+)$')
    fixed_width_call = re.compile(r'call [^\n]*@STRING_TO_[A-Z0-9_]+\([^\n]*ptr (%[-\w.$]+)')

    def expand_function(match: re.Match[str]) -> str:
        body = match.group(0)
        operands = set(fixed_width_call.findall(body))
        padded: dict[str, int] = {}

        def expand_alloca(entry: re.Match[str]) -> str:
            name, size = entry.group(2), int(entry.group(3))
            if name not in operands or size >= capacity:
                return entry.group(0)
            padded[name] = size
            return f'{entry.group(1)}{name} = alloca [{capacity} x i8]{entry.group(4)}'

        body = alloca.sub(expand_alloca, body)
        for name, size in padded.items():
            body = re.sub(
                rf'(@llvm\.memset\.p0\.i64\(ptr align \d+ {re.escape(name)}, i8 0, i64 ){size}(,)',
                rf'\g<1>{capacity}\2', body,
            )
        return body

    return re.sub(r'(?ms)^define [^\n]+\{.*?^\}', expand_function, text)


def public_api_list(source: str, function: str) -> str:
    symbols = [function]
    ctor = f"{function}__ctor"
    if re.search(rf"(?m)^define\s+.*@{re.escape(ctor)}\(", source):
        symbols.append(ctor)
    return ",".join(symbols)


def run_llvm_bounds_sanitizer(opt: str, input_ll: Path, output_ll: Path) -> None:
    """Apply LLVM's standard local bounds-checking sanitizer to prepared IR."""

    command = [
        opt,
        "-S",
        "-passes=function(bounds-checking<rt-abort>)",
        str(input_ll),
        "-o",
        str(output_ll),
    ]
    print("+", " ".join(command))
    subprocess.run(command, check=True)
    if not output_ll.exists():
        raise SystemExit(f"[prepare_target_ir] LLVM bounds sanitizer did not produce {output_ll}")


def address_sanitizer_enabled(flags: str) -> bool:
    """Follow the ordered sanitizer flags passed to the target's clang command."""
    enabled = False
    for flag in flags.split():
        if flag.startswith("-fsanitize="):
            if "address" in flag.partition("=")[2].split(","):
                enabled = True
        elif flag.startswith("-fno-sanitize="):
            if {"address", "all"}.intersection(flag.partition("=")[2].split(",")):
                enabled = False
    return enabled


def validate_address_sanitizer_attributes(source: str) -> int:
    """Check opt's canonical text output; ASan runtime symbols are not evidence."""
    groups = dict(re.findall(r"(?m)^attributes #(\d+) = \{([^\n]*)\}", source))
    definitions = re.findall(r"(?m)^define\s+[^\n]+", source)
    missing = []
    for header in definitions:
        # opt prints function attributes in numbered groups on individual lines.
        # Quoted string attributes (and nosanitize_address metadata) must not be
        # mistaken for the enum attribute required by the ASan function pass.
        unquoted_header = re.sub(r'"(?:\\.|[^"\\])*"', "", header)
        attributes = " ".join(
            groups.get(group, "") for group in re.findall(r"#(\d+)\b", unquoted_header)
        )
        attributes = re.sub(r'"(?:\\.|[^"\\])*"', "", attributes)
        tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z_0-9]*\b", attributes))
        if "sanitize_address" not in tokens or "disable_sanitizer_instrumentation" in tokens:
            missing.append(header)
    if not definitions or missing:
        detail = "; ".join(missing[:3]) if missing else "no function definitions"
        raise SystemExit(f"[prepare_target_ir] incomplete ASan function attributes: {detail}")
    return len(definitions)


def prepare_address_sanitizer(opt: str, input_ll: Path, output_ll: Path) -> None:
    """Mark every retained ST function before the downstream ASan compilation.

    clang -fsanitize=address does not add this attribute to existing LLVM IR.
    Only add attributes here: each baseline's clang must run its own ASan pass
    so shared IR does not acquire LLVM-version-specific sanitizer runtime calls.
    """
    command = [
        opt,
        "-S",
        "-passes=forceattrs",
        "-force-attribute=sanitize_address",
        str(input_ll),
        "-o",
        str(output_ll),
    ]
    print("+", " ".join(command))
    subprocess.run(command, check=True)
    count = validate_address_sanitizer_attributes(output_ll.read_text(encoding="utf-8"))
    print(f"[prepare_target_ir] ASan enabled for {count} retained function definitions")


INTEGER_TYPE_RE = re.compile(r"^i([1-9][0-9]*)$")
ARRAY_TYPE_RE = re.compile(r"^\[([0-9]+) x (.+)\]$")
ALLOCA_RE = re.compile(r"^\s*(%[-A-Za-z0-9_.$]+)\s*=\s*alloca\s+(.+?)(?:,\s|$)")
MEMMOVE_CALL_RE = re.compile(
    r"call\s+ptr\s+@(?:SysMemMove|MemMove)\(ptr\s+([^,]+),\s*ptr\s+([^,]+),\s*i32\s+([^)]+)\)"
)
MEMCPY_CALL_RE = re.compile(
    r"call\s+ptr\s+@(?:SysMemCpy|MemCpy)\(ptr\s+([^,]+),\s*ptr\s+([^,]+),\s*i32\s+([^)]+)\)"
)
MEMSET_CALL_RE = re.compile(
    r"call\s+ptr\s+@(?:SysMemSet|MemSet)\(ptr\s+([^,]+),\s*i32\s+([^,]+),\s*i32\s+([^)]+)\)"
)
GEP_FIELD_RE = re.compile(
    r"^\s*(%[-A-Za-z0-9_.$]+)\s*=\s*getelementptr(?:\s+inbounds)?\s+"
    r"(%[-A-Za-z0-9_.$]+|\[[^\n]+\]),\s*ptr\s+[^,]+,\s*i\d+\s+0,\s*i\d+\s+(\d+)"
)
STRUCT_TYPE_RE = re.compile(r"^\s*(%[-A-Za-z0-9_.$]+)\s*=\s*type\s+\{(.*)\}\s*$")


def split_llvm_types(value: str) -> list[str]:
    """Split a literal LLVM aggregate type without treating nested commas as separators."""

    fields: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character in "[{<":
            depth += 1
        elif character in "]}>":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            fields.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        fields.append(tail)
    return fields


def llvm_type_size(llvm_type: str, named_types: dict[str, list[str]] | None = None) -> int | None:
    llvm_type = llvm_type.strip()
    integer = INTEGER_TYPE_RE.match(llvm_type)
    if integer:
        return (int(integer.group(1)) + 7) // 8
    if llvm_type == "half":
        return 2
    if llvm_type == "float":
        return 4
    if llvm_type == "double":
        return 8
    if llvm_type == "ptr":
        return 8
    array = ARRAY_TYPE_RE.match(llvm_type)
    if array:
        element_size = llvm_type_size(array.group(2), named_types)
        if element_size is None:
            return None
        return int(array.group(1)) * element_size
    return None


def memory_object_size(
    value: str,
    alloca_sizes: dict[str, int],
    pointer_sizes: dict[str, int] | None = None,
) -> int | None:
    value = value.strip()
    if pointer_sizes and value in pointer_sizes:
        return pointer_sizes[value]
    if value in alloca_sizes:
        return alloca_sizes[value]
    gep_base = re.search(r"getelementptr\s+.*?,\s*ptr\s+(%[-A-Za-z0-9_.$]+),", value)
    if gep_base:
        if pointer_sizes and gep_base.group(1) in pointer_sizes:
            return pointer_sizes[gep_base.group(1)]
        return alloca_sizes.get(gep_base.group(1))
    return None


def size_literal(size: int | None) -> str:
    return str(size) if size is not None else "-1"


def add_memory_checked_declarations(source: str, declarations: set[str]) -> str:
    if not declarations:
        return source
    declare_lines = {
        "memcpy": "declare ptr @__SEMANTIST_MEMCPY_CHECKED(ptr, i64, ptr, i64, i32)",
        "memmove": "declare ptr @__SEMANTIST_MEMMOVE_CHECKED(ptr, i64, ptr, i64, i32)",
        "memset": "declare ptr @__SEMANTIST_MEMSET_CHECKED(ptr, i64, i32, i32)",
    }
    to_add = [
        declaration
        for key, declaration in sorted(declare_lines.items())
        if key in declarations and declaration not in source
    ]
    if not to_add:
        return source
    return source.rstrip() + "\n\n" + "\n".join(to_add) + "\n"


def instrument_memory_primitive_bounds(input_ll: Path, output_ll: Path) -> bool:
    """Route known CODESYS memory primitive calls through checked runtime shims."""

    lines = input_ll.read_text(encoding="utf-8", errors="replace").splitlines()
    transformed: list[str] = []
    alloca_sizes: dict[str, int] = {}
    pointer_sizes: dict[str, int] = {}
    named_types = {
        match.group(1): split_llvm_types(match.group(2))
        for line in lines
        if (match := STRUCT_TYPE_RE.match(line))
    }
    in_function = False
    changed = False
    declarations: set[str] = set()

    for line in lines:
        if line.startswith("define "):
            in_function = True
            alloca_sizes = {}
            pointer_sizes = {}
        elif in_function and line.startswith("}"):
            in_function = False
            alloca_sizes = {}
            pointer_sizes = {}

        if in_function:
            alloca = ALLOCA_RE.match(line)
            if alloca:
                size = llvm_type_size(alloca.group(2), named_types)
                if size is not None:
                    alloca_sizes[alloca.group(1)] = size
            gep = GEP_FIELD_RE.match(line)
            if gep:
                source_type, field_index = gep.group(2), int(gep.group(3))
                fields = named_types.get(source_type)
                if fields is not None and field_index < len(fields):
                    size = llvm_type_size(fields[field_index], named_types)
                    if size is not None:
                        pointer_sizes[gep.group(1)] = size
                else:
                    array = ARRAY_TYPE_RE.match(source_type)
                    if array:
                        size = llvm_type_size(array.group(2), named_types)
                        if size is not None:
                            pointer_sizes[gep.group(1)] = size

            def replace_memmove(match: re.Match[str]) -> str:
                nonlocal changed
                dest, source, count = (part.strip() for part in match.groups())
                changed = True
                declarations.add("memmove")
                return (
                    f"call ptr @__SEMANTIST_MEMMOVE_CHECKED(ptr {dest}, "
                    f"i64 {size_literal(memory_object_size(dest, alloca_sizes, pointer_sizes))}, "
                    f"ptr {source}, i64 {size_literal(memory_object_size(source, alloca_sizes, pointer_sizes))}, "
                    f"i32 {count})"
                )

            def replace_memcpy(match: re.Match[str]) -> str:
                nonlocal changed
                dest, source, count = (part.strip() for part in match.groups())
                changed = True
                declarations.add("memcpy")
                return (
                    f"call ptr @__SEMANTIST_MEMCPY_CHECKED(ptr {dest}, "
                    f"i64 {size_literal(memory_object_size(dest, alloca_sizes, pointer_sizes))}, "
                    f"ptr {source}, i64 {size_literal(memory_object_size(source, alloca_sizes, pointer_sizes))}, "
                    f"i32 {count})"
                )

            def replace_memset(match: re.Match[str]) -> str:
                nonlocal changed
                dest, value, count = (part.strip() for part in match.groups())
                changed = True
                declarations.add("memset")
                return (
                    f"call ptr @__SEMANTIST_MEMSET_CHECKED(ptr {dest}, "
                    f"i64 {size_literal(memory_object_size(dest, alloca_sizes, pointer_sizes))}, "
                    f"i32 {value}, i32 {count})"
                )

            line = MEMMOVE_CALL_RE.sub(replace_memmove, line)
            line = MEMCPY_CALL_RE.sub(replace_memcpy, line)
            line = MEMSET_CALL_RE.sub(replace_memset, line)

        transformed.append(line)

    if not changed:
        return False
    output_ll.write_text(
        add_memory_checked_declarations("\n".join(transformed) + "\n", declarations),
        encoding="utf-8",
    )
    return True


def prepare_target_ir_main() -> int:
    parser = argparse.ArgumentParser(description="Trim project IR to a single public entry point")
    parser.add_argument("input_ll")
    parser.add_argument("function")
    parser.add_argument("--output-ll", required=True)
    parser.add_argument("--llvm-bin", default=os.environ.get("SEMANTIST_LLVM_BIN", str(DEFAULT_LLVM_BIN)))
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args()

    input_ll = Path(args.input_ll).resolve()
    output_ll = Path(args.output_ll).resolve()
    bin_dir = Path(args.llvm_bin).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else output_ll.parent / ".ir-prep"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_ll.parent.mkdir(parents=True, exist_ok=True)

    compat_ll = work_dir / f"{output_ll.stem}.compat.ll"
    source = input_ll.read_text(encoding="utf-8", errors="replace")
    source = make_llvm16_compatible(source)
    compat_ll.write_text(source, encoding="utf-8")
    api_list = public_api_list(source, args.function)

    opt = ir_tool(bin_dir, "opt")
    command = [
        opt,
        "-S",
        "-passes=internalize,globaldce",
        f"--internalize-public-api-list={api_list}",
        str(compat_ll),
        "-o",
        str(output_ll),
    ]
    print("+", " ".join(command))
    subprocess.run(command, check=True)
    if not output_ll.exists():
        raise SystemExit(f"[prepare_target_ir] opt did not produce {output_ll}")
    if os.environ.get("SEMANTIST_ARRAY_BOUNDS_SANITIZER", "1").lower() not in {
        "0",
        "false",
        "no",
    }:
        bounds_ll = work_dir / f"{output_ll.stem}.bounds.ll"
        run_llvm_bounds_sanitizer(opt, output_ll, bounds_ll)
        shutil.move(bounds_ll, output_ll)
    if os.environ.get("SEMANTIST_MEMORY_BOUNDS_CHECKS", "1").lower() not in {
        "0",
        "false",
        "no",
    }:
        checked_ll = work_dir / f"{output_ll.stem}.memory-bounds.ll"
        if instrument_memory_primitive_bounds(output_ll, checked_ll):
            shutil.move(checked_ll, output_ll)
            print("[prepare_target_ir] instrumented CODESYS memory primitive bounds")
    padded_source = pad_rusty_string_storage(output_ll.read_text(encoding="utf-8"))
    output_ll.write_text(padded_source, encoding="utf-8")
    # Match build_target.sh's default (including an explicitly empty variable).
    # This is independent of semantic instrumentation: semantic-off targets
    # need the same oracle.
    sanitizer_flags = os.environ.get("SEMANTIST_TARGET_SANITIZERS") or (
        "-fsanitize=address,undefined -fno-omit-frame-pointer"
    )
    if address_sanitizer_enabled(sanitizer_flags):
        asan_ll = work_dir / f"{output_ll.stem}.asan.ll"
        prepare_address_sanitizer(opt, output_ll, asan_ll)
        shutil.move(asan_ll, output_ll)
    # LLVM 21 optimization passes can reintroduce attributes that LLVM 16
    # cannot parse. Keep the prepared IR readable by LLVM 16 tools by
    # normalizing the final output as well as the input to opt.
    final_source = output_ll.read_text(encoding="utf-8", errors="replace")
    compatible_source = make_llvm16_compatible(final_source)
    if compatible_source != final_source:
        output_ll.write_text(compatible_source, encoding="utf-8")
    print(f"[prepare_target_ir] wrote {output_ll}")
    return 0




def llvm_bin_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    return Path(env.get("SEMANTIST_LLVM_BIN") or DEFAULT_LLVM_BIN)


def with_llvm_toolchain_env(env: dict[str, str] | None = None) -> dict[str, str]:
    out = dict(env or os.environ)
    bin_dir = llvm_bin_dir(out)
    out["SEMANTIST_LLVM_BIN"] = str(bin_dir)
    out["PATH"] = f"{bin_dir}{os.pathsep}{out.get('PATH', '')}" if out.get("PATH") else str(bin_dir)
    return out



def main() -> int:
    return compile_st_project_main()
