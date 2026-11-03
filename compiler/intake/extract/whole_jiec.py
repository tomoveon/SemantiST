"""Expand authoritative Jiec sources and round-trip official XML into whole-library ST."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
import hashlib
import re
import subprocess

from .extractor_native import mask
from . import provenance

# Configured by the extraction driver: HERE receives the report, BASE holds the
# unpacked JiecLib/JiecUnit sources, EXE is the official jiecc executable.
HERE = None
BASE = None
EXE = None
OUT = None
WORK = None
# Corpus chains of the JiecLib and JiecUnit source archives, for provenance.
SOURCE_ARCHIVES = {}


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def public_entries():
    entries = []
    source = BASE / "lib/src"
    for folder in sorted(p for p in source.rglob("*") if p.is_dir()):
        files = [p for p in folder.glob("*.iec") if "template" not in p.stem]
        if not files:
            continue
        umbrella = folder / (folder.name + ".iec")
        if umbrella.exists():
            entries.append(umbrella)
        else:
            entries.extend(sorted(files))
    return entries


def run_one(target, family, entries):
    root = BASE / ("lib" if family == "JiecLib" else "unit")
    folder = WORK / target / family
    folder.mkdir(parents=True, exist_ok=True)
    wrapper = root / "whole_library_entry.iec"
    # One immutable wrapper per family, prepared before concurrent processes.
    xml = folder / "library.xml"
    st = folder / "library.st"
    cmd = [str(EXE), str(wrapper), "-I" + str(root / "src"), "-I" + str(root),
           "-I" + str(BASE / "unit"), "-I" + str(root / "sys"), "-t", target, "-o", str(xml)]
    r = subprocess.run(cmd, cwd=root, capture_output=True, timeout=120)
    if r.returncode:
        return target, family, None, r.stderr.decode("utf8", "replace")
    tree = ET.fromstring(xml.read_bytes())
    generated_terminators = 0
    if target == "keyence":
        # Jiecc adds an XML-only END/ENDH ladder trailer for every KEYENCE POU.
        # The source collections contain no LD/FBD bodies. Verify that exact
        # transport trailer before omitting it from the extracted ST text.
        for parent in tree.iter():
            for child in list(parent):
                if local(child) != "BodyContent" or child.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type") == "ST":
                    continue
                assert child.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type") == "LD"
                blocks = [n.attrib.get("typeName") for n in child.iter() if local(n) == "FbdObject"]
                assert blocks == ["END", "ENDH"]
                assert len([n for n in child if local(n) == "Rung"]) == 2
                allowed = {"BodyContent", "Rung", "LdObject", "FbdObject", "ConnectionPointOut", "InputVariables", "InputVariable", "ConnectionPointIn", "Connection", "OutputVariables", "OutputVariable"}
                assert all(local(n) in allowed for n in child.iter())
                parent.remove(child)
                generated_terminators += 1
        xml = folder / "source_st_only.xml"
        ET.register_namespace("", "www.iec.ch/public/TC65SC65BWG7TF10")
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        ET.ElementTree(tree).write(xml, encoding="utf8", xml_declaration=True)
    r2 = subprocess.run([str(EXE), "-d", str(xml), "--retro_caps", "-o", str(st)],
                        cwd=root, capture_output=True, timeout=120)
    if r2.returncode:
        return target, family, None, r2.stderr.decode("utf8", "replace")
    text = st.read_text(encoding="utf-8-sig")
    bodies = [n.text or "" for n in tree.iter() if local(n) == "ST"]
    for body in bodies:
        assert not body.strip() or body.strip() in text, "Decompiler did not preserve a source body"
    active = mask(text)
    assert not re.search(r"\{\s*#|@@|__VA_ARGS__", active), "Unexpanded macro in output"
    assert not re.search(r"<(?:Rung|LdObject|FbdObject|ConnectionPoint)", active), "Graphical XML leaked into ST"
    # All POU signatures come from the target XML, not invented declarations.
    names = [n.attrib["name"] for n in tree.iter() if local(n) in ("Function", "FunctionBlock", "Program")]
    assert len(names) == len(set(n.casefold() for n in names)), "Conflicting POU declarations"
    for name in names:
        assert re.search(r"\b(?:FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+" + re.escape(name) + r"\b", active, re.I)
    if not bodies:
        return target, family, None, "No actual ST body after official expansion"
    # Jiecc's body delimiters are comments; omit the delimiter-only lines.
    text = re.sub(r"(?im)^\s*\(\*\s*\{(?:ST|END)\}\s*\*\)\s*$", "", text)
    header = f"(* {family}: complete public source collection; target {target}.\n   Macro expansion and target XML conversion: official Jiecc 8.0.0.\n   Source includes: {len(entries)}; POU objects: {len(names)}. *)\n\n"
    vendor = "18_Omron_Sysmac" if target == "omron" else "20_KEYENCE_KV"
    path = OUT / vendor / ".st文件" / f"{family}_{target}.st"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + text.strip() + "\n", encoding="utf8", newline="\n")
    provenance.register(path, [SOURCE_ARCHIVES.get(family, family)])
    info = dict(path=path.relative_to(OUT).as_posix(), inputs=len(entries), pous=len(names), bodies=len(bodies), bytes=path.stat().st_size,
                generated_keyence_xml_only_terminators_omitted=generated_terminators)
    return target, family, info, r.stderr.decode("utf8", "replace")


def main(selection=()):
    WORK.mkdir(exist_ok=True)
    entries = public_entries()
    (BASE / "lib/whole_library_entry.iec").write_text("\n".join('{#include "' + p.relative_to(BASE / "lib").as_posix() + '"}' for p in entries), encoding="utf8")
    unit_entries = [BASE / "unit/jiecunit.iec"]
    (BASE / "unit/whole_library_entry.iec").write_text('{#include "jiecunit.iec"}\n', encoding="utf8")
    jobs = [(target, family, paths) for target in ("omron", "keyence") for family, paths in (("JiecLib", entries), ("JiecUnit", unit_entries))
            if not selection or family in selection or target in selection]
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda args: run_one(*args), jobs):
            rows.append(result)
            print(result[:3], result[3][:900], flush=True)
    with (HERE / "jiec_extraction_report.txt").open("a", encoding="utf8") as report:
        report.write("\n\n".join(repr(r) for r in rows) + "\n")
    assert all(r[2] is not None for r in rows), "One or more complete macro libraries could not be expanded"

