"""Conservative extraction of textual IEC ST sources. Standard library only.

This is a source-container extractor, not a vendor-language normalizer or a
Jiecc compiler. It does not execute input code. Unknown preprocessing remains
explicitly marked and must be excluded from direct compiler test inputs.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

EXTENSIONS = {'.st', '.scl', '.iecst', '.exp', '.txt', '.iec', '.fun', '.typ', '.var', '.prg', '.inp', '.pro', '.udt', '.dt', '.decl'}
_GROUP_CACHE = []


def decode(data: bytes):
    """Return text, declared/detected encoding, and an explicit uncertainty note."""
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig'), 'utf-8-sig', None
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16'), 'utf-16-bom', None
    if b'\0' in data[:8192]:
        sample = data[:8192]
        if len(sample) > 4 and sample[1::2].count(0) / len(sample[1::2]) > .75:
            return data.decode('utf-16-le'), 'utf-16-le-inferred', 'BOM-less UTF-16LE inferred from zero-byte distribution.'
        if len(sample) > 4 and sample[::2].count(0) / len(sample[::2]) > .75:
            return data.decode('utf-16-be'), 'utf-16-be-inferred', 'BOM-less UTF-16BE inferred from zero-byte distribution.'
        raise ValueError('Binary or unknown encoding (embedded NUL bytes).')
    try:
        return data.decode('utf-8'), 'utf-8', None
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('cp1252'), 'windows-1252-fallback', 'Original encoding unspecified; Windows-1252 fallback must be reviewed for non-ASCII literals.'
    except UnicodeDecodeError:
        return data.decode('latin1'), 'latin-1-byte-preserving-fallback', 'Original encoding unspecified; byte-preserving Latin-1 fallback must be reviewed for non-ASCII literals.'


def mask(text: str):
    """Mask comments/quoted literals while preserving offsets and newlines."""
    out = list(text)
    i = 0
    while i < len(text):
        if text.startswith('(*', i):
            start = i
            i += 2
            depth = 1
            while i < len(text) and depth:
                if text.startswith('(*', i):
                    depth += 1; i += 2
                elif text.startswith('*)', i):
                    depth -= 1; i += 2
                else:
                    i += 1
            for j in range(start, i):
                if out[j] not in '\r\n': out[j] = ' '
        elif text.startswith('//', i):
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            for j in range(i, end): out[j] = ' '
            i = end
        elif text[i] in "'\"":
            start, quote = i, text[i]
            i += 1
            while i < len(text):
                if text[i] == '$': i += 2
                elif text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote: i += 2
                    else:
                        i += 1; break
                else: i += 1
            for j in range(start, min(i, len(text))):
                if out[j] not in '\r\n': out[j] = ' '
        else:
            i += 1
    return ''.join(out)


_POU = re.compile(r'(?im)^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s+([\w]+)\b[^\r\n]*')
_IX_ANNOTATIONS = {'ix-attr', 'ix-set', 'ix-prop', 'ix-generic'}


def _hash_syntax(text):
    """Distinguish known AXOpen annotations from preprocessing directives."""
    code = mask(text)
    names = set(re.findall(r'\{\s*#\s*([A-Za-z_][\w-]*)', code))
    annotations = sorted(n for n in names if n.lower() in _IX_ANNOTATIONS)
    macros = sorted(n for n in names if n.lower() not in _IX_ANNOTATIONS)
    macros += re.findall(r'(?im)^\s*#\s*(include|define|undef|if|ifdef|ifndef|elif|else|endif|error|warning)\b', code)
    return annotations, sorted(set(macros))


def _oo_boundaries(text):
    code = mask(text)
    code = re.sub(r'\{[^{}]*\}', lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()), code)
    counts = {}
    for kind in ('NAMESPACE', 'CLASS', 'INTERFACE'):
        start = len(re.findall(r'(?im)^\s*(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|ABSTRACT|FINAL|PARTIAL|STATIC)\s+)*' + kind + r'\b', code))
        end = len(re.findall(r'\bEND_' + kind + r'\b', code, re.I))
        if start or end:
            counts[kind] = {'open': start, 'close': end}
    return counts, bool(counts) and all(c['open'] == c['close'] and c['open'] > 0 for c in counts.values())


def reclassify_native_unit(name, unit, notes=None):
    """Repair native AX/OO classification without changing text or provenance.

    Returns the same {'units': [...], 'notes': [...]} shape as extract().
    Existing metadata is copied, including hashes and assembly/source mappings.
    This recognizes container completeness, never claims successful compilation.
    """
    updated = dict(unit)
    metadata = dict(unit.get('metadata', {}))
    updated['metadata'] = metadata
    revised_notes = list(notes or [])
    text = unit['st']
    annotations, directives = _hash_syntax(text)
    boundaries, oo_complete = _oo_boundaries(text)
    if annotations:
        metadata['vendor_annotations'] = annotations
        metadata['vendor_annotations_preserved'] = True
        metadata['annotation_role'] = 'AXOpen/ix twin-generation attributes; not Jiecc macro directives; original text retained'
    if boundaries:
        metadata['oo_declaration_boundaries'] = boundaries
    if directives:
        metadata['preprocessor_directives_detected'] = directives
        updated['status'] = 'preprocessor_required'
    else:
        metadata.pop('preprocessor_directives_detected', None)
        revised_notes = [n for n in revised_notes if n != 'Preprocessing directives preserved; not a ready standalone ST input.']
        # An external-tool dependency recorded by the .iec handler remains in
        # force even when its stage text happens not to contain a directive.
        protected = (metadata.get('preprocessor') or metadata.get('historical_extracted_copy')
                     or metadata.get('missing_pou_closures') or metadata.get('program_context')
                     or metadata.get('test_fixture')
                     or any(p.lower().endswith('_st_extracted') for p in PurePosixPath(name.replace('\\', '/')).parts))
        code = mask(text)
        complete_pou = bool(_pous(text))
        native_decl = bool(re.search(r'(?im)^\s*(TYPE|VAR(?:_[A-Z_]+)?)\b', code))
        eligible = PurePosixPath(name.replace('\\', '/')).suffix.lower() in {'.st', '.scl', '.iecst'}
        if eligible and not protected and (oo_complete or (not boundaries and (complete_pou or native_decl))):
            updated['status'] = 'assembled_vendor_source' if metadata.get('assembled_declarations') else 'native_vendor_source'
            revised_notes = [n for n in revised_notes if n != 'Body-only source; original declarations/scope were not invented.']
        elif unit['status'] == 'preprocessor_required' and not metadata.get('preprocessor'):
            updated['status'] = 'context_required'
        if not code.strip():
            metadata['comment_only_source'] = True
            updated['status'] = 'context_required'
            revised_notes.append('Source contains comments/whitespace only; retained as original context, not active executable ST.')
    if boundaries and not oo_complete:
        updated['status'] = 'context_required' if not directives else 'preprocessor_required'
        revised_notes.append('OO declaration opening/closing counts differ; original source retained for context review.')
    return {'units': [updated], 'notes': list(dict.fromkeys(revised_notes))}


def _pous(text):
    mtext = mask(text)
    result = []
    for match in _POU.finditer(mtext):
        kind, name = match.group(1).upper(), match.group(2)
        end = re.search(r'(?im)^\s*END_' + kind + r'\b[^\r\n]*', mtext[match.end():])
        if end:
            result.append({'kind': kind, 'name': name, 'start': match.start(),
                           'header_end': match.end(), 'end_start': match.end() + end.start(),
                           'end': match.end() + end.end(), 'text': text[match.start():match.end() + end.end()]})
    return result


def _indexes(files):
    for entry in _GROUP_CACHE:
        if entry[0] is files:
            return entry[1]
    index = {'fun': [], 'prg': {}, 'sibling': {}}
    for fn, data in files.items():
        norm = fn.replace('\\', '/')
        path = PurePosixPath(norm)
        ext = path.suffix.lower()
        index['sibling'].setdefault(str(path.parent).lower(), []).append(norm)
        if ext == '.fun':
            try:
                text, encoding, warning = decode(data)
                index['fun'].append((norm, _pous(text), encoding, warning))
            except (UnicodeError, ValueError):
                continue
        elif ext == '.prg':
            try:
                root = ET.fromstring(data)
                if root.tag.rsplit('}', 1)[-1] == 'Program':
                    members = []
                    for e in root.iter():
                        if e.tag.rsplit('}', 1)[-1] == 'File' and e.text:
                            members.append({'path': posixpath.normpath(str(path.parent / e.text.strip())),
                                            'attributes': dict(e.attrib)})
                    for member in members:
                        index['prg'][member['path'].lower()] = {'manifest': norm,
                            'attributes': dict(root.attrib), 'members': members}
            except ET.ParseError:
                pass
    _GROUP_CACHE.append((files, index))
    if len(_GROUP_CACHE) > 1: _GROUP_CACHE.pop(0)
    return index


def _find_fun(name, pou_name, kind, index):
    """Only search declaration files in the implementation's ancestor chain."""
    parent = PurePosixPath(name).parent
    ancestors = [str(parent).lower()] + [str(p).lower() for p in parent.parents]
    matches = []
    for filename, declarations, encoding, warning in index['fun']:
        folder = str(PurePosixPath(filename).parent).lower()
        if folder not in ancestors:
            continue
        for declaration in declarations:
            if declaration['name'].casefold() == pou_name.casefold() and (not kind or declaration['kind'] == kind):
                matches.append((ancestors.index(folder), filename, declaration, encoding, warning))
    if not matches: return None
    nearest = min(m[0] for m in matches)
    nearest_matches = [m for m in matches if m[0] == nearest]
    # Two libraries with the same declaration at the same ancestor are ambiguous.
    return nearest_matches[0] if len(nearest_matches) == 1 else None


def _jiecc(text, name, files, metadata):
    """Preserve preprocessing, include graph and documented build commands."""
    _, directives = _hash_syntax(text)
    if not directives and not re.search(r'@@|__VA_ARGS__|__VA_ARGC__', mask(text)):
        return None
    includes = re.findall(r'\{\s*#\s*include\s+[\"<]([^\">]+)[\">]', text)
    targets = ['omron'] if 'jieclib' in name.lower() else ['omron', 'keyence', 'codesys']
    normalized = {fn.replace('\\', '/'): fn for fn in files}
    dependencies = []
    for include in includes:
        local = posixpath.normpath(posixpath.join(posixpath.dirname(name), include))
        suffix_matches = [fn for fn in normalized if fn.endswith('/' + include)]
        dependencies.append({'include': include, 'relative_match': local if local in normalized else None,
                             'other_suffix_candidates': suffix_matches[:30],
                             'note': 'Suffix candidates are references only; include search path was not assumed.'})
    commands = re.findall(r'(?m)^\s*(?:\*\s*)?(?:\$\s*)?(jiecc\s+[^\r\n]+)', text)
    metadata.update({'preprocessor': 'Jiecc/jiepp', 'directives': sorted(set(directives)),
                     'includes': dependencies, 'documented_commands': commands,
                     'target_candidates_from_library_readme': targets,
                     'macro_expanded': False,
                     'preprocessing_required': 'Use the matching documented Jiecc version and explicit target; then extract ST from its IEC 61131-10 XML. Original directives were preserved, never guessed.'})
    # Deliberately do not run downloaded preprocessors or implement undocumented
    # token-pasting, variadic macros, conditional target logic or include order.
    return {'units': [{'name': PurePosixPath(name).stem + '__jiecc_pending', 'st': text,
                       'status': 'preprocessor_required', 'metadata': metadata}],
            'notes': ['Contains unexpanded Jiecc syntax. This .st is an auditable staging source, not a ready compiler input.']}


def extract(name: str, data: bytes, files: dict[str, bytes]):
    """Extract units from textual ST, B&R split declarations and legacy exports."""
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    ext = path.suffix.lower()
    if ext not in EXTENSIONS: return None
    # XML project wrappers, archive containers and HTML documents belong to
    # specialized extractors, never to extension-renaming.
    if data.startswith((b'PK\x03\x04', b'\x1f\x8b', b'\xd0\xcf\x11\xe0')):
        return None
    try:
        text, encoding, warning = decode(data)
    except (UnicodeError, ValueError) as exc:
        return {'units': [], 'notes': [str(exc)]} if ext not in {'.txt', '.pro'} else None
    if text.lstrip().startswith(('<', '{"', '{\n"')):
        return None
    code = mask(text)
    has_pou = bool(_POU.search(code))
    has_declaration = bool(re.search(r'(?im)^\s*(TYPE|VAR(?:_[A-Z_]+)?)\b', code))
    oo_boundaries, oo_complete = _oo_boundaries(text)
    has_st = has_pou or has_declaration or oo_complete or bool(re.search(r':=|\bEND_(IF|FOR|WHILE|CASE|REPEAT|ACTION)\b', code, re.I))
    if ext == '.decl':
        declaration_code = re.sub(r'\{[^{}]*\}', '', code).lstrip()
        if not re.match(r'(?:FUNCTION_BLOCK|FUNCTION|PROGRAM|TYPE|VAR(?:_[A-Z_]+)?)\b', declaration_code, re.I):
            return {'units': [], 'notes': ['.decl content does not begin with a clear ST declaration; original retained.']}
    elif ext in {'.udt', '.dt'}:
        if not re.match(r'\s*TYPE\b', code, re.I) or not re.search(r'\bEND_TYPE\b', code, re.I):
            return {'units': [], 'notes': ['Type container is not confirmed textual IEC/SCL TYPE...END_TYPE; original retained without extension-only conversion.']}
    elif ext in {'.txt', '.pro'}:
        # Reject prose snippets and manual text; require a full top-level
        # declaration and its matching end token in a source-shaped file.
        first = code.lstrip()
        if not re.match(r'(FUNCTION_BLOCK|FUNCTION|PROGRAM|TYPE|VAR(?:_[A-Z_]+)?)\b', first, re.I): return None
        if not re.search(r'\bEND_(FUNCTION_BLOCK|FUNCTION|PROGRAM|TYPE|VAR)\b', code, re.I): return None
        if re.search(r'(?m)^\s*(?:#{1,6}\s|```|<!DOCTYPE|<html)', text, re.I): return None
    elif ext == '.prg' and not has_st:
        return None
    elif ext == '.exp' and not has_st:
        return {'units': [], 'notes': ['Legacy export has no confidently identified textual ST declarations/body.']}
    elif ext == '.inp':
        # MatIEC/OpenPLC test directories mix ST, IL, shared declarations,
        # malformed lexer fragments and other input formats. Never rename
        # every .inp to .st simply because it is a compiler fixture.
        body = re.sub(r'(?is)\bVAR(?:_[A-Z_]+)?\b.*?\bEND_VAR\b', '', code)
        il = re.search(
            r'(?im)^\s*(?:[A-Za-z_]\w*:\s*)?'
            r'(?:LDN?|STN?|S|R|ANDN?|ORN?|XORN?|NOT|ADD|SUB|MUL|DIV|MOD|'
            r'GT|GE|EQ|NE|LE|LT|CALC?N?|RETC?N?|JMPC?N?)'
            r'(?:[ \t]+[^\r\n;]+|[ \t]*)$', body)
        if il:
            return {'units': [], 'notes': ['Compiler input contains IL instructions; original .inp retained, not relabeled as ST.']}
        if not has_st:
            return {'units': [], 'notes': ['Compiler input language is uncertain: no clear ST declaration or statement evidence; original .inp retained.']}
    metadata = {'source_name': name, 'source_encoding': encoding,
                'source_sha256': hashlib.sha256(data).hexdigest(), 'format': ext,
                'normalization_performed': False}
    notes = [warning] if warning else []
    if warning: metadata['encoding_warning'] = warning
    if ext == '.iec':
        result = _jiecc(text, name, files, metadata)
        if result: return result
    status = 'native_vendor_source' if has_pou or has_declaration or oo_complete else 'context_required'
    if ext == '.decl':
        status = 'context_required'
        metadata['context_role'] = 'Split ST declaration fragment; original implementation and project scope required'
        metadata['source_kind'] = 'test_scaffold' if '/scaffold/' in name.lower() else 'declaration_fragment'
        notes.append('Declaration-only source, not a complete library implementation; no body or closing syntax synthesized.')
    if ext in {'.udt', '.dt'}:
        metadata['context_role'] = 'Native vendor TYPE declaration text; dialect syntax retained'
        starts = len(re.findall(r'(?im)^\s*TYPE\b', code))
        ends = len(re.findall(r'\bEND_TYPE\b', code, re.I))
        metadata['type_boundary_counts'] = {'TYPE': starts, 'END_TYPE': ends}
        if starts != ends:
            status = 'context_required'
            notes.append('TYPE/END_TYPE counts differ; original declaration text retained for review.')
    if ext == '.exp':
        directives = re.findall(r'\(\*\s*@.*?\*\)', text, re.S)
        metadata['editor_directives'] = directives
        text = re.sub(r'\(\*\s*@.*?\*\)', '', text, flags=re.S)
        metadata['editor_directives_removed_from_st'] = True
        if re.search(r'(?im)^\s*(?:FBD|LD|SFC|NETWORK|END_NETWORK|_FBD_BODY|_SFC_BODY)\b', mask(text)):
            status = 'context_required'
            notes.append('Legacy export may include a graphical/IL section; textual content preserved for review.')
    if ext in {'.fun', '.var'}:
        status = 'context_required'
        metadata['context_role'] = 'B&R POU declarations (implementations in sibling .st)' if ext == '.fun' else 'Variable declarations with original enclosing scope preserved in project metadata'
        notes.append('Context declarations; do not compile with assembled declarations a second time.')
    if ext == '.inp':
        status = 'context_required'
        metadata['test_fixture'] = True
        metadata['expected_validity'] = 'unknown; repository may intentionally contain negative/IL tests'
        metadata['context_role'] = 'Original compiler test fixture; may be a partial production or deliberately invalid source'
        notes.append('Identified ST declaration/statement fixture; original repository may intend a syntax or semantic failure.')
    if ext in {'.st', '.scl', '.iecst', '.prg'}:
        index = _indexes(files)
        project = index['prg'].get(name.lower())
        if project:
            metadata['program_context'] = project
        sibling_names = index['sibling'].get(str(path.parent).lower(), [])
        metadata['sibling_context_files'] = [n for n in sibling_names if PurePosixPath(n).suffix.lower() in {'.fun', '.typ', '.var', '.prg', '.yaml', '.yml'}]
        pous = _pous(text)
        replacements = []
        assembled = []
        for pou in pous:
            # If the source already declares local/interface variables or a
            # function return type, it is not a bare B&R implementation header.
            segment = text[pou['header_end']:pou['end_start']]
            header = text[pou['start']:pou['header_end']]
            if re.search(r'(?im)^\s*VAR\b|^\s*VAR_', mask(segment)) or ':' in mask(header):
                continue
            match = _find_fun(name, pou['name'], pou['kind'], index)
            if match:
                _, fn, declaration, dec_encoding, dec_warning = match
                # Offsets inside declaration text are relative to .fun source.
                prefix_len = declaration['end_start'] - declaration['start']
                declaration_prefix = declaration['text'][:prefix_len].rstrip()
                implementation = text[pou['header_end']:pou['end_start']]
                merged = declaration_prefix + '\n' + implementation + '\nEND_' + pou['kind']
                replacements.append((pou['start'], pou['end'], merged))
                assembled.append({'pou': pou['name'], 'declaration_file': fn,
                                  'declaration_encoding': dec_encoding,
                                  'declaration_sha256': hashlib.sha256(files[fn]).hexdigest(),
                                  'operation': 'Use matching .fun declaration and .st implementation; keep exactly one END_' + pou['kind']})
                if dec_warning: notes.append(dec_warning)
        if not pous and has_st:
            match = _find_fun(name, path.stem, None, index)
            if match:
                _, fn, declaration, dec_encoding, dec_warning = match
                prefix_len = declaration['end_start'] - declaration['start']
                text = declaration['text'][:prefix_len].rstrip() + '\n' + text + '\nEND_' + declaration['kind'] + '\n'
                assembled.append({'pou': declaration['name'], 'declaration_file': fn,
                                  'declaration_encoding': dec_encoding, 'operation': 'Wrap body using explicit matching .fun declaration'})
        for start, end, merged in reversed(replacements):
            text = text[:start] + merged + text[end:]
        if assembled:
            status = 'assembled_vendor_source'
            metadata['assembled_declarations'] = assembled
        if project and (not assembled):
            status = 'context_required'
            notes.append('B&R lifecycle/action source requires the referenced program variables and scheduling context; no synthetic wrapper was invented.')
        elif re.search(r'(?im)^\s*ACTION\b', mask(text)):
            status = 'context_required'
            notes.append('Action declarations require their owning POU/project context.')
        elif not has_pou and not has_declaration and not oo_complete and not assembled:
            status = 'context_required'
            notes.append('Body-only source; original declarations/scope were not invented.')
    _, preprocessing = _hash_syntax(text)
    if preprocessing:
        status = 'preprocessor_required'
        notes.append('Preprocessing directives preserved; not a ready standalone ST input.')
    # Earlier research downloads contain convenience extracts that may omit
    # interfaces or closing tokens. Keep them distinct from native full source.
    if any(part.lower().endswith('_st_extracted') for part in path.parts):
        status = 'context_required'
        metadata['historical_extracted_copy'] = True
        notes.append('Historical *_ST_extracted copy; original container must be used to verify declarations and complete POU boundaries.')
    final_code = mask(text)
    missing_closures = []
    for pou_start in _POU.finditer(final_code):
        kind = pou_start.group(1).upper()
        # Match before the next POU opening, so a later function's terminator
        # cannot falsely close the current incomplete extraction.
        next_pou = _POU.search(final_code, pou_start.end(2))
        end_limit = next_pou.start() if next_pou else len(final_code)
        if not re.search(r'\bEND_' + kind + r'\b', final_code[pou_start.end(2):end_limit], re.I):
            missing_closures.append({'kind': kind, 'name': pou_start.group(2)})
    if missing_closures:
        status = 'context_required'
        metadata['missing_pou_closures'] = missing_closures
        notes.append('POU opening lacks its matching closing token; no closing syntax was invented.')
    unit = {'name': path.stem, 'st': text, 'status': status, 'metadata': metadata}
    return reclassify_native_unit(name, unit, notes)
