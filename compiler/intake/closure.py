"""Dependency closure between extracted library files.

A library is compiled together with the libraries it uses when those are part
of the corpus. Dependencies come from three sources, in this order:

1. library configurations (``normalize/libraries/*.json``: ``dependencies``),
   which state the evidence for the choice;
2. TwinCAT/CODESYS library qualifiers ``Lib.Name`` where a project of the
   same repository is called ``Lib`` (``xml_<repo>__<Lib>.st``);
3. SIMATIC AX apax manifests: the ``dependencies:`` list in the extraction
   header names packages, mapped to the files whose header names that package.

No dependency is guessed from a symbol alone: an unresolved name that several
corpus libraries define stays unresolved (the report lists candidates).
"""

from __future__ import annotations

import fnmatch
import json
import re
from functools import lru_cache
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

LIBRARY_CONFIGS = Path(__file__).resolve().parent / "normalize" / "libraries"
_AX_PACKAGE = re.compile(r"SIMATIC AX whole apax package (?P<name>\S+);")
_AX_DEPENDENCIES = re.compile(r"; dependencies: (?P<list>[^*]*?)(?:;| \*\))")
_XML_NAME = re.compile(r"^xml_(?P<repo>.+?)__(?P<project>[^_].*)\.st$")


@lru_cache(maxsize=None)
def configs() -> tuple[dict, ...]:
    return tuple(json.loads(p.read_text(encoding="utf-8")) for p in sorted(LIBRARY_CONFIGS.glob("*.json")))


def config_for(name: str) -> dict:
    for config in configs():
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in config.get("match", [])):
            return config
    return {}


def _first_line(path: Path) -> str:
    with path.open(encoding="utf-8") as handle:
        return handle.readline()


def _qualifiers(path: Path) -> set[str]:
    tokens = significant(tokenize_lossless(path.read_text(encoding="utf-8")))
    found = set()
    for previous, token, following in zip([None, *tokens], tokens, tokens[1:]):
        if token.is_word and following.text == "." and (previous is None or previous.text != "."):
            found.add(token.upper)
    return found


def direct_dependencies(path: Path, folder: Path) -> list[tuple[Path, str]]:
    """(dependency file, reason) for one library file in ``folder``."""
    result: list[tuple[Path, str]] = []
    config = config_for(path.name)
    for item in config.get("dependencies", []):
        target = folder.parent.parent / item["vendor"] / ".st文件" / item["library"]
        result.append((target, "library config: " + item.get("reason", "")))
    match = _XML_NAME.match(path.name)
    if match:
        siblings = {}
        for other in folder.glob(f"xml_{match['repo']}__*.st"):
            sibling = _XML_NAME.match(other.name)
            if sibling and other != path:
                siblings[sibling["project"].upper()] = other
        for qualifier in sorted(_qualifiers(path) & set(siblings)):
            result.append((siblings[qualifier], f"library qualifier {qualifier}."))
    header = _first_line(path)
    dependencies = _AX_DEPENDENCIES.search(header)
    if _AX_PACKAGE.search(header) and dependencies:
        packages = {}
        for other in folder.glob("*.st"):
            if other.name.endswith("__test_project.st"):
                continue
            package = _AX_PACKAGE.search(_first_line(other))
            if package:
                packages[package["name"]] = other
        for entry in dependencies["list"].split(","):
            name = entry.strip().split(" ")[0]
            if name in packages and packages[name] != path:
                result.append((packages[name], f"apax dependency {name}"))
    seen, unique = set(), []
    for target, reason in result:
        if target not in seen and target != path:
            seen.add(target)
            unique.append((target, reason))
    return unique


def closure(path: Path) -> list[tuple[Path, str]]:
    """Transitive dependencies of ``path`` in dependency-first order."""
    folder = path.parent
    order: list[tuple[Path, str]] = []
    visiting: set[Path] = set()
    done: set[Path] = set()

    def visit(current: Path, reason: str) -> None:
        if current in done or current in visiting:
            return
        visiting.add(current)
        for target, why in direct_dependencies(current, current.parent if current.parent.exists() else folder):
            if target.is_file():
                visit(target, why)
        visiting.discard(current)
        done.add(current)
        if current != path:
            order.append((current, reason))

    visit(path, "")
    return order
