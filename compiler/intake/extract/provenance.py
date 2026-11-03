"""Record which original containers and members each extracted file came from.

Extractors call :func:`register` when they write an output. Source references
are *chains*: a path relative to the corpus root, followed by ``!member`` for
each nested archive level (``a.zip!dir/b.zip!dir/c.EXP``). The worker process
runs with the corpus root as its working directory, so ``str(path)`` of every
original file is already such a relative POSIX path.

:mod:`compiler.intake.extract.pou_index` turns these file-level records into
POU-level provenance after extraction.
"""

from __future__ import annotations

import json
from pathlib import Path

OUTPUTS: list[dict] = []


def register(
    path: Path,
    sources,
    *,
    archive: str | None = None,
    prefix: str = "",
    pous: list[dict] | None = None,
) -> None:
    """Record the original sources of one written output file.

    ``archive`` is the chain of the archive that member names are relative to;
    ``prefix`` is a directory below the corpus root that relative paths share.
    ``pous`` lists POU-level origins (``name``, ``kind``, ``source``,
    ``element``) for extractors that know them while writing.
    """
    chains = []
    for source in sources:
        source = prefix + str(source)
        if archive and not source.startswith(archive):
            source = archive + "!" + source
        chains.append(source)
    record = {"file": str(path), "sources": list(dict.fromkeys(chains))}
    if pous:
        record["pous"] = pous
    OUTPUTS.append(record)


def dump(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(OUTPUTS, key=lambda item: item["file"])
    destination.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
