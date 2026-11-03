"""Rule interface of the dialect normalizer.

A rule is a token pattern with preconditions that turns one RuSTy diagnostic
into span edits. Rules never run preventively: the engine offers a rule only
the diagnostics the pinned compiler actually reported. Every rule documents the
semantic premise under which the rewrite preserves behaviour for all inputs,
where it does not apply, and why it is equivalent; a probe shows that the
pinned compiler rejects the original form and accepts the rewritten one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..diagnostics import Located, SourceIndex


@dataclass(frozen=True)
class RuleSpec:
    id: str
    version: int
    title: str
    level: str  # "L1" spelling substitution, "L2" structure rewrite with scope/type information
    dialects: frozenset[str]
    codes: frozenset[str]
    premise: str
    not_applicable: str
    equivalence: str
    probe: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "level": self.level,
            "dialects": sorted(self.dialects),
            "codes": sorted(self.codes),
            "premise": self.premise,
            "not_applicable": self.not_applicable,
            "equivalence": self.equivalence,
            "probe": self.probe,
        }


@dataclass(frozen=True)
class Edit:
    """Replace ``[start, end)`` by ``replacement``; with ``move_to`` set, move
    ``[start, end)`` to that offset instead (``replacement`` is ignored)."""

    file: str
    start: int
    end: int
    replacement: str = ""
    move_to: int | None = None


@dataclass
class Proposal:
    """Edits one rule proposes for one diagnostic.

    ``key`` identifies the rewrite: two diagnostics caused by the same
    construct (a symbol that must be renamed everywhere) propose one rewrite.
    """

    rule: RuleSpec
    key: tuple
    edits: list[Edit]
    note: str = ""
    renames: dict[str, str] = field(default_factory=dict)


class RuleContext:
    """What a rule may look at: current texts, their indices, configuration."""

    def __init__(self, files: dict[str, str], indices: dict[str, SourceIndex], dialect: dict, library: dict,
                 known_names: set[str], standard_names: set[str] = frozenset()):
        self.files = files
        self.indices = indices
        self.dialect = dialect
        self.library = library
        self.known_names = known_names
        self.standard_names = standard_names

    def fresh_name(self, base: str, suffix: str) -> str:
        """A name that no file, standard-library or compatibility symbol uses."""
        candidate = base + suffix
        number = 2
        while candidate.upper() in self.known_names:
            candidate = f"{base}{suffix}{number}"
            number += 1
        self.known_names.add(candidate.upper())
        return candidate


class Rule:
    spec: RuleSpec

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        raise NotImplementedError
