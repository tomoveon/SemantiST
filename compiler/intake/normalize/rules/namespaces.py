"""Library namespace qualifiers (TwinCAT/CODESYS ``Lib.Name``)."""

from __future__ import annotations

from ...diagnostics import Located
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec


class LibraryQualifier(Rule):
    spec = RuleSpec(
        id="library-qualifier",
        version=1,
        title="Lib.Name -> Name when library Lib is compiled in the same project and defines Name",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007", "E006", "E048", "E052"}),
        premise="In TwinCAT/CODESYS a library's namespace qualifies its POUs, types and global variables, and "
                "a global variable list's name qualifies its variables (GVL.x). "
                "When that library is part of the compiled project (dependency closure) and defines Name, "
                "Lib.Name denotes exactly that definition. The dialect's standard namespaces (Tc2_Standard, "
                "Standard) denote the IEC standard blocks RuSTy's standard library provides.",
        not_applicable="Qualifiers of libraries outside the project (Tc2_System, Tc2_EtherCAT, ...): left "
                       "unchanged and reported as unresolved dependencies; names defined by more than one "
                       "project file; a qualifier that is also a variable or POU name.",
        equivalence="The qualifier only selects the definition; after removing it the name resolves to the "
                    "same unique definition in the project.",
        probe="library-qualifier",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        namespaces: dict[str, set[str]] = context.library.get("_namespaces", {})
        standard = {n.upper() for n in context.dialect.get("standard_namespaces", [])}
        defined: dict[str, int] = {}
        for index in context.indices.values():
            for unit in index.units:
                if unit.name:
                    defined[unit.name.upper()] = defined.get(unit.name.upper(), 0) + 1
        # Variables of global variable lists count as defined once per list.
        for members in namespaces.values():
            for member in members:
                defined.setdefault(member, 1)
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(located.offset)
        candidates = [k for k in range(max(0, i - 6), min(len(tokens) - 2, i + 6))
                      if tokens[k].is_word and tokens[k + 1].text == "." and tokens[k + 2].is_word
                      and (k == 0 or tokens[k - 1].text != ".")]
        for k in candidates:
            qualifier = tokens[k].upper
            name = tokens[k + 2].upper
            if qualifier in defined:
                continue
            if qualifier in namespaces and name in namespaces[qualifier] and defined.get(name) == 1:
                pass
            elif qualifier in standard and name in context.standard_names and name not in defined:
                pass
            else:
                continue
            edits = []
            for other_file, other in sorted(context.indices.items()):
                sig = other.significant
                for m in range(len(sig) - 2):
                    if (sig[m].upper == qualifier and sig[m + 1].text == "." and sig[m + 2].upper == name
                            and (m == 0 or sig[m - 1].text != ".")):
                        edits.append(Edit(other_file, sig[m].start, sig[m + 2].start, ""))
            return Proposal(self.spec, (self.spec.id, qualifier, name), edits, note=f"{qualifier}.{name}")
        return None
