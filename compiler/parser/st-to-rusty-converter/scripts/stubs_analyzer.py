#!/usr/bin/env python3
"""Build an auditable dependency report from RuSTy and linker diagnostics.

This tool deliberately does not generate executable stubs.  It inventories
symbols in resolution order and leaves unknown semantics unresolved.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compiler.toolchain.compatibility import load_project


# These are compiler language operations, not IEC standard-library functions.
# In particular DATE_TO_DWORD is intentionally absent: pinned RuSTy does not
# provide it as a compiler intrinsic or as an IEC ST standard-library function.
COMPILER_INTRINSICS = {"REF", "SIZEOF"}

POU_RE = re.compile(
    r"(?im)^\s*(?:@EXTERNAL\s+)?(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
TYPE_RE = re.compile(r"(?im)^\s*TYPE\s+([A-Za-z_][A-Za-z0-9_]*)\b")
EXTERNAL_RE = re.compile(
    r"(?im)^\s*@EXTERNAL\s+(?:FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def _st_files(path: Path | Iterable[Path] | None) -> list[Path]:
    if path is None:
        return []
    paths = [path] if isinstance(path, Path) else list(path)
    result: list[Path] = []
    for item in paths:
        if not item.exists():
            continue
        if item.is_file() and item.suffix.lower() == ".st":
            result.append(item)
        elif item.is_dir():
            result.extend(item.rglob("*.st"))
    return sorted(set(result))


def st_symbols(path: Path | Iterable[Path] | None) -> set[str]:
    symbols: set[str] = set()
    for source in _st_files(path):
        text = source.read_text(encoding="utf-8", errors="replace")
        symbols.update(match.group(2).upper() for match in POU_RE.finditer(text))
        symbols.update(match.group(1).upper() for match in TYPE_RE.finditer(text))
    return symbols


def external_symbols(path: Path | Iterable[Path] | None) -> set[str]:
    symbols: set[str] = set()
    for source in _st_files(path):
        text = source.read_text(encoding="utf-8", errors="replace")
        symbols.update(match.group(1).upper() for match in EXTERNAL_RE.finditer(text))
    return symbols


def archive_symbols(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    result = subprocess.run(
        ["nm", "-g", "--defined-only", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    symbols: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-2] in {"T", "D", "B", "R", "W", "V"}:
            symbols.add(fields[-1].upper())
    return symbols


@dataclass
class Inventory:
    compiler_intrinsics: set[str] = field(default_factory=lambda: set(COMPILER_INTRINSICS))
    rusty_stdlib_st: set[str] = field(default_factory=set)
    runtime_archive: set[str] = field(default_factory=set)
    library: set[str] = field(default_factory=set)
    native_compatibility: set[str] = field(default_factory=set)
    declarations: set[str] = field(default_factory=set)
    semantic_adapters: set[str] = field(default_factory=set)
    environment_models: set[str] = field(default_factory=set)

    @classmethod
    def load(
        cls,
        stdlib: Path | None,
        runtime_archive: Path | None,
        library: Path | Iterable[Path] | None,
        stubs: Path | Iterable[Path] | None,
        environment_models: Iterable[str] = (),
        native_providers: Iterable[Path] = (),
    ) -> "Inventory":
        stub_symbols = st_symbols(stubs)
        declarations = external_symbols(stubs)
        models = {name.upper() for name in environment_models}
        return cls(
            rusty_stdlib_st=st_symbols(stdlib),
            runtime_archive=archive_symbols(runtime_archive),
            library=st_symbols(library),
            native_compatibility=set().union(
                *(archive_symbols(path) for path in native_providers)
            ),
            declarations=declarations,
            semantic_adapters=stub_symbols - declarations - models,
            environment_models=stub_symbols & models,
        )

    def resolve(self, name: str) -> tuple[str, str]:
        """Return source and dependency classification in mandated order."""
        key = name.upper()
        if key in self.compiler_intrinsics:
            return "compiler_intrinsic", "provided"
        if key in self.rusty_stdlib_st:
            return "rusty_iec_st_library", "provided"
        if key in self.runtime_archive:
            return "libiec61131std.a", "provided"
        if key in self.library:
            return "input_library", "provided"
        if key in self.native_compatibility:
            return "compatibility_runtime", "provided"
        if key in self.environment_models:
            return "stubs.st", "environment_model"
        if key in self.semantic_adapters:
            return "stubs.st", "semantic_adapter"
        if key in self.declarations:
            return "stubs.st", "declaration"
        return "none", "unresolved"

    def counts(self) -> dict[str, int]:
        return {
            "compiler_intrinsics": len(self.compiler_intrinsics),
            "rusty_stdlib_st": len(self.rusty_stdlib_st),
            "runtime_archive": len(self.runtime_archive),
            "library": len(self.library),
            "native_compatibility": len(self.native_compatibility),
            "declarations": len(self.declarations),
            "semantic_adapters": len(self.semantic_adapters),
            "environment_models": len(self.environment_models),
        }

    def conflicts(self) -> dict[str, list[str]]:
        stub_symbols = self.declarations | self.semantic_adapters | self.environment_models
        return {
            "compiler_intrinsic": sorted(stub_symbols & self.compiler_intrinsics),
            "rusty_iec_st_library": sorted(stub_symbols & self.rusty_stdlib_st),
            "libiec61131std.a": sorted(stub_symbols & self.runtime_archive),
            "input_library": sorted(stub_symbols & self.library),
        }


@dataclass
class Dependency:
    name: str
    diagnostic: str
    symbol_kind: str
    source_file: str = ""
    line_number: int = 0
    context: str = ""
    source: str = "none"
    classification: str = "unresolved"
    assumptions: list[str] = field(default_factory=list)
    impact: str = ""


class StubsAnalyzer:
    E048 = re.compile(r"error\[E048\].*?Could not resolve reference to ([A-Za-z_][A-Za-z0-9_]*)")
    E052 = re.compile(r"error\[E052\].*?Unknown type:\s*([A-Za-z_][A-Za-z0-9_]*)")
    LOCATION = re.compile(r"[┌╭]-?\s*([^\s:]+):(\d+):\d+")
    CONTEXT = re.compile(r"│\s*(.+)")
    GNU_UNDEFINED = re.compile(r"undefined reference to [`'‘]([^'’`]+)")
    LLD_UNDEFINED = re.compile(r"undefined symbol:\s*([^\s]+)")

    def __init__(self, inventory: Inventory | None = None) -> None:
        self.inventory = inventory or Inventory()
        self.dependencies: dict[tuple[str, str], Dependency] = {}

    def _add(
        self,
        name: str,
        diagnostic: str,
        symbol_kind: str,
        source_file: str = "",
        line_number: int = 0,
        context: str = "",
    ) -> None:
        # Linkers may print foo@@VERSION or a trailing punctuation mark.
        name = name.split("@@", 1)[0].strip("'`:, ")
        source, classification = self.inventory.resolve(name)
        if diagnostic == "linker_undefined" and classification == "declaration":
            source, classification = "unprovided_declaration", "unresolved"
        key = (diagnostic, name.upper())
        self.dependencies.setdefault(
            key,
            Dependency(
                name=name,
                diagnostic=diagnostic,
                symbol_kind=symbol_kind,
                source_file=source_file,
                line_number=line_number,
                context=context[:1000],
                source=source,
                classification=classification,
            ),
        )

    @staticmethod
    def _kind(name: str, context: str, error: str) -> str:
        if error == "E052":
            return "type"
        if re.search(rf"\b{re.escape(name)}\s*\(", context, re.IGNORECASE):
            return "function"
        return "symbol"

    def parse(self, content: str) -> None:
        lines = content.splitlines()
        for index, line in enumerate(lines):
            matched = self.E048.search(line) or self.E052.search(line)
            if matched:
                diagnostic = "E048" if "E048" in line else "E052"
                nearby = lines[index + 1 : index + 9]
                location = next((self.LOCATION.search(item) for item in nearby if self.LOCATION.search(item)), None)
                context = "\n".join(
                    match.group(1).strip()
                    for item in nearby
                    if (match := self.CONTEXT.search(item))
                )
                self._add(
                    matched.group(1),
                    diagnostic,
                    self._kind(matched.group(1), context, diagnostic),
                    location.group(1) if location else "",
                    int(location.group(2)) if location else 0,
                    context,
                )

            for pattern in (self.GNU_UNDEFINED, self.LLD_UNDEFINED):
                link_match = pattern.search(line)
                if link_match:
                    self._add(link_match.group(1), "linker_undefined", "runtime_symbol", context=line)

    def to_dict(self) -> dict[str, Any]:
        dependencies = sorted(
            (asdict(item) for item in self.dependencies.values()),
            key=lambda item: (item["classification"], item["name"].upper()),
        )
        grouped = {
            "declaration": sorted(self.inventory.declarations),
            "semantic_adapter": sorted(self.inventory.semantic_adapters),
            "environment_model": sorted(self.inventory.environment_models),
            "unresolved": [],
        }
        for item in dependencies:
            if item["classification"] in grouped and item["name"] not in grouped[item["classification"]]:
                grouped[item["classification"]].append(item["name"])
        conflicts = self.inventory.conflicts()
        has_conflicts = any(conflicts.values())
        return {
            "schema_version": "semantist.dependency-report/1.0.0",
            "resolution_order": [
                "compiler_intrinsic",
                "rusty_iec_st_library",
                "libiec61131std.a",
                "input_library",
                "compatibility_runtime",
                "stubs.st",
            ],
            "inventory": self.inventory.counts(),
            "conflicting_stub_symbols": conflicts,
            "dependencies": dependencies,
            "classifications": grouped,
            "gate": {
                "status": "blocked" if grouped["unresolved"] or has_conflicts else "ready_for_link_validation",
                "unresolved_count": len(grouped["unresolved"]),
                "conflict_count": sum(len(items) for items in conflicts.values()),
                "note": "IR generation alone is not executable validation",
            },
        }


def read_inputs(paths: Iterable[str]) -> str:
    chunks: list[str] = []
    for value in paths:
        if value == "-":
            chunks.append(sys.stdin.read())
        else:
            chunks.append(Path(value).read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="+", help="RuSTy/linker log files; use - for stdin")
    parser.add_argument("--output", "-o", default="missing-deps/dependency-report.json")
    parser.add_argument("--stdlib", type=Path)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--compatibility-manifest", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--stubs", type=Path)
    parser.add_argument(
        "--environment-model",
        action="append",
        default=[],
        help="name a deterministic executable stubs.st function as an environment_model",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.compatibility_manifest:
        try:
            project = load_project(args.compatibility_manifest)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        library = project.library_sources
        stubs = project.compatibility_sources
        runtime_archive = args.runtime_archive or project.runtime_archive
        native_providers = (*project.native_objects, *project.native_libraries)
        stdlib = args.stdlib
        if stdlib is None and project.stdlib_glob:
            stdlib = Path(project.stdlib_glob.removesuffix("/*.st"))
    else:
        library = args.library
        stubs = args.stubs
        runtime_archive = args.runtime_archive
        stdlib = args.stdlib
        native_providers = ()

    inventory = Inventory.load(
        stdlib,
        runtime_archive,
        library,
        stubs,
        args.environment_model,
        native_providers,
    )
    analyzer = StubsAnalyzer(inventory)
    analyzer.parse(read_inputs(args.input))
    report = analyzer.to_dict()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.verbose:
        for item in report["dependencies"]:
            print(f"{item['name']}: {item['diagnostic']} -> {item['source']} ({item['classification']})")
    print(f"dependency report: {output} ({report['gate']['status']})")
    return 2 if report["gate"]["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
