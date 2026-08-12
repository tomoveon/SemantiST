#!/usr/bin/env python3
"""Shared ST dialect-normalization rules for the RuSTy converter.

The analyzer and converter both import this module so rule ids, patterns, and
replacement text cannot drift apart.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict

Replacement = Callable[[re.Match[str]], str]


def _const(value: str) -> Replacement:
    return lambda _match: value


RULES: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "SET -> SET0",
        "description": "Rename SET variable to SET0 (keyword conflict)",
        "pattern": r"\bSET\b",
        "replacement": _const("SET0"),
        "dialects": {"codesys"},
    },
    2: {
        "name": "OVERRIDE -> _OVERRIDE",
        "description": "Rename OVERRIDE to _OVERRIDE (keyword conflict)",
        "pattern": r"\bOVERRIDE\b",
        "replacement": _const("_OVERRIDE"),
        "dialects": {"codesys"},
    },
    3: {
        "name": "POINTER TO -> REF_TO",
        "description": "Replace CODESYS/TwinCAT pointer declarations with RuSTy's REF_TO form",
        "pattern": r"\bPOINTER\s+TO\b",
        "replacement": _const("REF_TO"),
        "dialects": {"codesys", "twincat", "generic"},
    },
    4: {
        "name": "ADR() -> REF()",
        "description": "Replace address-of builtin with RuSTy's reference builtin",
        "pattern": r"\bADR\s*\(",
        "replacement": _const("REF("),
        "dialects": {"codesys", "twincat", "generic"},
    },
    5: {
        "name": "Remove VAR_INPUT CONSTANT",
        "description": "Remove CONSTANT from VAR_INPUT CONSTANT",
        "pattern": r"\bVAR_INPUT\s+CONSTANT\b",
        "replacement": _const("VAR_INPUT"),
        "dialects": {"codesys", "twincat"},
    },
    6: {
        "name": "Array size UINT -> ULINT",
        "description": "Change array size parameter from UINT to ULINT",
        "pattern": r"\b(size)\s*:\s*UINT\b",
        "replacement": lambda match: match.group(0).replace("UINT", "ULINT"),
        "dialects": {"codesys"},
    },
    7: {
        "name": "Array init with []",
        "description": "Wrap array initialization values with []",
        "pattern": r":=\s*([+-]?\d+(?:\s*,\s*[+-]?\d+)+)\s*;",
        "replacement": lambda match: ":= [" + match.group(1) + "];",
        "context_required": True,
        "dialects": {"codesys", "twincat", "generic"},
    },
    8: {
        "name": "STRING() -> STRING[]",
        "description": "Change STRING(n) or WSTRING(n) to STRING[n] or WSTRING[n]",
        "pattern": r"\b(W?STRING)\s*\(\s*([^)]+)\s*\)",
        "replacement": lambda match: f"{match.group(1)}[{match.group(2).strip()}]",
        "dialects": {"codesys", "twincat", "generic"},
    },
    9: {
        "name": "FUNCTIONBLOCK -> FUNCTION_BLOCK",
        "description": "Add underscore to compact FUNCTIONBLOCK spelling",
        "pattern": r"\bFUNCTIONBLOCK\b",
        "replacement": _const("FUNCTION_BLOCK"),
        "dialects": {"codesys", "twincat", "generic"},
    },
    10: {
        "name": "TOD HH:MM -> TOD HH:MM:SS",
        "description": "Ensure TOD literals have HH:MM:SS format",
        "pattern": r"\bTOD#(\d{1,2}):(\d{2})(?!:\d)",
        "replacement": lambda match: f"TOD#{match.group(1)}:{match.group(2)}:00",
        "dialects": {"codesys", "twincat", "generic"},
    },
    11: {
        "name": "Function call [] -> ()",
        "description": "Replace named-argument function calls written with []",
        "pattern": r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*([A-Za-z_][A-Za-z0-9_]*\s*:=\s*[^\]]+)\]",
        "replacement": lambda match: f"{match.group(1)}({match.group(2)})",
        "dialects": {"codesys", "twincat"},
    },
    12: {
        "name": "METHOD -> FUNCTION",
        "description": "Convert METHOD/END_METHOD to FUNCTION/END_FUNCTION (unsupported POU)",
        "pattern": None,
        "replacement": None,
        "dialects": {"codesys", "twincat"},
    },
    13: {
        "name": "Add missing END_*",
        "description": "Add missing END_FUNCTION/END_FUNCTION_BLOCK/END_PROGRAM",
        "pattern": None,
        "replacement": None,
        "dialects": {"codesys", "twincat", "generic"},
    },
    14: {
        "name": "ENDIF identifier -> ENDIF0",
        "description": "Rename ENDIF identifier to ENDIF0 (keyword conflict)",
        "pattern": r"\bENDIF\b",
        "replacement": _const("ENDIF0"),
        "dialects": {"codesys"},
    },
    15: {
        "name": "STRING_LENGTH -> 255",
        "description": "Inline OSCAT string length constant for RuSTy type-size extraction",
        "pattern": r"\bSTRING_LENGTH\b(?!\s*:)",
        "replacement": _const("255"),
        "dialects": {"codesys"},
    },
    16: {
        "name": "LIST_LENGTH -> 255",
        "description": "Inline OSCAT list length constant for RuSTy type-size extraction",
        "pattern": r"\bLIST_LENGTH\b(?!\s*:)",
        "replacement": _const("255"),
        "dialects": {"codesys"},
    },
    17: {
        "name": "ELSEIF -> ELSIF",
        "description": "Normalize alternate IF branch spelling to IEC/RuSTy ELSIF",
        "pattern": r"\bELSEIF\b",
        "replacement": _const("ELSIF"),
        "dialects": {"twincat", "generic"},
    },
    18: {
        "name": "ENDCASE -> END_CASE",
        "description": "Normalize compact CASE terminator spelling",
        "pattern": r"\bENDCASE\b",
        "replacement": _const("END_CASE"),
        "dialects": {"twincat", "generic"},
    },
    19: {
        "name": "ENDFOR -> END_FOR",
        "description": "Normalize compact FOR terminator spelling",
        "pattern": r"\bENDFOR\b",
        "replacement": _const("END_FOR"),
        "dialects": {"twincat", "generic"},
    },
    20: {
        "name": "ENDWHILE -> END_WHILE",
        "description": "Normalize compact WHILE terminator spelling",
        "pattern": r"\bENDWHILE\b",
        "replacement": _const("END_WHILE"),
        "dialects": {"twincat", "generic"},
    },
    21: {
        "name": "ENDREPEAT -> END_REPEAT",
        "description": "Normalize compact REPEAT terminator spelling",
        "pattern": r"\bENDREPEAT\b",
        "replacement": _const("END_REPEAT"),
        "dialects": {"twincat", "generic"},
    },
    22: {
        "name": "ENDTYPE -> END_TYPE",
        "description": "Normalize compact TYPE terminator spelling",
        "pattern": r"\bENDTYPE\b",
        "replacement": _const("END_TYPE"),
        "dialects": {"twincat", "generic"},
    },
    23: {
        "name": "ENDSTRUCT -> END_STRUCT",
        "description": "Normalize compact STRUCT terminator spelling",
        "pattern": r"\bENDSTRUCT\b",
        "replacement": _const("END_STRUCT"),
        "dialects": {"twincat", "generic"},
    },
    24: {
        "name": "T# -> TIME#",
        "description": "Normalize short TIME literal prefix",
        "pattern": r"\bT#",
        "replacement": _const("TIME#"),
        "dialects": {"twincat", "generic"},
    },
    25: {
        "name": "D# -> DATE#",
        "description": "Normalize short DATE literal prefix",
        "pattern": r"\bD#",
        "replacement": _const("DATE#"),
        "dialects": {"twincat", "generic"},
    },
    26: {
        "name": "Namespace call dot -> underscore",
        "description": "Flatten vendor namespace calls such as Tc2_Standard.Foo(...)",
        "pattern": r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        "replacement": lambda match: f"{match.group(1)}_{match.group(2)}(",
        "dialects": {"twincat"},
    },
}

# Only proven spelling/syntax aliases are automatic. Rules that can affect
# range, mutability, or symbol binding require per-library evidence. A METHOD
# is not generally equivalent to a free FUNCTION and therefore blocks intake.
for _rule_id, _rule in RULES.items():
    _rule["safety"] = "syntax_alias"
for _rule_id in {5, 6, 13, 15, 16, 26}:
    RULES[_rule_id]["safety"] = "review_required"
RULES[12]["safety"] = "unsupported"


DIALECT_PROFILES = {
    "codesys": "CODESYS/OSCAT compatibility rules already used by SemantiST",
    "twincat": "TwinCAT-style compact terminators, short literals, and namespace calls",
    "generic": "Conservative IEC-style normalizations shared by multiple ST dialects",
    "all": "All known normalizations; use for exploration and review carefully",
}


BLOCK_ENDINGS = {
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    "FUNCTION": "END_FUNCTION",
    "PROGRAM": "END_PROGRAM",
}


def active_rules(profile: str) -> Dict[int, Dict[str, Any]]:
    """Return rules enabled for a dialect profile."""
    profile = profile.lower()
    if profile not in DIALECT_PROFILES:
        raise ValueError(f"unknown dialect profile {profile!r}")
    if profile == "all":
        return dict(RULES)
    return {
        rule_id: rule
        for rule_id, rule in RULES.items()
        if profile in rule.get("dialects", set())
    }


def dialect_choices() -> list[str]:
    return sorted(DIALECT_PROFILES)
