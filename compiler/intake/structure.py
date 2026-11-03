"""Top-level structure of an ST file from the lossless token stream.

This is a structural scan, not a parser. It pairs each declaration keyword with
its END keyword (compact ``ENDxxx`` and ``END_FUNCTIONBLOCK`` spellings
included) and records character spans, so later stages can attribute
diagnostics to units, rename symbols per unit or exclude a unit from a
measurement build. Namespaces are transparent: their units are reported with
qualified names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from compiler.parser.st import LosslessToken, significant, tokenize_lossless

UNIT_END = {
    "FUNCTION": "END_FUNCTION",
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    "PROGRAM": "END_PROGRAM",
    "CLASS": "END_CLASS",
    "INTERFACE": "END_INTERFACE",
    "TYPE": "END_TYPE",
    "CONFIGURATION": "END_CONFIGURATION",
    "DATA_BLOCK": "END_DATA_BLOCK",
    "ORGANIZATION_BLOCK": "END_ORGANIZATION_BLOCK",
    "VAR_GLOBAL": "END_VAR",
    "ACTIONS": "END_ACTIONS",
    # Methods, properties and actions exported outside their owner.
    "METHOD": "END_METHOD",
    "PROPERTY": "END_PROPERTY",
    "ACTION": "END_ACTION",
    "PROPERTY_GET": "END_PROPERTY",
    "PROPERTY_SET": "END_PROPERTY",
}
CONTAINER_KINDS = {"FUNCTION_BLOCK", "CLASS", "INTERFACE", "PROGRAM"}
MEMBER_KINDS = {"METHOD", "PROPERTY", "ACTION", "PROPERTY_GET", "PROPERTY_SET"}
MODIFIERS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "ABSTRACT", "FINAL", "OVERRIDE"}
MARKER = re.compile(
    r"^\(\* (?:Source member: |Source: |Object: |Global variable list: |Original worksheet |Source )(?P<value>.*) \*\)$", re.S
)


def canonical_keyword(word: str) -> str:
    """Map RuSTy's alternative keyword spellings to the underscore form."""
    word = word.upper()
    aliases = {"FUNCTIONBLOCK": "FUNCTION_BLOCK", "END_FUNCTIONBLOCK": "END_FUNCTION_BLOCK",
               "ENDFUNCTIONBLOCK": "END_FUNCTION_BLOCK", "PROPERTYGET": "PROPERTY_GET",
               "PROPERTYSET": "PROPERTY_SET"}
    if word in aliases:
        return aliases[word]
    if word.startswith("END") and not word.startswith("END_") and len(word) > 3:
        return "END_" + word[3:]
    return word


@dataclass
class Member:
    kind: str
    name: str
    start: int
    end: int
    line: int


@dataclass
class Unit:
    kind: str
    name: str
    start: int
    end: int
    line: int
    end_line: int
    namespace: str = ""
    marker: str | None = None
    closed: bool = True
    members: list[Member] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        if self.namespace and self.name:
            return self.namespace + "." + self.name
        return self.name


def _declared_name(tokens: list[LosslessToken], index: int) -> str:
    i = index + 1
    # A modifier is followed by another name; otherwise it is the name itself
    # (OSCAT has a FUNCTION called OVERRIDE).
    while (i + 1 < len(tokens) and tokens[i].upper in MODIFIERS
           and tokens[i + 1].kind in {"identifier", "keyword", "wstring"}):
        i += 1
    if i < len(tokens) and tokens[i].kind in {"identifier", "keyword", "wstring"}:
        name = tokens[i].text.strip('"')
        # ACTION fb.name: the action is named with its owner.
        if (tokens[index].upper == "ACTION" and i + 2 < len(tokens) and tokens[i + 1].text == "."
                and tokens[i + 2].kind in {"identifier", "keyword"}):
            name += "." + tokens[i + 2].text
        return name
    return ""


def _span_end(tokens: list[LosslessToken], j: int) -> int:
    """End offset of the END keyword at ``j``, including a directly following ';'."""
    end = tokens[j].end
    if j + 1 < len(tokens) and tokens[j + 1].text == ";":
        end = tokens[j + 1].end
    return end


def split_units(text: str, tokens: list[LosslessToken] | None = None) -> list[Unit]:
    all_tokens = tokens if tokens is not None else tokenize_lossless(text)
    sig = significant(all_tokens)
    markers = [(t.start, MARKER.match(t.text).group("value")) for t in all_tokens
               if t.kind == "comment" and MARKER.match(t.text)]
    result: list[Unit] = []
    namespaces: list[str] = []
    marker_index = 0
    current_marker = None
    i = 0
    while i < len(sig):
        token = sig[i]
        while marker_index < len(markers) and markers[marker_index][0] < token.start:
            current_marker = markers[marker_index][1]
            marker_index += 1
        word = canonical_keyword(token.text) if token.is_word else ""
        if word == "NAMESPACE":
            j = i + 1
            parts = []
            while j < len(sig) and sig[j].is_word:
                parts.append(sig[j].text)
                j += 1
                if j < len(sig) and sig[j].text == ".":
                    j += 1
                else:
                    break
            namespaces.append(".".join(parts))
            i = j
            continue
        if word == "END_NAMESPACE" and namespaces:
            namespaces.pop()
            i += 1
            continue
        if word not in UNIT_END:
            i += 1
            continue
        end_word = UNIT_END[word]
        name = "" if word == "VAR_GLOBAL" else _declared_name(sig, i)
        unit = Unit(word, name, token.start, token.end, token.line, token.line, ".".join(namespaces),
                    current_marker)
        depth = 0
        j = i + 1
        while j < len(sig):
            other = canonical_keyword(sig[j].text) if sig[j].is_word else ""
            if other == end_word:
                if depth == 0:
                    break
                depth -= 1
            elif other == word and word not in {"VAR_GLOBAL", "TYPE"}:
                depth += 1
            elif word in CONTAINER_KINDS and other in MEMBER_KINDS:
                member_end_word = UNIT_END[other]
                k = j + 1
                while k < len(sig):
                    inner = canonical_keyword(sig[k].text) if sig[k].is_word else ""
                    if inner in {member_end_word, end_word}:
                        break
                    k += 1
                closed = k < len(sig) and canonical_keyword(sig[k].text) == member_end_word
                member_end = _span_end(sig, k) if closed else sig[k - 1].end
                unit.members.append(Member(other, _declared_name(sig, j), sig[j].start, member_end, sig[j].line))
                # An unclosed member ends where its owner ends; rescan that END.
                j = k + 1 if closed else k
                continue
            j += 1
        if j < len(sig):
            unit.end = _span_end(sig, j)
            unit.end_line = sig[j].line
        else:
            unit.end = sig[-1].end
            unit.end_line = sig[-1].line
            unit.closed = False
        result.append(unit)
        i = j + 1
    return result
