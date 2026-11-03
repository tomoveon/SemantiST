# -*- coding: utf-8 -*-
"""
Extract ST source code from a CODESYS .library file.

Run inside CODESYS:
    Tools -> Scripting -> Execute Script File...

Optional command line usage:
    CODESYS.exe --runscript="extract_codesys_library_functions.py" --scriptargs="<library>;<output>"

Why the first version could miss objects:
    1. Some CODESYS versions do not return all nested library nodes from
       project.get_children(True). This script performs its own recursive walk.
    2. POUs is a container name, not a guarantee that every child is a FUNCTION.
       It can also contain FUNCTION_BLOCK, PROGRAM, ACTION, METHOD, etc.
    3. Some text objects expose declaration/implementation documents without
       setting has_textual_declaration or has_textual_implementation cleanly.
       This script tries several access patterns.
"""

from __future__ import print_function

import os
import re
import sys
import traceback


LIBRARY_PATH = r"C:\Users\32846\CODESYS OSCAT Basic\3.3.5.0\oscat_basic_335.library"
OUTPUT_ST_PATH = r"D:\Industrial Control System\codex projects\oscat_basic.st"

# True  = only export IEC FUNCTION POUs.
# False = export every textual ST object found under "POUs" folders. This is
#         often better for vulnerability tools, because CODESYS POUs may include
#         FUNCTION_BLOCK, PROGRAM, METHOD, ACTION, etc.
ONLY_FUNCTIONS = False

# Keep extraction inside folders named POUs. Set to False if you also want data
# types, global variable lists, library metadata comments, and other text nodes.
ONLY_UNDER_POUS_FOLDER = True

# A sidecar inventory is useful when checking whether anything was skipped.
WRITE_INVENTORY = True
INVENTORY_PATH = r"D:\Industrial Control System\codex projects\oscat_basic_inventory.txt"
INVENTORY_GROUP_SIZE = 5
INVENTORY_GROUP_SEPARATOR = "-----"


FUNCTION_RE = re.compile(r"^\s*FUNCTION(?!_BLOCK)\b", re.IGNORECASE | re.MULTILINE)
POU_HEADER_RE = re.compile(
    r"^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|ACTION)\b",
    re.IGNORECASE | re.MULTILINE,
)
END_BY_KIND = {
    "FUNCTION": "END_FUNCTION",
    "FUNCTION_BLOCK": "END_FUNCTION_BLOCK",
    "PROGRAM": "END_PROGRAM",
    "METHOD": "END_METHOD",
    "ACTION": "END_ACTION",
}


try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str


def log(message):
    print("[extract-st] " + to_text(message))


def to_text(value):
    if value is None:
        return TEXT_TYPE("")
    if isinstance(value, TEXT_TYPE):
        return value
    try:
        return TEXT_TYPE(value)
    except UnicodeDecodeError:
        return value.decode("utf-8", "replace")


def normalize_newlines(text):
    return to_text(text).replace("\r\n", "\n").replace("\r", "\n")


def encode_file_text(text):
    return normalize_newlines(text).replace("\n", "\r\n").encode("utf-8")


def get_script_args():
    args = []
    for item in sys.argv[1:]:
        if not item:
            continue
        if item.startswith("--scriptargs="):
            item = item.split("=", 1)[1]
        parts = [p.strip().strip('"') for p in item.split(";") if p.strip()]
        args.extend(parts)

    library_path = args[0] if len(args) >= 1 else LIBRARY_PATH
    output_path = args[1] if len(args) >= 2 else OUTPUT_ST_PATH
    return os.path.abspath(library_path), os.path.abspath(output_path)


def get_name(obj):
    for attr in ("get_name",):
        try:
            return to_text(getattr(obj, attr)(False))
        except Exception:
            try:
                return to_text(getattr(obj, attr)())
            except Exception:
                pass
    try:
        return to_text(obj.name)
    except Exception:
        return to_text(obj)


def get_children(obj):
    for args in ((False,), tuple()):
        try:
            return list(obj.get_children(*args))
        except Exception:
            pass
    try:
        return list(obj.children)
    except Exception:
        return []


def walk_objects(root):
    stack = [(root, [])]
    seen = set()

    while stack:
        obj, parent_names = stack.pop()
        try:
            key = obj.guid
        except Exception:
            key = id(obj)
        if key in seen:
            continue
        seen.add(key)

        name = get_name(obj)
        path_names = parent_names + ([name] if name else [])
        yield obj, path_names

        children = get_children(obj)
        for child in reversed(children):
            stack.append((child, path_names))


def is_truthy_property(obj, name):
    try:
        value = getattr(obj, name)
        if callable(value):
            value = value()
        return bool(value)
    except Exception:
        return False


def get_text_document_text(doc):
    if doc is None:
        return ""

    # Common CODESYS text document API.
    for attr in ("text", "content"):
        try:
            value = getattr(doc, attr)
            if callable(value):
                value = value()
            return normalize_newlines(value)
        except Exception:
            pass

    # Some versions provide get_text().
    try:
        return normalize_newlines(doc.get_text())
    except Exception:
        pass

    # Fallback to line access.
    for count_attr in ("linecount", "line_count", "LineCount"):
        try:
            count = int(getattr(doc, count_attr))
            lines = []
            for idx in range(count):
                try:
                    lines.append(to_text(doc.get_line(idx)))
                except Exception:
                    lines.append(to_text(doc.GetLine(idx)))
            return normalize_newlines("\n".join(lines))
        except Exception:
            pass

    return ""


def read_document_attr(obj, attr_names, flag_names):
    texts = []

    # First honor CODESYS flags when they are present.
    for flag_name, attr_name in zip(flag_names, attr_names):
        if is_truthy_property(obj, flag_name):
            try:
                text = get_text_document_text(getattr(obj, attr_name))
                if text.strip():
                    texts.append(text)
            except Exception:
                pass

    # Then try direct access anyway. This avoids missing library objects whose
    # "has_textual_*" flags are absent or false although the document exists.
    for attr_name in attr_names:
        try:
            text = get_text_document_text(getattr(obj, attr_name))
            if text.strip() and text not in texts:
                texts.append(text)
        except Exception:
            pass

    return "\n".join(texts).strip()


def get_textual_parts(obj):
    declaration = read_document_attr(
        obj,
        ("textual_declaration", "declaration", "interface"),
        ("has_textual_declaration", "has_declaration", "has_interface"),
    )
    implementation = read_document_attr(
        obj,
        ("textual_implementation", "implementation"),
        ("has_textual_implementation", "has_implementation"),
    )

    return declaration.strip(), implementation.strip()


def detect_kind(declaration, implementation):
    text = (declaration or "") + "\n" + (implementation or "")
    match = POU_HEADER_RE.search(text)
    if match:
        return match.group(1).upper()
    return ""


def under_pous_folder(path_names):
    for name in path_names:
        if to_text(name).strip().upper() == "POUS":
            return True
    return False


def should_export(path_names, declaration, implementation):
    if ONLY_UNDER_POUS_FOLDER and not under_pous_folder(path_names):
        return False, "not under POUs"

    if not declaration.strip() and not implementation.strip():
        return False, "no ST text"

    if ONLY_FUNCTIONS and not FUNCTION_RE.search(declaration or ""):
        return False, "not FUNCTION"

    return True, ""


def open_library_project(library_path):
    if not os.path.isfile(library_path):
        raise IOError("Library file does not exist: " + library_path)

    # Reuse already opened project/library when possible.
    try:
        for project in projects.all:
            try:
                project_path = os.path.abspath(to_text(project.path)).lower()
                if project_path == library_path.lower():
                    log("Using already opened library: " + library_path)
                    return project, False
            except Exception:
                pass
    except Exception:
        pass

    # Open as non-primary so an already opened user project does not conflict.
    log("Opening library as non-primary project: " + library_path)
    try:
        return projects.open(path=library_path, primary=False), True
    except TypeError:
        return projects.open(library_path), True


def collect_st_objects(project):
    exported = []
    inventory = []

    for obj, path_names in walk_objects(project):
        path = "/".join([p for p in path_names if p])
        declaration, implementation = get_textual_parts(obj)
        kind = detect_kind(declaration, implementation)
        include, reason = should_export(path_names, declaration, implementation)

        if declaration.strip() or implementation.strip():
            inventory.append({
                "status": "EXPORT" if include else "SKIP",
                "reason": reason,
                "kind": kind or "?",
                "path": path,
                "decl_chars": len(declaration),
                "impl_chars": len(implementation),
            })

        if not include:
            continue

        exported.append({
            "name": path_names[-1] if path_names else get_name(obj),
            "path": path,
            "kind": kind or "ST",
            "declaration": declaration,
            "implementation": implementation,
        })

    exported.sort(key=lambda item: item["path"].lower())
    inventory.sort(key=lambda item: item["path"].lower())
    return exported, inventory


def has_end_marker(text, kind):
    marker = END_BY_KIND.get(kind.upper(), "")
    if not marker:
        return True
    return re.search(r"\b" + re.escape(marker) + r"\s*$", text, re.IGNORECASE) is not None


def render_item(item):
    declaration = normalize_newlines(item["declaration"]).strip()
    implementation = normalize_newlines(item["implementation"]).strip()

    parts = []
    parts.append("(* ========================================================================== *)")
    parts.append("(* Source object: " + item["path"] + " *)")
    parts.append("(* Object kind: " + item["kind"] + " *)")
    parts.append("(* ========================================================================== *)")

    if declaration:
        parts.append(declaration)
    if implementation:
        parts.append("")
        parts.append(implementation)

    combined = "\n".join(parts).rstrip()
    if item["kind"] in END_BY_KIND and not has_end_marker(combined, item["kind"]):
        combined += "\n" + END_BY_KIND[item["kind"]]
    return combined + "\n"


def write_output(output_path, items):
    folder = os.path.dirname(output_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)

    body = []
    body.append("(*")
    body.append("    Generated by extract_codesys_library_functions.py")
    body.append("    Source library: " + LIBRARY_PATH)
    body.append("    Export mode: " + ("FUNCTION only" if ONLY_FUNCTIONS else "all textual ST under POUs"))
    body.append("    Object count: " + str(len(items)))
    body.append("*)")
    body.append("")

    for item in items:
        body.append(render_item(item))

    with open(output_path, "wb") as handle:
        handle.write(encode_file_text("\n".join(body)))


def write_inventory(inventory_path, items):
    if not WRITE_INVENTORY:
        return

    folder = os.path.dirname(inventory_path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)

    lines = []
    for index, item in enumerate(items):
        if index > 0 and index % INVENTORY_GROUP_SIZE == 0:
            lines.append(INVENTORY_GROUP_SEPARATOR)
        lines.append("  - " + item["name"])

    with open(inventory_path, "wb") as handle:
        handle.write(encode_file_text("\n".join(lines)))


def main():
    library_path, output_path = get_script_args()

    project = None
    opened_by_script = False
    try:
        project, opened_by_script = open_library_project(library_path)
        items, inventory = collect_st_objects(project)

        if not items:
            raise RuntimeError(
                "No matching ST objects were exported. Set ONLY_FUNCTIONS = False "
                "or ONLY_UNDER_POUS_FOLDER = False, then check the inventory file."
            )

        write_output(output_path, items)
        write_inventory(INVENTORY_PATH, items)

        log("Exported objects: " + str(len(items)))
        log("Inventory names: " + str(len(items)))
        log("Wrote ST: " + output_path)
        if WRITE_INVENTORY:
            log("Wrote inventory: " + INVENTORY_PATH)
    finally:
        if opened_by_script and project is not None:
            try:
                project.close()
            except Exception:
                pass


try:
    main()
except Exception as exc:
    log("FAILED: " + str(exc))
    traceback.print_exc()
    raise
