"""Raw ST declaration extraction for benchmark harness generation.

Extracts the exact declaration text of a FUNCTION or FUNCTION_BLOCK from a
normalized target.st file so that the generated PLC_PRG wrapper can re-declare
the target's inputs with byte-identical types (including array bounds that
reference VAR_GLOBAL CONSTANT names such as ``ARRAY[0..ArraySize] OF LREAL``).

Also provides a small dependency-closure resolver used to inline the minimal
set of library POUs into ``structuredfuzzer.st`` for the matiec/StructuredFuzzer
toolchain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from compiler.parser.st import (
    StToken,
    strip_comments,
    tokenize_st,
    normalize_var_input_constant,
)


@dataclass
class Declaration:
    block_kind: str
    name: str
    type_raw: str


@dataclass
class Signature:
    kind: str
    name: str
    ret_type_raw: str
    params: list[Declaration]
    outputs: list[Declaration]
    inouts: list[Declaration]
    all_inputs: list[Declaration]  # VAR_INPUT + VAR_IN_OUT in source order
    # Raw text of leading VAR_GLOBAL blocks (needed by scan-cycle targets).
    globals_raw: str


VAR_BLOCK_STARTS = {"VAR", "VAR_INPUT", "VAR_IN_OUT", "VAR_OUTPUT", "VAR_TEMP"}
VAR_BLOCK_MODIFIERS = {"CONSTANT", "RETAIN", "PERSISTENT", "NON_RETAIN"}


def _tokens_to_text(tokens: list[StToken]) -> str:
    if not tokens:
        return ""
    out = ""
    tight_left = {"[", "(", "^", "#"}
    tight_right = {"]", ")", ",", ";", ":", "..", "#"}
    for token in tokens:
        text = token.text
        if not out or text in tight_right or out[-1] in tight_left:
            out += text
        else:
            out += " " + text
    return out


def _clean_type(tokens: list[StToken]) -> str:
    text = _tokens_to_text(tokens).strip()
    text = re.sub(r"\s+\[", "[", text)
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\s*\.\.\s*", "..", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_signature(path: str, target: str) -> Signature:
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    normalized = strip_comments(normalize_var_input_constant(source))
    tokens = tokenize_st(normalized)

    globals_raw = ""
    for match in re.finditer(r"(?i)\bVAR_GLOBAL\b", normalized):
        # Capture the full VAR_GLOBAL ... END_VAR block as raw text.
        pass

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.upper in ("FUNCTION", "FUNCTION_BLOCK"):
            kind = tok.upper
            name = tokens[i + 1].text if i + 1 < n else ""
            # Skip "name : RETTYPE" for FUNCTION; "name" for FUNCTION_BLOCK.
            j = i + 2
            ret_tokens: list[StToken] = []
            if kind == "FUNCTION":
                while j < n and tokens[j].text not in (":",):
                    j += 1
                if j < n and tokens[j].text == ":":
                    j += 1
                while j < n and tokens[j].upper not in VAR_BLOCK_STARTS | {"END_FUNCTION"}:
                    ret_tokens.append(tokens[j])
                    j += 1
            else:
                while j < n and tokens[j].upper not in VAR_BLOCK_STARTS | {"END_FUNCTION_BLOCK"}:
                    j += 1

            params: list[Declaration] = []
            outputs: list[Declaration] = []
            inouts: list[Declaration] = []
            all_inputs: list[Declaration] = []
            globals_raw = _extract_globals(normalized)
            # Parse VAR blocks.
            while j < n:
                tk = tokens[j]
                if tk.upper in VAR_BLOCK_STARTS:
                    base = tk.upper
                    j += 1
                    modifiers: set[str] = set()
                    while j < n and tokens[j].upper in VAR_BLOCK_MODIFIERS:
                        modifiers.add(tokens[j].upper)
                        j += 1
                    # Skip "(* CONSTANT *)" style is handled by normalization.
                    block_kind = base + ("_CONSTANT" if "CONSTANT" in modifiers else "")
                    j = _parse_declarations(tokens, j, block_kind, params, outputs, inouts, all_inputs)
                elif tk.upper in ("END_FUNCTION", "END_FUNCTION_BLOCK"):
                    break
                else:
                    j += 1
            return Signature(
                kind=kind,
                name=name,
                ret_type_raw=_clean_type(ret_tokens),
                params=params,
                outputs=outputs,
                inouts=inouts,
                all_inputs=all_inputs,
                globals_raw=globals_raw,
            )
        i += 1
    raise ValueError(f"target POU {target!r} not found in {path}")


def _extract_globals(normalized: str) -> str:
    blocks: list[str] = []
    for match in re.finditer(r"(?is)\bVAR_GLOBAL\b(?:[\s]*CONSTANT|RETAIN|PERSISTENT|NON_RETAIN)*\s*.*?END_VAR\b", normalized):
        blocks.append(match.group(0))
    return "\n\n".join(blocks)


def _parse_declarations(tokens: list[StToken], start: int, block_kind: str,
                        params: list[Declaration], outputs: list[Declaration],
                        inouts: list[Declaration], all_inputs: list[Declaration]) -> int:
    """Parse declarations until END_VAR. Returns the index after END_VAR."""
    i = start
    n = len(tokens)
    while i < n and tokens[i].upper != "END_VAR":
        if tokens[i].text == ";":
            i += 1
            continue
        # Collect names.
        names: list[str] = []
        j = i
        while j < n and tokens[j].kind == "identifier":
            names.append(tokens[j].text)
            j += 1
            if j < n and tokens[j].text == ",":
                j += 1
                continue
            break
        if not names:
            i += 1
            continue
        # Skip "AT %..." if present.
        if j < n and tokens[j].upper == "AT":
            j += 1
            if j < n and tokens[j].kind == "address":
                j += 1
        if j < n and tokens[j].text != ":":
            # Malformed; skip to next ';'
            while j < n and tokens[j].text != ";":
                j += 1
            i = j + 1 if j < n else j
            continue
        j += 1  # skip ':'
        # Collect type tokens until ';' or ':='.
        type_tokens: list[StToken] = []
        depth = 0
        while j < n:
            tk = tokens[j]
            if depth == 0 and tk.text in (";", ":="):
                break
            if tk.text in ("[", "("):
                depth += 1
            elif tk.text in ("]", ")"):
                depth = max(0, depth - 1)
            type_tokens.append(tk)
            j += 1
        type_raw = _clean_type(type_tokens)
        for name in names:
            d = Declaration(block_kind=block_kind, name=name, type_raw=type_raw)
            if block_kind == "VAR_OUTPUT":
                outputs.append(d)
            elif block_kind == "VAR_IN_OUT":
                inouts.append(d)
                all_inputs.append(d)
            elif block_kind in ("VAR_INPUT", "VAR_INPUT_CONSTANT"):
                params.append(d)
                all_inputs.append(d)
            # VAR / VAR_TEMP / VAR_CONSTANT / VAR_RETAIN state fields are ignored.
        # Skip any initializer (':= ...' until ';').
        if j < n and tokens[j].text == ":=":
            depth = 0
            while j < n:
                tk = tokens[j]
                if tk.text in ("[", "("):
                    depth += 1
                elif tk.text in ("]", ")"):
                    depth = max(0, depth - 1)
                if tk.text == ";" and depth == 0:
                    j += 1
                    break
                j += 1
        else:
            if j < n and tokens[j].text == ";":
                j += 1
        i = j
    if i < n and tokens[i].upper == "END_VAR":
        i += 1
    return i


# ---------------------------------------------------------------------------
# Dependency closure
# ---------------------------------------------------------------------------

_CALL_RE = re.compile(r"(?i)\b([A-Z_][A-Z0-9_]*)\s*\(")
_POU_HEADER_RE = re.compile(r"(?im)^[ \t]*(FUNCTION|FUNCTION_BLOCK)[ \t]+([A-Z_][A-Z0-9_]*)")

_POU_END = {
    "FUNCTION": "END_FUNCTION",
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
}

_ST_KEYWORDS = {
    "IF", "THEN", "ELSIF", "ELSE", "END_IF", "FOR", "TO", "BY", "DO", "END_FOR",
    "WHILE", "END_WHILE", "REPEAT", "UNTIL", "END_REPEAT", "CASE", "END_CASE",
    "VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_GLOBAL",
    "VAR_EXTERNAL", "VAR_ACCESS", "END_VAR", "FUNCTION", "END_FUNCTION",
    "FUNCTION_BLOCK", "END_FUNCTION_BLOCK", "PROGRAM", "END_PROGRAM",
    "TYPE", "END_TYPE", "STRUCT", "END_STRUCT", "CONFIGURATION",
    "END_CONFIGURATION", "RESOURCE", "END_RESOURCE", "TASK",
    "ARRAY", "OF", "AT", "RETAIN", "PERSISTENT", "NON_RETAIN", "CONSTANT",
    "RETURN", "EXIT", "CONTINUE", "NOT", "AND", "OR", "XOR", "MOD", "TRUE",
    "FALSE", "INITIAL_STEP", "END_STEP", "TRANSITION", "END_TRANSITION",
    "ACTION", "END_ACTION", "WITH", "ON", "ONLY", "REF", "ADR", "SIZEOF",
    "REF_TO", "POINTER", "STEP", "EN", "ENO", "BOOL", "SINT", "INT", "DINT",
    "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL", "BYTE", "WORD",
    "DWORD", "LWORD", "STRING", "WSTRING", "CHAR", "WCHAR", "TIME", "DATE",
    "DATE_AND_TIME", "DT", "TOD", "TIME_OF_DAY", "LTIME", "LDATE", "LTOD",
}


def _pou_map(source: str) -> dict[str, str]:
    """Map uppercase POU name -> full POU text."""
    result: dict[str, str] = {}
    for match in _POU_HEADER_RE.finditer(source):
        kind = match.group(1).upper()
        name = match.group(2).upper()
        start = match.start()
        end_kw = _POU_END[kind]
        end = re.search(rf"(?im)^[ \t]*{end_kw}\b[^\n]*(?:\n|$)", source[match.end():])
        if end is None:
            continue
        block_end = match.end() + end.end()
        result[name] = source[start:block_end]
    return result


def _call_names(text: str) -> list[str]:
    stripped = strip_comments(text)
    return [m for m in _CALL_RE.findall(stripped) if m.upper() not in _ST_KEYWORDS]


_SCALAR_TYPE_NAMES = {
    "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
    "REAL", "LREAL", "BYTE", "WORD", "DWORD", "LWORD", "STRING", "WSTRING",
    "CHAR", "WCHAR", "TIME", "LTIME", "DATE", "LDATE", "DATE_AND_TIME", "DT",
    "LDT", "TIME_OF_DAY", "TOD", "LTOD", "LDATE_AND_TIME", "ARRAY", "POINTER",
    "REF_TO", "ANY", "ANY_NUM", "ANY_INT", "ANY_REAL", "ANY_BIT", "ANY_STRING",
    "ANY_DATE", "ANY_DERIVED", "ANY_MAGNITUDE", "ANY_ELEMENTARY",
}


def _declared_identifiers(text: str) -> set[str]:
    """Collect identifiers declared in VAR blocks (variable and POU names)."""
    stripped = strip_comments(text)
    tokens = tokenize_st(stripped)
    declared: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].upper in VAR_BLOCK_STARTS:
            i += 1
            while i < n and tokens[i].upper in VAR_BLOCK_MODIFIERS:
                i += 1
            while i < n and tokens[i].upper != "END_VAR":
                if tokens[i].kind == "identifier":
                    names = [tokens[i].text]
                    j = i + 1
                    while j < n and tokens[j].text == ",":
                        if j + 1 < n and tokens[j + 1].kind == "identifier":
                            names.append(tokens[j + 1].text)
                            j += 2
                        else:
                            break
                    for name in names:
                        declared.add(name.upper())
                    i = j
                    continue
                i += 1
            if i < n and tokens[i].upper == "END_VAR":
                i += 1
        else:
            i += 1
    return declared


def _external_refs(text: str, stdlib_names: set[str]) -> set[str]:
    """Return identifiers referenced by a POU that are external (functions/types)."""
    stripped = strip_comments(text)
    tokens = tokenize_st(stripped)
    declared = _declared_identifiers(text)
    refs: set[str] = set()
    for token in tokens:
        if token.kind != "identifier":
            continue
        up = token.upper
        if up in _ST_KEYWORDS:
            continue
        if up in _SCALAR_TYPE_NAMES:
            continue
        if up in stdlib_names:
            continue
        if up in declared:
            continue
        refs.add(up)
    return refs


def extract_pou_text(path: str, target: str) -> str:
    """Return the full FUNCTION/FUNCTION_BLOCK text (header .. END_*) for target."""
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    pmap = _pou_map(source)
    up = target.upper()
    if up in pmap:
        return pmap[up].strip() + "\n"
    raise ValueError(f"POU {target!r} not found in {path}")


def strip_pou(source: str, target: str) -> str:
    """Return ``source`` with the named POU's block removed (for deps-only libraries)."""
    pmap = _pou_map(source)
    up = target.upper()
    if up not in pmap:
        raise ValueError(f"POU {target!r} not found in source")
    block = pmap[up]
    index = source.find(block)
    if index == -1:
        # Fall back to locating the header line.
        for match in _POU_HEADER_RE.finditer(source):
            kind = match.group(1).upper()
            name = match.group(2).upper()
            if name != up:
                continue
            start = match.start()
            end_kw = _POU_END[kind]
            end = re.search(rf"(?im)^[ \t]*{end_kw}\b[^\n]*(?:\n|$)", source[match.end():])
            if end is None:
                break
            block_end = match.end() + end.end()
            return source[:start] + source[block_end:]
    return source[:index] + source[index + len(block):]


def resolve_dependencies(target_name: str, target_pou_text: str, libraries: list[str],
                         stdlib_names: set[str]) -> tuple[list[str], list[str]]:
    """Return (ordered POU texts, unresolved names) for the transitive closure."""
    pou_texts: dict[str, str] = {}
    library_maps = [_pou_map(lib) for lib in libraries]
    order: list[str] = []
    unresolved: list[str] = []

    def visit(name: str) -> None:
        up = name.upper()
        if up in pou_texts or up == target_name.upper():
            return
        found = None
        for lib in library_maps:
            if up in lib:
                found = lib[up]
                break
        if found is None:
            if up not in stdlib_names:
                unresolved.append(up)
            return
        pou_texts[up] = found
        order.append(up)
        for ref in _external_refs(found, stdlib_names):
            if ref not in pou_texts and ref != target_name.upper():
                visit(ref)

    for ref in _external_refs(target_pou_text, stdlib_names):
        visit(ref)

    return [pou_texts[name] for name in order], unresolved
