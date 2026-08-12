"""Load the compatibility manifest shared by intake, compilation, and fuzzing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "semantist.compatibility/1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _paths(root: Path, values: Iterable[str], field: str) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise ValueError(f"compatibility manifest {field} does not exist: {path}")
        result.append(path)
    return tuple(result)


def _glob_static_prefix(value: str) -> str:
    wildcard = min((index for index in (value.find("*"), value.find("?")) if index >= 0), default=-1)
    if wildcard < 0:
        return value
    return value[:wildcard]


def _toolchain_glob(root: Path, value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)

    prefix = Path(_glob_static_prefix(value))
    root_candidate = root / path
    project_candidate = PROJECT_ROOT / path
    if (root / prefix).exists():
        return str(root_candidate.resolve())
    if (PROJECT_ROOT / prefix).exists():
        return str(project_candidate.resolve())
    return str(root_candidate.resolve())


@dataclass(frozen=True)
class CompatibilityProject:
    manifest: Path
    name: str
    dialect: str
    library_sources: tuple[Path, ...]
    compatibility_sources: tuple[Path, ...]
    st_sources: tuple[Path, ...]
    native_sources: tuple[Path, ...]
    native_objects: tuple[Path, ...]
    native_libraries: tuple[Path, ...]
    native_compile_args: tuple[str, ...]
    link_args: tuple[str, ...]
    profiles: tuple[str, ...]
    stdlib_glob: str | None
    runtime_archive: Path | None

    @property
    def root(self) -> Path:
        return self.manifest.parent


def load_project(path: str | Path) -> CompatibilityProject:
    manifest = Path(path).expanduser().resolve()
    if not manifest.is_file():
        raise ValueError(f"compatibility manifest does not exist: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported compatibility schema {data.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )

    root = manifest.parent
    library = data.get("library", {})
    native = data.get("native", {})
    toolchain = data.get("toolchain", {})
    library_sources = _paths(
        root,
        library.get("normalized_sources", library.get("sources", [])),
        "library.normalized_sources",
    )
    compatibility_sources = _paths(
        root,
        library.get("compatibility_sources", []),
        "library.compatibility_sources",
    )
    st_sources = tuple(dict.fromkeys((*library_sources, *compatibility_sources)))
    if not library_sources:
        raise ValueError("compatibility manifest must contain at least one normalized ST source")

    runtime_value = toolchain.get("runtime_archive")
    runtime_archive = None
    if runtime_value:
        runtime_archive = _paths(root, [runtime_value], "toolchain.runtime_archive")[0]

    return CompatibilityProject(
        manifest=manifest,
        name=str(data.get("name") or manifest.parent.name),
        dialect=str(data.get("dialect") or "generic"),
        library_sources=library_sources,
        compatibility_sources=compatibility_sources,
        st_sources=st_sources,
        native_sources=_paths(root, native.get("sources", []), "native.sources"),
        native_objects=_paths(root, native.get("objects", []), "native.objects"),
        native_libraries=_paths(root, native.get("libraries", []), "native.libraries"),
        native_compile_args=tuple(str(value) for value in native.get("compile_args", [])),
        link_args=tuple(str(value) for value in native.get("link_args", [])),
        profiles=tuple(str(value) for value in data.get("profiles", [])),
        stdlib_glob=_toolchain_glob(root, toolchain.get("stdlib_glob")),
        runtime_archive=runtime_archive,
    )
