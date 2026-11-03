"""Clone families of POUs and libraries (design section 4.1).

A POU is reduced to its significant token sequence with comments, pragmas and
whitespace dropped, keywords upper-cased, and identifiers renamed by order of
first appearance (``$1``, ``$2`` ...), so copies that differ only in names,
layout or comments get the same *exact family* key. Families whose 4-gram
shingle sets have an estimated Jaccard similarity of at least
``NEAR_THRESHOLD`` (MinHash with banding) are merged into *near families*,
which absorbs small edits between releases.

Libraries are grouped the same way on their sets of near families: two
libraries whose family sets overlap by at least ``LIBRARY_THRESHOLD`` belong
to one library family. All hashing is deterministic (BLAKE2 with fixed keys).

    python3 -m compiler.intake.clones --outputs artifacts/intake/extract/outputs \\
        --output artifacts/intake/clones.json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import struct
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

from . import corpus
from .structure import split_units

POU_KINDS = {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS"}
SHINGLE = 4
PERMUTATIONS = 32
BANDS = 8
NEAR_THRESHOLD = 0.85
LIBRARY_THRESHOLD = 0.7
_MASK = (1 << 61) - 1


def normalized_tokens(text: str) -> list[str]:
    names: dict[str, str] = {}
    result = []
    for token in significant(tokenize_lossless(text)):
        if token.kind == "identifier":
            key = token.upper
            if key not in names:
                names[key] = f"${len(names) + 1}"
            result.append(names[key])
        elif token.kind in {"keyword", "symbol"}:
            result.append(token.upper)
        else:
            result.append(token.text.upper() if token.kind == "typed_literal" else token.text)
    return result


def _hash64(data: bytes, key: int) -> int:
    return struct.unpack("<Q", hashlib.blake2b(data, digest_size=8, key=key.to_bytes(8, "little")).digest())[0]


def signature(tokens: list[str]) -> tuple[int, ...]:
    shingles = {"\x1f".join(tokens[i:i + SHINGLE]).encode() for i in range(max(1, len(tokens) - SHINGLE + 1))}
    base = [_hash64(s, 0) for s in shingles]
    coefficients = [(_hash64(b"a", k) | 1, _hash64(b"b", k)) for k in range(1, PERMUTATIONS + 1)]
    return tuple(min(((a * x + b) & _MASK) for x in base) for a, b in coefficients)


class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic representative: the smaller key.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def estimated_jaccard(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def families(outputs: Path) -> dict:
    pous = []  # (vendor, family, library, kind, name, exact key)
    sequences: dict[str, list[str]] = {}
    for vendor, path in corpus.libraries(outputs):
        text = path.read_text(encoding="utf-8")
        tokens_all = tokenize_lossless(text)
        for unit in split_units(text, tokens_all):
            if unit.kind not in POU_KINDS:
                continue
            tokens = normalized_tokens(text[unit.start:unit.end])
            key = hashlib.blake2b("\x1e".join(tokens).encode(), digest_size=16).hexdigest()
            sequences.setdefault(key, tokens)
            pous.append((vendor.directory, vendor.family, path.name, unit.kind, unit.qualified_name, key))
    keys = sorted(sequences)
    signatures = {key: signature(sequences[key]) for key in keys}
    near = UnionFind(keys)
    rows = PERMUTATIONS // BANDS
    buckets: dict[tuple, list[str]] = collections.defaultdict(list)
    for key in keys:
        sig = signatures[key]
        for band in range(BANDS):
            buckets[(band, sig[band * rows:(band + 1) * rows])].append(key)
    for members in buckets.values():
        # All pairs in small buckets; large buckets against their first member.
        pairs = ([(a, b) for i, a in enumerate(members) for b in members[i + 1:]] if len(members) <= 40
                 else [(members[0], b) for b in members[1:]])
        for a, b in pairs:
            if near.find(a) != near.find(b) and estimated_jaccard(signatures[a], signatures[b]) >= NEAR_THRESHOLD:
                near.union(a, b)
    near_of = {key: near.find(key) for key in keys}
    libraries: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    library_family_of_vendor = {}
    for vendor, family, library, _, _, key in pous:
        libraries[(vendor, library)].add(near_of[key])
        library_family_of_vendor[(vendor, library)] = family
    library_keys = sorted(libraries)
    library_union = UnionFind(library_keys)
    by_family: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for lib in library_keys:
        for fam in libraries[lib]:
            by_family[fam].append(lib)
    for members in by_family.values():
        for other in members[1:]:
            a, b = libraries[members[0]], libraries[other]
            if len(a & b) / max(1, min(len(a), len(b))) >= LIBRARY_THRESHOLD:
                library_union.union(members[0], other)
    summary_by_family = collections.defaultdict(lambda: collections.Counter())
    exact_by_family = collections.defaultdict(set)
    near_by_family = collections.defaultdict(set)
    library_families_by_family = collections.defaultdict(set)
    for vendor, family, library, _, _, key in pous:
        summary_by_family[family]["pous"] += 1
        exact_by_family[family].add(key)
        near_by_family[family].add(near_of[key])
    for lib in library_keys:
        family = library_family_of_vendor[lib]
        summary_by_family[family]["libraries"] += 1
        library_families_by_family[family].add(library_union.find(lib))
    per_family = {}
    for family in sorted(summary_by_family):
        per_family[family] = {
            "libraries": summary_by_family[family]["libraries"],
            "library_families": len(library_families_by_family[family]),
            "pous": summary_by_family[family]["pous"],
            "exact_families": len(exact_by_family[family]),
            "near_families": len(near_by_family[family]),
        }
    return {
        "schema_version": "semantist.clone-families/1.0.0",
        "method": {"normalization": "significant tokens, identifiers renamed by first occurrence",
                   "shingle": SHINGLE, "minhash_permutations": PERMUTATIONS, "bands": BANDS,
                   "near_threshold": NEAR_THRESHOLD, "library_threshold": LIBRARY_THRESHOLD},
        "total": {
            "libraries": len(library_keys),
            "library_families": len({library_union.find(lib) for lib in library_keys}),
            "pous": len(pous),
            "exact_families": len(keys),
            "near_families": len(set(near_of.values())),
        },
        "per_family": per_family,
        "pou_families": [{"vendor": v, "library": lib, "kind": kind, "name": name, "exact": key[:16],
                          "near": near_of[key][:16]} for v, _, lib, kind, name, key in pous],
        "library_families": {f"{v}/{lib}": library_union.find((v, lib))[1] for v, lib in library_keys},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = families(args.outputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["total"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
