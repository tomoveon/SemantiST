"""Rules for Siemens SIMATIC AX (ST with namespaces and classes)."""

from __future__ import annotations

from ...diagnostics import Located
from ...structure import MARKER
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec
from .codesys import as_comment

AX = frozenset({"simatic-ax"})
VAR_SECTIONS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_EXTERNAL", "VAR_GLOBAL"}
ACCESS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL"}
DECLARING = {"CLASS", "INTERFACE", "FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "TYPE", "CONFIGURATION"}


def mangle(qualified: str) -> str:
    namespace, _, name = qualified.rpartition(".")
    return namespace.replace(".", "_") + "__" + name if namespace else name


def _dotted(tokens, i: int) -> tuple[list[str], int]:
    """Dotted name from ``tokens[i]``; a typed-literal prefix (``Enum#``) ends it."""
    parts = []
    while i < len(tokens) and (tokens[i].is_word or tokens[i].kind == "cast_prefix"):
        if tokens[i].kind == "cast_prefix":
            parts.append(tokens[i].text[:-1])
            return parts, i + 1
        parts.append(tokens[i].text)
        i += 1
        if i < len(tokens) and tokens[i].text == ".":
            i += 1
        else:
            break
    return parts, i


class NamespaceFlatten(Rule):
    spec = RuleSpec(
        id="ax-namespace-flatten",
        version=2,
        title="NAMESPACE/USING -> one flat name space with mangled names (NS.Name -> NS__Name)",
        level="L2",
        dialects=AX,
        codes=frozenset({"E007"}),
        premise="Every declaration inside NAMESPACE N is the symbol N.Name; a reference resolves to the "
                "current namespace and its parents, then to the namespaces imported by USING in the same "
                "source file, then to the global namespace.",
        not_applicable="References to namespaces outside the compiled project (Siemens.Simatic.*): left "
                       "unchanged and reported as unresolved; a short name that is also declared as a variable "
                       "or member is not resolved as a type.",
        equivalence="Each declaration gets one fresh project-wide name and every reference that the AX "
                    "scoping rules bind to it is rewritten to that name, so bindings are unchanged. NAMESPACE, "
                    "END_NAMESPACE and USING lines are kept as comments.",
        probe="ax-namespace-flatten",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.upper not in {"NAMESPACE", "USING", "END_NAMESPACE"}:
            return None
        definitions: dict[str, str] = {}
        variables: set[str] = set()
        for index in context.indices.values():
            for unit in index.units:
                if unit.namespace and unit.name and unit.kind in DECLARING | {"VAR_GLOBAL"}:
                    definitions[unit.qualified_name.upper()] = mangle(unit.qualified_name)
            sig = index.significant
            section = False
            for k, item in enumerate(sig):
                word = item.upper
                if word in VAR_SECTIONS or word == "STRUCT":
                    section = True
                elif word in {"END_VAR", "END_STRUCT"}:
                    section = False
                elif section and item.is_word and k + 1 < len(sig) and sig[k + 1].text in {":", ","}:
                    variables.add(word)
        edits: list[Edit] = []
        for name, index in sorted(context.indices.items()):
            edits += self._rewrite(name, index, definitions, variables)
        if not edits:
            return None
        return Proposal(self.spec, (self.spec.id,), edits, note=f"{len(definitions)} namespaced declarations",
                        renames={q: m for q, m in definitions.items()})

    def _rewrite(self, file, index, definitions, variables) -> list[Edit]:
        sig = index.significant
        markers = sorted(t.start for t in index.tokens if t.kind == "comment" and MARKER.match(t.text))
        edits: list[Edit] = []
        stack: list[str] = []
        usings: list[str] = []
        marker_index = 0
        i = 0
        while i < len(sig):
            token = sig[i]
            while marker_index < len(markers) and markers[marker_index] < token.start:
                usings = []  # USING applies to one source file
                marker_index += 1
            word = token.upper
            if word == "NAMESPACE" and token.is_word:
                parts, j = _dotted(sig, i + 1)
                stack.append(".".join(parts))
                edits.append(Edit(file, token.start, sig[j - 1].end,
                                  as_comment(index.text[token.start:sig[j - 1].end])))
                i = j
                continue
            if word == "END_NAMESPACE":
                if stack:
                    stack.pop()
                end = sig[i + 1].end if i + 1 < len(sig) and sig[i + 1].text == ";" else token.end
                edits.append(Edit(file, token.start, end, as_comment(index.text[token.start:end])))
                i += 1
                continue
            if word == "USING" and token.is_word:
                parts, j = _dotted(sig, i + 1)
                usings.append(".".join(parts))
                end = sig[j].end if j < len(sig) and sig[j].text == ";" else sig[j - 1].end
                edits.append(Edit(file, token.start, end, as_comment(index.text[token.start:end])))
                i = j + 1
                continue
            literal = token.kind == "cast_prefix"  # NS.Enum#Value, Enum#Value
            if (token.is_word or literal) and (i == 0 or sig[i - 1].text != "."):
                current = ".".join(stack)
                parts, j = _dotted(sig, i)
                replaced = False
                # Qualified reference: longest prefix naming a declaration.
                for k in range(len(parts), 1, -1):
                    candidates = [".".join(parts[:k])]
                    if current:
                        candidates.append(current + "." + ".".join(parts[:k]))
                    target = next((definitions[c.upper()] for c in candidates if c.upper() in definitions), None)
                    if target:
                        last = i + 2 * (k - 1)
                        suffix = "#" if sig[last].kind == "cast_prefix" else ""
                        edits.append(Edit(file, token.start, sig[last].end, target + suffix))
                        i = last + 1
                        replaced = True
                        break
                if replaced:
                    continue
                short = token.text[:-1] if literal else token.text
                if literal or word not in variables and not (i + 1 < len(sig) and sig[i + 1].text == ":="
                                                             and i > 0 and sig[i - 1].text in {"(", ","}):
                    scopes = []
                    namespace = current
                    while namespace:
                        scopes.append(namespace)
                        namespace = namespace.rpartition(".")[0]
                    scopes += usings
                    target = next((definitions[(s + "." + short).upper()] for s in scopes
                                   if (s + "." + short).upper() in definitions), None)
                    if target and target != short:
                        edits.append(Edit(file, token.start, token.end, target + ("#" if literal else "")))
            i += 1
        return edits

