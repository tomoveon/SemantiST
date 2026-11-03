"""Run the intake over the whole extracted corpus.

    python3 -m compiler.intake.corpus_run --outputs artifacts/intake/extract/outputs \\
        --work artifacts/intake/libraries --jobs 64

Writes one intake directory per library (``<work>/<vendor>/<library>/``) and
``<work>/corpus.json`` with the per-library compatibility summaries.

``--stubs DIR`` adds ``DIR/<vendor>/<library>.st`` (environment stubs, see
:mod:`compiler.intake.stubs`) to a library's compatibility sources: the
second, approximate tier. The core results are runs without it.
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import corpus
from .library import intake


def library_directory(work: Path, vendor: str, path: Path) -> Path:
    return work / vendor / path.stem


def _job(arguments: tuple[str, str, str, str | None]) -> dict:
    vendor_name, path, work, stubs = arguments
    vendor = corpus.BY_DIRECTORY[vendor_name]
    path = Path(path)
    output = library_directory(Path(work), vendor_name, path)
    entry = {"vendor": vendor_name, "family": vendor.family, "priority": vendor.priority, "library": path.name}
    if vendor.dialect is None:
        entry.update({"status": "unsupported_family", "reason": vendor.note})
        return entry
    stub_file = Path(stubs) / vendor_name / f"{path.stem}.st" if stubs else None
    extra = [stub_file] if stub_file is not None and stub_file.is_file() else []
    if extra:
        entry["stubs"] = str(stub_file.name)
    try:
        report = intake(path, vendor, output, extra)
    except Exception as error:  # recorded, never hidden: the library counts as not ingested
        entry.update({"status": "intake_error", "error": f"{type(error).__name__}: {error}",
                      "traceback": traceback.format_exc()[-3000:]})
        return entry
    entry.update({"status": "done", "syntax": report["syntax"], "behaviour": report["behaviour"]["level"],
                  "rules_applied": report["rules_applied"], "dependencies": report["dependencies"]})
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--library", action="append", default=[], help="only these file names")
    parser.add_argument("--stubs", type=Path, help="per-library environment stub files (second tier)")
    args = parser.parse_args(argv)
    vendors = [v for v in corpus.VENDORS if not args.family or v.family in args.family]
    work = args.work.resolve()
    stubs = str(args.stubs.resolve()) if args.stubs else None
    jobs = [(vendor.directory, str(path.resolve()), str(work), stubs)
            for vendor, path in corpus.libraries(args.outputs, vendors)
            if not args.library or path.name in args.library]
    jobs.sort(key=lambda job: -Path(job[1]).stat().st_size)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        entries = list(pool.map(_job, jobs))
    index = work / "corpus.json"
    if (args.family or args.library) and index.is_file():
        # A partial run replaces only the libraries it ran.
        ran = {(e["vendor"], e["library"]) for e in entries}
        previous = json.loads(index.read_text(encoding="utf-8"))["libraries"]
        entries += [e for e in previous if (e["vendor"], e["library"]) not in ran]
    entries.sort(key=lambda e: (e["vendor"], e["library"]))
    work.mkdir(parents=True, exist_ok=True)
    (work / "corpus.json").write_text(json.dumps({"schema_version": "semantist.intake-corpus/1.0.0",
                                                  "libraries": entries}, ensure_ascii=False, indent=1) + "\n",
                                      encoding="utf-8")
    done = [e for e in entries if e["status"] == "done"]
    print(f"{len(done)}/{len(entries)} libraries ingested; errors: "
          f"{sum(1 for e in entries if e['status'] == 'intake_error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
