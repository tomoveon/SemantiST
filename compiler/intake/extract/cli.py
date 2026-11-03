"""Run the whole-library extractors and compare them with the collected sources.

``run`` writes everything below a new work directory outside the corpus:

* ``outputs/<vendor>/.st文件/*.st`` -- the extracted libraries;
* ``provenance/*.json`` -- per extractor group, the containers each output
  file came from;
* ``pou-index.json`` -- POU-level provenance (see :mod:`.pou_index`);
* ``extraction.json`` -- groups run, fixed inputs, curation applied;
* ``raw-inventory.json`` -- raw ``.library`` containers and which are not
  ingested (compressed or encrypted format);
* ``logs/`` -- the extractors' own diagnostic records.

``compare`` checks a run against ``rusty_compatibility/*/.st文件`` and explains
every difference it can attribute (see :mod:`.compare`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from . import naming

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[2]
PARTS = ("xml", "industrial", "legacy", "pcworx", "native", "other", "sysmac", "jiec")
CURATION = PACKAGE / "curation.json"


def unpack_source(archive: Path, destination: Path) -> None:
    """Unpack a GitHub source archive below ``destination`` without its root."""
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            rel = PurePosixPath(item.filename)
            if rel.is_absolute() or ".." in rel.parts or any(":" in p or "\\" in p for p in rel.parts):
                raise ValueError("Unsafe source archive member: " + item.filename)
            if len(rel.parts) < 2:
                continue
            path = destination.joinpath(*rel.parts[1:])
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as out:
                    out.write(z.read(item))


def worker(part: str, source: Path, work: Path, jiecc: Path | None) -> None:
    """Run one extractor group; called in a fresh process per group.

    Every module is imported before changing into the corpus root, so the
    original files are addressed by paths relative to that root. Those relative
    paths are what output headers and provenance records contain.
    """
    from . import provenance
    from . import (whole_industrial, whole_jiec, whole_legacy_native, whole_native_vendors,
                   whole_other_native, whole_pcworx_exports, whole_sysmac, whole_xml)

    out = work / "outputs"
    logs = work / "logs"
    out.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    base = Path(".")
    os.chdir(source)

    def configure_native() -> None:
        whole_native_vendors.BASE, whole_native_vendors.SOURCE, whole_native_vendors.OUT = logs, base, out

    def configure_legacy() -> None:
        whole_legacy_native.BASE, whole_legacy_native.WORK, whole_legacy_native.OUT = base, logs, out

    if part == "xml":
        whole_xml.BASE, whole_xml.HERE = base, work
        audit = []
        for vendor in whole_xml.VENDORS:
            result = whole_xml.Builder(vendor).run()
            audit.append(result)
            print(vendor, json.dumps(dict(sorted(result["stats"].items()))), flush=True)
        (logs / "whole_xml_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    elif part == "industrial":
        whole_industrial.BASE, whole_industrial.WORK, whole_industrial.OUT = base, logs, out
        whole_industrial.main()
    elif part == "legacy":
        configure_legacy()
        whole_legacy_native.main()
    elif part == "pcworx":
        configure_legacy()
        whole_pcworx_exports.main()
    elif part == "native":
        configure_native()
        whole_native_vendors.main()
    elif part in ("other", "sysmac"):
        configure_native()
        module = whole_other_native if part == "other" else whole_sysmac
        module.BASE, module.OUT = logs, out
        module.main()
    elif part == "jiec":
        if jiecc is None or not jiecc.is_file():
            raise ValueError("Macro extraction needs --jiecc pointing to official Jiecc 8.0.0 jiecc.exe")
        if not (jiecc.parent / "jiepp.exe").is_file():
            raise ValueError("Keep jiepp.exe beside jiecc.exe from the official distribution")
        source_root = base / "18_Omron_Sysmac" / "原始库文件" / "源码"
        short = work / "jiec"
        for pattern, folder, family in (("jieclib_*.zip", "lib", "JiecLib"), ("jiecunit_*.zip", "unit", "JiecUnit")):
            matches = sorted(source_root.glob(pattern))
            if len(matches) != 1:
                raise ValueError("Expected exactly one matching source ZIP: " + pattern)
            unpack_source(matches[0], short / folder)
            whole_jiec.SOURCE_ARCHIVES[family] = str(matches[0])
        whole_jiec.HERE, whole_jiec.BASE, whole_jiec.EXE = logs, short, jiecc
        whole_jiec.OUT, whole_jiec.WORK = out, work / "jiec_converted"
        whole_jiec.main()
    provenance.dump(work / "provenance" / f"{part}.json")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header_source(path: Path) -> str | None:
    """Return the container chain in a legacy output's first-line header."""
    with path.open(encoding="utf-8") as handle:
        first = handle.readline().rstrip("\n")
    prefix = "(* Original library/project: "
    if first.startswith(prefix) and first.endswith(" *)"):
        return first[len(prefix) : -3]
    return None


def apply_curation(out: Path, curation: dict) -> list[dict]:
    """Apply the recorded renames and merges; return what was done.

    These steps were performed by hand after the original extraction. They are
    recorded in ``curation.json`` so a run reproduces the collected file names
    and the merged collection exactly.
    """
    applied: list[dict] = []
    by_source: dict[tuple[str, str], Path] = {}
    for path in sorted(out.glob("*/.st文件/*.st")):
        source = header_source(path)
        if source is not None:
            by_source[(path.parts[-3], source)] = path
    for merge in curation.get("merges", []):
        members = [by_source.get((merge["vendor"], chain)) for chain in merge["members"]]
        if any(member is None for member in members):
            raise SystemExit(f"curation merge {merge['name']}: member output missing")
        target = out / merge["vendor"] / ".st文件" / merge["name"]
        if target.exists():
            raise SystemExit(f"curation merge target exists: {target}")
        text = merge["header"] + "\n" + "\n".join(m.read_text(encoding="utf-8") for m in members)
        target.write_text(text, encoding="utf-8", newline="\n")
        for member in members:
            member.unlink()
        applied.append({"action": "merge", "vendor": merge["vendor"], "name": merge["name"],
                        "members": [m.name for m in members], "reason": merge["reason"]})
    for rename in curation.get("renames", []):
        path = by_source.get((rename["vendor"], rename["source"]))
        if path is None:
            raise SystemExit(f"curation rename source not produced: {rename['source']}")
        target = path.with_name(rename["name"])
        if target != path:
            if naming.exists(target):
                raise SystemExit(f"curation rename target exists: {target}")
            path.rename(target)
        applied.append({"action": "rename", "vendor": rename["vendor"], "from": path.name,
                        "to": rename["name"], "reason": rename["reason"]})
        by_source[(rename["vendor"], rename["source"])] = target
    prefix = "(* Original library/project: "
    for header in curation.get("headers", []):
        path = by_source.get((header["vendor"], header["source"]))
        if path is None or path.name != header["name"]:
            raise SystemExit(f"curation header target not produced: {header['name']}")
        text = path.read_text(encoding="utf-8")
        first, rest = text.split("\n", 1)
        path.write_text(prefix + header["header_source"] + " *)\n" + rest, encoding="utf-8", newline="\n")
        applied.append({"action": "header", "vendor": header["vendor"], "name": header["name"],
                        "reason": header["reason"]})
    return applied


def copy_fixed_inputs(out: Path, reference: Path, curation: dict, produced_parts: set[str]) -> list[dict]:
    """Copy collected outputs that cannot be regenerated by this run."""
    copied: list[dict] = []
    for item in curation.get("fixed_inputs", []):
        if item.get("part") and item["part"] in produced_parts:
            continue
        origin = reference / item["vendor"] / ".st文件" / item["name"]
        if not origin.is_file():
            raise SystemExit(f"fixed input missing in the reference corpus: {origin}")
        target = out / item["vendor"] / ".st文件" / item["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise SystemExit(f"fixed input collides with an extracted output: {target}")
        shutil.copyfile(origin, target)
        copied.append({"vendor": item["vendor"], "name": item["name"], "sha256": sha256(target),
                       "origin": str(origin.relative_to(reference)), "reason": item["reason"]})
    return copied


def run(args: argparse.Namespace) -> int:
    source = args.source.resolve(strict=True)
    if not (source / "01_CODESYS3" / "原始库文件").is_dir():
        raise SystemExit("--source is not the collected corpus root")
    work = args.work_dir.resolve()
    if work == source or work.is_relative_to(source):
        raise SystemExit("--work-dir must lie outside the corpus")
    if work.exists() and any(work.iterdir()):
        raise SystemExit("--work-dir must be new or empty")
    parts = [part for part in PARTS if part in args.parts]
    if "pcworx" in parts and "legacy" not in parts:
        raise SystemExit("pcworx completion must run together with legacy")
    if "jiec" in parts and not args.jiecc:
        raise SystemExit("jiec needs --jiecc; without it the collected Jiec outputs are used as fixed inputs")
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONHASHSEED="0")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    for part in parts:
        command = [sys.executable, "-B", "-m", "compiler.intake.extract", "worker", "--part", part,
                   "--source", str(source), "--work-dir", str(work)]
        if args.jiecc:
            command += ["--jiecc", str(args.jiecc.resolve())]
        print("extractor group: " + part, flush=True)
        (work / "logs").mkdir(exist_ok=True)
        with (work / "logs" / f"{part}.log").open("wb") as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise SystemExit(f"{part} failed; see {work / 'logs' / (part + '.log')}")
    curation = json.loads(CURATION.read_text(encoding="utf-8"))
    out = work / "outputs"
    applied = apply_curation(out, curation)
    reference = (args.reference or source).resolve()
    fixed = copy_fixed_inputs(out, reference, curation, set(parts))
    payloads = sorted(p for p in out.rglob("*") if p.is_file())
    if any(p.suffix.lower() != ".st" for p in payloads):
        raise SystemExit("unexpected non-ST file in the output directory")

    from .pou_index import build_index

    index = build_index(out, work / "provenance", source, fixed, applied)
    (work / "pou-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                         encoding="utf-8")
    from .inventory import library_inventory

    inventory = library_inventory(source)
    (work / "raw-inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    summary = {
        "schema_version": "semantist.extraction/1.0.0",
        "parts": parts,
        "outputs": len(payloads),
        "curation": applied,
        "fixed_inputs": fixed,
        "raw_libraries_not_ingested": inventory["unique_files"].get("compressed_or_encrypted", 0),
        "files": {str(p.relative_to(out)): sha256(p) for p in payloads},
    }
    (work / "extraction.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    print(f"extracted {len(payloads)} files into {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m compiler.intake.extract", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="extract the whole corpus into a new work directory")
    run_parser.add_argument("--source", type=Path, default=ROOT / "rusty_compatibility",
                            help="corpus root containing the vendor directories")
    run_parser.add_argument("--work-dir", type=Path, required=True)
    run_parser.add_argument("--parts", nargs="+", choices=PARTS, default=[p for p in PARTS if p != "jiec"])
    run_parser.add_argument("--jiecc", type=Path, help="official Jiecc 8.0.0 jiecc.exe (Windows only)")
    run_parser.add_argument("--reference", type=Path,
                            help="collected corpus providing fixed inputs (default: --source)")
    worker_parser = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--part", choices=PARTS, required=True)
    worker_parser.add_argument("--source", type=Path, required=True)
    worker_parser.add_argument("--work-dir", type=Path, required=True)
    worker_parser.add_argument("--jiecc", type=Path)
    compare_parser = sub.add_parser("compare", help="compare a run with the collected sources")
    compare_parser.add_argument("--work-dir", type=Path, required=True)
    compare_parser.add_argument("--reference", type=Path, default=ROOT / "rusty_compatibility")
    compare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args)
    if args.command == "worker":
        worker(args.part, args.source.resolve(strict=True), args.work_dir.resolve(strict=True),
               args.jiecc.resolve() if args.jiecc else None)
        return 0
    from .compare import compare

    return compare(args.work_dir.resolve(), args.reference.resolve(), args.output)
