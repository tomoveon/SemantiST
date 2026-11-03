"""Whole-library extraction for B&R, Mitsubishi CSV/IECST, and SIMATIC AX.

This module only writes ST payloads below outputs. Diagnostics remain adjacent
to this script. Original dialect and code are retained; no unavailable runtime
implementation is manufactured.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

from . import naming, provenance

# Configured by the extraction driver: BASE receives diagnostic logs, SOURCE is
# the corpus root and OUT the output directory.
BASE = None
SOURCE = None
OUT = None
LOG: list[str] = []
COUNTS: dict[str, int] = defaultdict(int)


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        value = data.decode("utf-16")
    else:
        try:
            value = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            value = data.decode("cp1252")
    return value.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")


def mask_comments(text: str) -> str:
    out = list(text)
    i = 0
    depth = 0
    quote = None
    while i < len(text):
        if depth:
            if text[i:i+2] == "(*":
                depth += 1
                out[i:i+2] = "  "
                i += 2
            elif text[i:i+2] == "*)":
                depth -= 1
                out[i:i+2] = "  "
                i += 2
            else:
                if text[i] != "\n": out[i] = " "
                i += 1
        elif quote:
            if text[i] == "$" and i + 1 < len(text):
                i += 2
            elif text[i] == quote:
                if i + 1 < len(text) and text[i+1] == quote: i += 2
                else: quote = None; i += 1
            else: i += 1
        elif text[i:i+2] == "(*":
            depth = 1
            out[i:i+2] = "  "
            i += 2
        elif text[i:i+2] == "//":
            end = text.find("\n", i)
            if end < 0: end = len(text)
            out[i:end] = " " * (end-i)
            i = end
        elif text[i] in "'\"":
            quote = text[i]
            i += 1
        else: i += 1
    return "".join(out)


def comment(value: str) -> str:
    return "(* " + value.replace("(*", "( *").replace("*)", "* )") + " *)"


def archives(vendor: str):
    for p in sorted((SOURCE/vendor/"原始库文件").rglob("*.zip")):
        if "_调查与历史记录" not in p.parts:
            yield p


def write_st(vendor: str, name: str, text: str, info: str, archive: Path | None = None):
    assert re.search(r"\w", mask_comments(text)), name
    safe = re.sub(r"[^\w.\-]+", "__", name).strip("_.")
    p = OUT/vendor/".st文件"/(safe+".st")
    # Two outputs must never share a name, also on case-insensitive hosts.
    assert not naming.exists(p), p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(comment(info) + "\n\n" + text.strip() + "\n", encoding="utf-8", newline="\n")
    members = re.findall(r"(?m)^\(\* Source: (.*) \*\)$", text)
    provenance.register(p, [m.split(" + ")[0] for m in members], archive=str(archive) if archive else None)
    COUNTS[vendor] += 1
    LOG.append(f"OUTPUT {vendor}/{p.name}: {len(text):,} chars; {info}")


def member_tag(member: str) -> str:
    return comment("Source: " + member)


POU_RE = re.compile(r"(?im)^\s*(FUNCTION_BLOCK|FUNCTION)\s+([A-Za-z_]\w*)\b[^\n]*\n")


def split_br_pous(text: str):
    masked = mask_comments(text)
    for start in POU_RE.finditer(masked):
        kind, name = start.group(1).upper(), start.group(2)
        end = re.search(r"\bEND_"+kind+r"\b", masked[start.end():], re.I)
        if not end:
            # Some upstream B&R OSCAT history comments contain unmatched nested
            # comment markers. Preserve those bytes; the explicit end-of-member
            # boundary remains available in the original separate source file.
            raw_ends = list(re.finditer(r"(?im)^\s*END_"+kind+r"\b", text[start.end():]))
            if not raw_ends: raise ValueError(f"Unclosed B&R {kind} {name}")
            end = raw_ends[-1]
            LOG.append(f"UPSTREAM B&R comment nesting anomaly: {kind} {name}; original comments preserved")
        close_start = start.end() + end.start()
        close_end = start.end() + end.end()
        yield kind, name, text[start.start():close_start].strip(), text[close_start:close_end]


def br_extract():
    vendor = "08_BR_Automation_Studio"
    for archive in archives(vendor):
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            for lby in sorted(n for n in names if n.lower().endswith(".lby")):
                doc = ET.fromstring(z.read(lby))
                folder = PurePosixPath(lby).parent
                refs = [(str(folder/(e.text or "").strip()).replace("\\", "/"))
                        for e in doc.iter() if e.tag.split("}")[-1] in ("File", "Object")
                        and (e.text or "").strip()]
                refs = [n for n in refs if n in names]
                st_refs = [n for n in refs if n.lower().endswith(".st")]
                if not st_refs:
                    LOG.append(f"SKIP B&R {archive.name}!{lby}: no ST implementation (binary/C library)")
                    continue
                decls = {}
                for n in refs:
                    if n.lower().endswith(".fun"):
                        for kind, name, declaration, end in split_br_pous(decode(z.read(n))):
                            key = (kind, name.casefold())
                            if key in decls: raise ValueError(f"Duplicate B&R declaration {lby} {name}")
                            decls[key] = (declaration, end)
                units = []
                merged_names = set()
                for n in st_refs:
                    src = decode(z.read(n))
                    if PurePosixPath(n).name == "SET_DATE.st":
                        broken_history = "(* chech for leap YEAR and add one DAY if true"
                        if broken_history in src:
                            # A surplus comment opener in upstream disabled old
                            # code otherwise absorbs later concatenated POUs.
                            src = src.replace(broken_history, "( * chech for leap YEAR and add one DAY if true", 1)
                            LOG.append(f"COMMENT FIX {archive.name}!{n}: neutralized one surplus opener inside disabled history; executable statements unchanged")
                    for kind, name, implementation, end in split_br_pous(src):
                        key = (kind, name.casefold())
                        if key not in decls: raise ValueError(f"No B&R declaration for {lby} {name}")
                        head = POU_RE.search(mask_comments(implementation))
                        assert head
                        body = implementation[head.end():].strip()
                        declaration, decl_end = decls[key]
                        units.append(member_tag(n)+"\n"+declaration+"\n\n"+body+"\n"+decl_end)
                        merged_names.add(key)
                missing = set(decls)-merged_names
                if missing:
                    LOG.append(f"B&R declared without ST body {archive.name}!{lby}: {sorted(missing)}")
                    # Do not silently replace external/C implementations with empty POUs.
                sections = []
                for n in refs:
                    if n.lower().endswith((".typ", ".var")):
                        src = decode(z.read(n))
                        if re.search(r"\w", mask_comments(src)):
                            # Library .var entries are exported constants/globals.
                            if n.lower().endswith(".var"):
                                src = re.sub(r"(?im)^(\s*)VAR(\s|$)", r"\1VAR_GLOBAL\2", src)
                            sections.append(member_tag(n)+"\n"+src)
                sections.extend(units)
                deps = [e.attrib.get("ObjectName", "") for e in doc.iter()
                        if e.tag.split("}")[-1] == "Dependency"]
                info = f"B&R whole library {folder.name}; {archive.name}!{lby}; merged {len(units)} POU implementations"
                if deps: info += "; dependencies: " + ", ".join(deps)
                write_st(vendor, archive.stem+"__"+folder.name, "\n\n".join(sections), info, archive)
            prgs = [n for n in names if n.lower().endswith(".prg")]
            if prgs:
                LOG.append(f"SKIP B&R {archive.name}: {len(prgs)} native task projects; shared lifecycle-local scope cannot be represented faithfully as a flat standard ST library")


def csv_rows(data: bytes):
    rows = list(csv.reader(io.StringIO(decode(data)), delimiter="\t"))
    head_i = next(i for i,r in enumerate(rows) if "Label Name" in r)
    head = rows[head_i]
    return [{k: (row[j] if j < len(row) else "").strip() for j,k in enumerate(head)}
            for row in rows[head_i+1:] if row and any(row)]


def csv_decl(rows, struct=False):
    groups = defaultdict(list)
    for r in rows:
        group = r.get("Class", "")
        if not struct and not group.startswith("VAR"): raise ValueError(f"Unknown CSV class {group}")
        group = "" if struct else group.replace("_CONSTANT", " CONSTANT")
        label, typ = r["Label Name"], r["Data Type"]
        if not label or not typ: raise ValueError(f"Incomplete CSV declaration {r}")
        init = " := " + r["Constant"] if r.get("Constant") else ""
        loc = " AT " + r["Address"] if r.get("Address") else ""
        line = f"    {label}{loc} : {typ}{init};"
        if r.get("Comment"): line += " " + comment(r["Comment"])
        if r.get("Device") and not loc: line += " " + comment("Device: "+r["Device"])
        groups[group].append(line)
    if struct: return "\n".join(groups[""])
    return "\n\n".join(g+"\n"+"\n".join(ls)+"\nEND_VAR" for g,ls in groups.items())


def function_return(name: str, body: str, documentation: str, labels):
    # Prefer explicitly stated return types in the original code comments.
    match = re.search(r"\bReturns?\s+(BOOL|WORD|DWORD|INT|DINT|REAL|TIME)\b", body[:800], re.I)
    if match: return match.group(1).upper(), "original implementation comment"
    # TimeControl manual explicitly defines POU return types per named section.
    chunks = re.split(r"(?m)^#{3,4}\s+", documentation)
    for chunk in chunks:
        title = chunk.split("\n",1)[0]
        if title.strip() == f"`{name}`":
            match = re.search(r"Return type:\s*\*\*(\w+)\*\*", chunk)
            if match: return match.group(1), "library manual POU properties"
    if name == "F_FLT10" and "`F_FLT10` returns `REAL`" in documentation:
        return "REAL", "library manual explicit return type"
    if name == "F_AM_DELAY_OUT" and "**Return value:** `TRUE`" in documentation:
        return "BOOL", "library manual boolean return value"
    if name == "MB_PORT_SETTINGS" and "`PortSettings` | INPUT  | `DWORD` | Return value of the `MB_PORT_SETTINGS`" in documentation:
        return "DWORD", "library manual explicitly typed function return"
    raise ValueError("No evidence for Mitsubishi return type: "+name)


def mitsubishi_extract():
    vendor = "19_Mitsubishi_GXWorks"
    for archive in archives(vendor):
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            libs = defaultdict(list)
            for n in sorted(names):
                m = re.search(r"/libraries/([^/]+)/POU/[^/]+\.iecst$", n, re.I)
                if m: libs[m.group(1)].append(n)
            for lib, members in libs.items():
                prefix = members[0].split("/POU/")[0]
                documentation = "\n".join(decode(z.read(n)) for n in names
                                          if n.startswith(prefix+"/") and n.endswith(".md")
                                          and PurePosixPath(n).name not in ("AGENT.md", "CHANGELOG.md"))
                unresolved = []
                for n in members:
                    stem = PurePosixPath(n).stem
                    if stem.startswith(("FB_", "PRG_")) or stem in ("MB_PROCESS_50", "L02_SCALE_AI"):
                        continue
                    pair = n[:-6]+".csv"
                    try:
                        function_return(stem, decode(z.read(n)), documentation, csv_rows(z.read(pair)))
                    except ValueError:
                        unresolved.append(stem)
                if unresolved:
                    LOG.append(f"SKIP Mitsubishi whole library {lib}: exported CSV omits function POU return properties and manuals do not declare return types for {', '.join(unresolved)}; result assignments alone are insufficient evidence")
                    for n in members: LOG.append(f"SKIP Mitsubishi member {archive.name}!{n}: whole-library boundary cannot be faithfully recovered")
                    continue
                prefix_csv = [n for n in sorted(names) if n.startswith(prefix+"/POU/") and n.endswith(".csv")]
                shared, test_globals = [], []
                for n in prefix_csv:
                    if n[:-4]+".iecst" in names: continue
                    stem = PurePosixPath(n).stem
                    rows = csv_rows(z.read(n))
                    if stem.startswith("ST_"):
                        typ = stem[3:]
                        # Confirm the actual structure type name in source declarations/manual.
                        all_csv = "\n".join(decode(z.read(c)) for c in prefix_csv)
                        if not re.search(r"\b"+re.escape(typ)+r"\b", all_csv+documentation):
                            raise ValueError("Unconfirmed CSV structure name: "+stem)
                        shared.append(member_tag(n)+f"\nTYPE\n{typ} : STRUCT\n"+csv_decl(rows, True)+"\nEND_STRUCT;\nEND_TYPE")
                    elif stem.startswith("GVL_"):
                        sec = member_tag(n)+"\n"+csv_decl(rows)
                        (test_globals if stem.endswith("_TEST") else shared).append(sec)
                    else: raise ValueError("Unknown standalone Mitsubishi CSV "+n)
                core, tests = [], []
                for n in members:
                    stem = PurePosixPath(n).stem
                    pair = n[:-6]+".csv"
                    if pair not in names: raise ValueError("Missing paired labels "+n)
                    body = decode(z.read(n)).strip()
                    labels = csv_rows(z.read(pair))
                    declarations = csv_decl(labels)
                    if stem.startswith("PRG_"):
                        kind, tail = "PROGRAM", ""
                    elif stem.startswith("FB_") or stem in ("MB_PROCESS_50", "L02_SCALE_AI"):
                        kind, tail = "FUNCTION_BLOCK", ""
                    else:
                        kind = "FUNCTION"
                        ret, evidence = function_return(stem, body, documentation, labels)
                        tail = " : "+ret
                        LOG.append(f"MITSUBISHI RETURN {lib}/{stem}: {ret} ({evidence})")
                    unit = member_tag(n+" + "+PurePosixPath(pair).name)+f"\n{kind} {stem}{tail}\n"+declarations+"\n\n"+body+f"\nEND_{kind}"
                    (tests if "TEST" in stem else core).append(unit)
                text = "\n\n".join(shared+core)
                info = f"GX Works 2 whole library {lib}; {archive.name}; CSV labels merged with IECST implementations"
                write_st(vendor, lib, text, info, archive)
                if tests:
                    # The complete test project embeds the library and its published test globals.
                    write_st(vendor, lib+"__test_project", "\n\n".join(shared+test_globals+core+tests),
                             info+"; complete published smoke-test project", archive)
            for n in sorted(names):
                if n.endswith(".sul") and PurePosixPath(n).parent.name not in libs:
                    LOG.append(f"SKIP Mitsubishi {archive.name}!{n}: only binary library, no paired source declarations/implementation")
                if n.endswith(".iecst") and "/libraries/" not in n:
                    LOG.append(f"SKIP Mitsubishi {archive.name}!{n}: standalone unpaired source outside known library boundary")


def ax_extract():
    vendor = "30_Siemens_SIMATIC_AX"
    for archive in archives(vendor):
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            manifests = {str(PurePosixPath(n).parent): n for n in names if n.endswith("/apax.yml")}
            packages = defaultdict(lambda: defaultdict(list))
            for n in sorted(names):
                if not n.lower().endswith(".st"): continue
                ancestors = [str(p) for p in PurePosixPath(n).parents]
                folder = next((p for p in ancestors if p in manifests), None)
                if folder is None:
                    LOG.append(f"SKIP AX {archive.name}!{n}: no apax package boundary")
                    continue
                body = decode(z.read(n))
                if not re.search(r"\w", mask_comments(body)):
                    LOG.append(f"SKIP AX {archive.name}!{n}: no active ST source")
                    continue
                rel = str(PurePosixPath(n).relative_to(folder))
                first = rel.split("/")[0].casefold()
                category = "tests" if first in ("test", "tests") else "source"
                packages[folder][category].append((n, body))
            for folder, cats in sorted(packages.items()):
                manifest = decode(z.read(manifests[folder]))
                pname = re.search(r"(?m)^name:\s*['\"]?([^\n'\"]+)", manifest)
                pname = pname.group(1).strip() if pname else PurePosixPath(folder).name
                dependency_block = re.search(r"(?ms)^dependencies:\s*\n((?:[ \t]+[^\n]*\n|\s*\n)*)", manifest)
                deps = re.findall(r"(?m)^\s+['\"]?(@[^'\":\s]+)['\"]?:\s*(.+)$", dependency_block.group(1)) if dependency_block else []
                version = re.search(r"(?m)^version:\s*(.+)$", manifest)
                info = f"SIMATIC AX whole apax package {pname}; version {version.group(1).strip() if version else 'unspecified'}; {archive.name}!{folder}"
                if deps: info += "; dependencies: " + ", ".join(a+" "+b for a,b in deps)
                relfolder = "/".join(PurePosixPath(folder).parts[1:]) or "root"
                outputname = archive.stem+"__"+relfolder
                source_units = cats.get("source", [])
                if source_units:
                    write_st(vendor, outputname, "\n\n".join(member_tag(n)+"\n"+t.strip() for n,t in source_units), info, archive)
                if cats.get("tests"):
                    # Preserve package test context without making every method a file.
                    all_units = source_units+cats["tests"]
                    write_st(vendor, outputname+"__test_project", "\n\n".join(member_tag(n)+"\n"+t.strip() for n,t in all_units),
                             info+"; published package source and tests", archive)


def main():
    br_extract()
    mitsubishi_extract()
    ax_extract()
    (BASE/"native_vendors_extraction.log").write_text("\n".join(LOG)+"\nCOUNTS "+repr(dict(COUNTS))+"\n",encoding="utf-8")
    print(dict(COUNTS))
    print("Diagnostic log:", BASE/"native_vendors_extraction.log")
