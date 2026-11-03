"""Extract real native library/project boundaries; never compiler test fragments.

JSON is read only as the upstream STruCpp library manifest. Delivered output
contains ST files exclusively. Logs and validation stay beside this script.
"""
import json
import hashlib
import re
import zipfile
from collections import Counter
from pathlib import PurePosixPath
from . import whole_native_vendors as common
from . import provenance

BASE, OUT = common.BASE, common.OUT
LOG = []
COUNTS = Counter()
RETAINED = []
WRITTEN = set()


def write(vendor, name, members, description, archive=None):
    """members is a sequence of original member-name/text tuples."""
    chunks = [common.comment(description)]
    for member, text in members:
        assert re.search(r"\w", common.mask_comments(text)), member
        chunks += [common.member_tag(member), text.strip()]
    text = "\n\n".join(chunks)+"\n"
    path = OUT/vendor/".st文件"/(name+".st")
    assert path not in WRITTEN, path
    WRITTEN.add(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    provenance.register(path, [member for member,_ in members], archive=str(archive) if archive else None)
    assert all(body.strip() in text for _,body in members)
    # Whole-library concatenation must not duplicate top-level POU definitions.
    definitions = re.findall(r"(?im)^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+(\w+)\b",common.mask_comments(text))
    assert len(definitions)==len(set(definitions)),(path,definitions)
    COUNTS[vendor]+=1
    RETAINED.extend((str(path),name) for name,_ in members)
    LOG.append(f"OUTPUT {vendor}/{name}.st: {len(members)} original source files, {len(definitions)} POU definitions")
    LOG.extend("  SOURCE "+member for member,_ in members)


def matiec(vendor):
    seen_libraries=set()
    for archive in common.archives(vendor):
        with zipfile.ZipFile(archive) as z:
            names=set(z.namelist())
            entries=[n for n in names if n.endswith("/lib/standard_FB.txt")]
            for entry in sorted(entries):
                libdir=PurePosixPath(entry).parent
                wrapper=common.decode(z.read(entry))
                includes=re.findall(r'\{#include\s+"([^\"]+)"\s*\}',wrapper)
                parts=[]
                for inc in includes:
                    member=str(libdir/inc)
                    if member not in names:raise ValueError("Missing MATIEC standard FB source "+member)
                    if inc in ("communication_blocks.txt", "sm_cards.txt", "synergy.txt", "SL-RP4.txt"):
                        LOG.append(f"SKIP {vendor}/{archive.name}!{member}: runtime/hardware dummy ST declaration; implementation is in C")
                        continue
                    body=common.decode(z.read(member))
                    assert re.search(r"(?im)^\s*FUNCTION_BLOCK\s+",common.mask_comments(body)),member
                    parts.append((member,body))
                assert len(parts)==11,(entry,len(parts))
                digest=hashlib.sha256("\n".join(t for _,t in parts).encode("utf-8")).hexdigest()
                if digest in seen_libraries:
                    LOG.append(f"SKIP duplicate {vendor}/{archive.name}!{entry}: 11 implemented FB source texts identical to existing library output")
                    continue
                seen_libraries.add(digest)
                # Preserve implementation code and native clock-access injection.
                # The wrapper's disable-generation markers govern the compiler's
                # intrinsic loading, rather than the extracted implementations.
                output_name = "native_matiec_standard_function_blocks"
                if vendor=="33_OpenPLC_v3":
                    output_name = "native_openplc_runtime_standard_function_blocks" if "/webserver/" in entry else "native_matiec_compiler_standard_function_blocks"
                write(vendor,output_name,parts,
                      f"MATIEC standard function-block source library; {archive.name}!{entry}; include manifest expanded; native C clock-access extensions retained",archive=archive)
                LOG.append(f"SKIP {vendor}/{archive.name}: standard_functions.txt declares compiler intrinsics with RETURN-only bodies; no ST implementation manufactured")
                LOG.append(f"EXCLUDE {vendor}/{archive.name}: compiler positive/negative tests, parser fixtures and code-generation fragments are not library source")
            if vendor=="33_OpenPLC_v3":
                for member in names:
                    if member.endswith("/webserver/st_files/webserver_program.st"):
                        body=common.decode(z.read(member))
                        assert "END_CONFIGURATION" in body and ":=" in body
                        write(vendor,"native_webserver_application",[(member,body)],
                              f"OpenPLC published webserver application, including program and configuration; {archive.name}",archive=archive)
                    if member.endswith("/webserver/st_files/blank_program.st"):
                        LOG.append(f"SKIP {vendor}/{archive.name}!{member}: blank application template")


def strucpp():
    vendor="34_OpenPLC_v4_STruCpp"
    for archive in common.archives(vendor):
        with zipfile.ZipFile(archive) as z:
            names=set(z.namelist())
            manifests=sorted(n for n in names if "/libs/sources/" in n and n.endswith("/library.json"))
            for member in manifests:
                manifest=json.loads(common.decode(z.read(member)))
                folder=str(PurePosixPath(member).parent)
                sources=sorted(n for n in names if n.startswith(folder+"/") and n.endswith(".st"))
                if not sources:
                    why="compiler/runtime intrinsics, explicitly no ST source" if manifest["name"]=="iec-std-functions" else "original CODESYS binary library requires dedicated extraction; no source invented"
                    LOG.append(f"SKIP {vendor}/{archive.name}!{member}: {why}")
                    continue
                # Place original type declarations before implementation files.
                sources.sort(key=lambda n:(not bool(re.search(r"(?im)^\s*TYPE\b",common.mask_comments(common.decode(z.read(n))))),n))
                parts=[(n,common.decode(z.read(n))) for n in sources]
                write(vendor,"native_"+manifest["name"],parts,
                      f"STruCpp whole source library {manifest['name']} {manifest.get('version','')}; {archive.name}!{member}; original source types and implementations retained",archive=archive)
            for member in sorted(names):
                if "/examples/" in member and member.endswith(".st"):
                    # Each upstream file is an independent complete example,
                    # not a collection of unrelated compiler test snippets.
                    body=common.decode(z.read(member))
                    masked=common.mask_comments(body)
                    if re.search(r"(?im)^\s*PROGRAM\s+",masked):
                        write(vendor,"native_application_"+PurePosixPath(member).stem,[(member,body)],
                              f"STruCpp complete published example application; {archive.name}!{member}",archive=archive)
                    else:
                        LOG.append(f"SKIP {vendor}/{archive.name}!{member}: standalone example function block without application boundary")
            if not manifests:
                LOG.append(f"SKIP {vendor}/{archive.name}: runtime C/C++ repository has no native ST source library manifests")
            LOG.append(f"EXCLUDE {vendor}/{archive.name}: tests, negative fixtures, generated previews and intrinsic stubs are not flattened into a library")


def festo():
    vendor="26_Festo"
    complete_categories={"actuators","motion","safety","sensors","types"}
    for archive in common.archives(vendor):
        with zipfile.ZipFile(archive) as z:
            parts=[]
            for member in sorted(z.namelist()):
                if "/knowledge/library/" not in member or not member.endswith(".st"):continue
                category=PurePosixPath(member).parent.name
                if category not in complete_categories:
                    LOG.append(f"SKIP {vendor}/{archive.name}!{member}: category contains incomplete simulated networking implementation (system/FTP or utilities/MQTT); whole category excluded")
                    continue
                body=common.decode(z.read(member))
                assert not re.search(r"Placeholder:|Simulated:",body,re.I),member
                parts.append((member,body))
            if not parts:continue
            parts.sort(key=lambda item:(PurePosixPath(item[0]).parent.name!="types",item[0]))
            assert len(parts)==23,(archive,len(parts))
            write(vendor,"third_party_examples_festo_codesys_complete_categories",parts,
                  f"Third-party CODESYS source examples from festo-codesys-mcp (not an official Festo library); complete actuator/motion/safety/sensor categories plus all 8 source types; {archive.name}",archive=archive)
            LOG.append(f"EXCLUDE {vendor}/{archive.name}: synthetic PLCopen fixtures and XML examples handled separately and never mixed with native source")


def main():
    matiec("32_Beremiz_MATIEC")
    matiec("33_OpenPLC_v3")
    strucpp()
    festo()
    LOG.append("COUNTS "+repr(dict(COUNTS)))
    LOG.append(f"Validated {len(RETAINED)} retained original source texts verbatim in their whole-library/project outputs; source declarations have no duplicate POU headers")
    (BASE/"other_native_extraction.log").write_text("\n".join(LOG)+"\n",encoding="utf-8")
    print(dict(COUNTS))
    print("Source texts verified:",len(RETAINED))

