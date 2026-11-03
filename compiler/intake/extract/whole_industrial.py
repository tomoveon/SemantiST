"""Whole-library ST assembly from verified industrial XML source.

Output trees contain .st only. Audit records remain beside this script.
No Logix alias / bit-overlay or Unity ExtractBit storage is guessed.
"""
from __future__ import annotations
from pathlib import Path, PurePosixPath
from collections import Counter, defaultdict
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile

from . import provenance

# Configured by the extraction driver: corpus root, work and output directories.
BASE = None
WORK = None
OUT = None
RECORDS = []
ST_BODIES = []


def nl(s):
    return s.replace('\r\r\n', '\n').replace('\r\n', '\n').replace('\r', '\n')


def dec(b):
    for enc in ('utf-8-sig', 'utf-16', 'cp1251', 'cp1252'):
        try:
            return nl(b.decode(enc))
        except UnicodeError:
            pass
    raise ValueError('Cannot decode source')


def has_code(s):
    # Native block comments can be nested; strings are irrelevant to empty tests.
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    prior = None
    while prior != s:
        prior = s
        s = re.sub(r'\(\*(?:(?!\(\*|\*\)).)*\*\)', '', s, flags=re.S)
    return bool(re.sub(r'//[^\n]*', '', s).strip())


def comment(s):
    return '(* ' + s.replace('(*', '( *').replace('*)', '* )') + ' *)\n'


def record(vendor, source, action, reason):
    RECORDS.append((vendor, source, action, reason))


def write(vendor, name, text, bodies=(), archive=None):
    if not has_code(text):
        return
    target = OUT / vendor / '.st文件' / (name + '.st')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + '\n', encoding='utf-8', newline='\n')
    provenance.register(target, [source for source, _ in bodies], archive=str(archive) if archive else None)
    for source, body in bodies:
        assert nl(body).rstrip() in text, source
    ST_BODIES.extend((str(target), source, hashlib.sha256(body.encode()).hexdigest()) for source, body in bodies)
    record(vendor, str(target.relative_to(OUT)), 'output', f'{len(bodies)} ST bodies; {len(text.encode("utf-8"))} bytes')


def unity_var(v):
    name, typ = v.get('name'), v.get('typeName')
    if not name or not typ:
        raise ValueError('Missing explicit variable name/type')
    lines = []
    if v.findtext('comment'):
        lines.append(comment(v.findtext('comment').strip()).rstrip())
    attrs = [(x.get('name'), x.get('value')) for x in v.findall('attribute')]
    if any(k == 'ExtractBit' for k, _ in attrs):
        raise ValueError('ExtractBit shared storage cannot be rendered as an independent ST member')
    if attrs:
        lines.append(comment('Unity attributes: ' + '; '.join(f'{k}={val}' for k, val in attrs)).rstrip())
    line = name
    if v.get('topologicalAddress'):
        line += ' AT ' + v.get('topologicalAddress')
    line += ' : ' + typ
    init = v.find('variableInit')
    if init is not None:
        if init.get('value') is None or len(init):
            raise ValueError('Complex initial value without a direct ST expression')
        line += ' := ' + init.get('value')
    lines.append(line + ';')
    for desc in v.findall('instanceElementDesc'):
        lines.append(comment('Unity member description: ' + ET.tostring(desc, encoding='unicode')).rstrip())
    return '\n'.join('    ' + x for x in lines) + '\n'


def unity_types(root):
    result, bad = {}, {}
    for dt in root.findall('DDTSource'):
        name = dt.get('DDTName')
        if dt.find('structure') is None:
            bad[name] = 'DDT is not an explicitly represented structure'
            continue
        try:
            text = f'TYPE\n{name} : STRUCT\n' + ''.join(unity_var(v) for v in dt.findall('./structure/variables')) + 'END_STRUCT;\nEND_TYPE\n'
            deps = {v.get('typeName') for v in dt.findall('./structure/variables')}
            result[name] = (text, deps)
        except ValueError as e:
            bad[name] = str(e)
    return result, bad


def typename(s):
    if not s:
        return ''
    if re.match(r'(?i)^ARRAY\s*\[', s):
        return re.split(r'(?i)\bOF\b', s)[-1].strip()
    return s


def closure_status(types, bad):
    reject = dict(bad)
    while True:
        old = len(reject)
        for name, (_, deps) in types.items():
            if name in reject:
                continue
            hit = next((typename(d) for d in deps if typename(d) in reject), None)
            if hit:
                reject[name] = f'depends on {hit}: {reject[hit]}'
        if len(reject) == old:
            return reject


def needed_types(names, types):
    seen = set()
    def add(n):
        n = typename(n)
        if n in seen or n not in types:
            return
        seen.add(n)
        for d in types[n][1]:
            add(d)
    for n in names:
        add(n)
    return [types[n][0] for n in sorted(seen)]


def unity_assemble(source, root, extra_types=None, extra_bad=None):
    types, bad = unity_types(root)
    alltypes = dict(extra_types or {})
    alltypes.update(types)
    allbad = dict(extra_bad or {})
    for key in types:
        allbad.pop(key, None)
    allbad.update(bad)
    reject = closure_status(alltypes, allbad)
    vendor = '10_Schneider_Control_Expert'
    pou_parts, bodies, typeneeded = [], [], set()
    for fb in root.findall('FBSource'):
        name = fb.get('nameOfFBType')
        sections = fb.findall('FBProgram')
        if any(x.find('STSource') is None for x in sections):
            record(vendor, source + '#' + name, 'omitted', 'DFB includes non-ST sections')
            continue
        native = [(s.get('name', 'section'), nl(s.findtext('STSource') or '')) for s in sections]
        if not any(has_code(body) for _, body in native):
            record(vendor, source + '#' + name, 'omitted', 'No active ST implementation')
            continue
        decl = []
        dependencies = set()
        reason = None
        for node, keyword in (('inputParameters','VAR_INPUT'),('outputParameters','VAR_OUTPUT'),('inOutParameters','VAR_IN_OUT'),('publicLocalVariables','VAR'),('privateLocalVariables','VAR')):
            block = fb.find(node)
            if block is None:
                continue
            variables = block.findall('variables')
            dependencies.update(typename(v.get('typeName')) for v in variables)
            try:
                if variables:
                    decl.append(keyword + '\n' + ''.join(unity_var(v) for v in variables) + 'END_VAR\n')
            except ValueError as e:
                reason = str(e)
        failed = sorted(dependencies & reject.keys())
        if failed:
            reason = '; '.join(f'{d}: {reject[d]}' for d in failed)
        if reason:
            record(vendor, source + '#' + name, 'omitted', reason)
            continue
        body = ''.join(comment(f'Original DFB section {section}') + text.rstrip() + '\n' for section, text in native)
        pou_parts.append(comment(f'Source {source}; DFB {name}') + 'FUNCTION_BLOCK ' + name + '\n' + ''.join(decl) + '\n' + body + 'END_FUNCTION_BLOCK\n')
        bodies.extend((source + '#' + name + '/' + sec, text) for sec, text in native if has_code(text))
        typeneeded.update(dependencies)
    # Entire project declarations are useful only when their types are available.
    # Each native task section remains an ordered section in one project PROGRAM;
    # sections are not speculatively invoked or converted from FBD/LD.
    programs = root.findall('program')
    task_sections = defaultdict(list)
    for section in programs:
        ident = section.find('identProgram')
        body = section.findtext('STSource')
        name = ident.get('name', 'section') if ident is not None else 'section'
        if body is None:
            record(vendor, source + '#section/' + name, 'omitted', 'Native non-ST task section')
            continue
        if has_code(body):
            task = ident.get('task', 'MAST') if ident is not None else 'MAST'
            task_sections[task].append((name, nl(body)))
    globals_block = root.find('dataBlock')
    # Avoid creating a project shell with undeclared / omitted graphical DFBs.
    available_fb = {re.search(r'FUNCTION_BLOCK ([^\n]+)', p).group(1) for p in pou_parts}
    original_fb = {f.get('nameOfFBType') for f in root.findall('FBSource')}
    global_decls = []
    project_reason = None
    if task_sections and globals_block is not None:
        for var in globals_block.findall('variables'):
            typ = typename(var.get('typeName'))
            if typ in reject or (typ in original_fb and typ not in available_fb):
                project_reason = f'Global declaration {var.get("name")} requires omitted type {typ}'
                break
            try:
                global_decls.append(unity_var(var))
                typeneeded.add(typ)
            except ValueError as e:
                project_reason = str(e)
                break
    if task_sections and project_reason:
        for task, sections in task_sections.items():
            for name, body in sections:
                record(vendor, source + '#section/' + name, 'omitted', project_reason)
    elif task_sections:
        if global_decls:
            pou_parts.insert(0, 'VAR_GLOBAL\n' + ''.join(global_decls) + 'END_VAR\n')
        for task, sections in task_sections.items():
            part = comment(f'Original Unity task {task}; ST sections in original document order')
            part += 'PROGRAM ' + re.sub(r'\W', '_', PurePosixPath(source).stem + '_' + task) + '\n'
            for name, body in sections:
                part += comment('Original task section: ' + name) + body.rstrip() + '\n'
                bodies.append((source + '#section/' + name, body))
            pou_parts.append(part + 'END_PROGRAM\n')
    if not bodies:
        return None
    text = comment('Schneider Control Expert ST extracted as whole source units. XML declarations transcribed to IEC text; native instructions remain unchanged.')
    text += '\n'.join(needed_types(typeneeded, alltypes)) + '\n' + '\n'.join(pou_parts)
    return text, bodies


def run_unity():
    vendor = '10_Schneider_Control_Expert'
    raw = next((BASE/vendor/'原始库文件').rglob('PACFramework*.zip'))
    with zipfile.ZipFile(raw) as z:
        roots = {n: ET.fromstring(z.read(n)) for n in z.namelist() if n.lower().endswith(('.xef','.xdb','.xdd'))}
    extra_types, extra_bad = {}, {}
    for n, r in roots.items():
        if n.lower().endswith('.xdd'):
            t, b = unity_types(r)
            extra_types.update(t)
            extra_bad.update(b)
    for n, root in roots.items():
        if n.lower().endswith('.xdd'):
            continue
        result = unity_assemble(n, root, extra_types, extra_bad)
        if result:
            text, bodies = result
            write(vendor, 'PACFramework_' + PurePosixPath(n).stem, text, bodies, raw)
        else:
            record(vendor, n, 'omitted', 'No complete ST POU can be represented without guessing shared storage or graphical implementation')


def run_plcnext():
    vendor = '15_Phoenix_PLCnext'
    raw = next((BASE/vendor/'原始库文件').rglob('PACFramework*.zip'))
    with zipfile.ZipFile(raw) as z:
        packages = [(n, z.read(n)) for n in z.namelist() if n.lower().endswith('.pcwex')]
    for name, data in packages:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n) for n in z.namelist() if not n.endswith('/')}
        pieces, bodies = [], []
        candidates, rejected = {}, {}
        for n, data in files.items():
            if not n.endswith('Metadata.meta') or '/RecycleBin/' in n:
                continue
            meta = ET.fromstring(data)
            if meta.findtext('FolderType') != 'PouFolder':
                continue
            folder = str(PurePosixPath(n).parent)
            pname = meta.findtext('DisplayName') or PurePosixPath(folder).stem
            worksheets = meta.findall('./ItemGroup/CodeWorksheet')
            source = '\n'.join(dec(files.get(folder + '/' + ws.get('Include',''),b'')) for ws in worksheets)
            decl = dec(files.get(folder + '/Variables.var', b''))
            candidates[pname] = source + '\n' + decl
            if any(not ws.get('Include','').lower().endswith('.st') for ws in worksheets):
                rejected[pname] = 'POU contains native graphical language worksheet'
            elif len(re.findall(r'(?i)(?::=|=>)\s*(?:BOOL|INT|REAL|DINT)\s*[,)]',source)) >= 4:
                rejected[pname] = 'Original IDE call template still contains unfilled BOOL/INT type-name placeholders as actual arguments'
        while True:
            old = len(rejected)
            for pname, source in candidates.items():
                if pname in rejected:
                    continue
                missing = next((p for p in rejected if re.search(r'\b' + re.escape(p) + r'\s*\(',source) or re.search(r':\s*' + re.escape(p) + r'\b',source)), None)
                if missing:
                    rejected[pname] = 'POU depends on omitted project POU ' + missing
            if len(rejected) == old:
                break
        for n, data in files.items():
            if '/RecycleBin/' in n:
                if n.lower().endswith('.st'):
                    record(vendor, name + '!' + n, 'omitted', 'Deleted POU in project recycle bin')
                continue
            if n.endswith('.dt') or n.endswith('GlobalVariables.var'):
                s = dec(data)
                if has_code(s):
                    pieces.append(comment('Source ' + name + '!' + n) + s.rstrip() + '\n')
        for n, data in files.items():
            if not n.endswith('Metadata.meta') or '/RecycleBin/' in n:
                continue
            meta = ET.fromstring(data)
            if meta.findtext('FolderType') != 'PouFolder':
                continue
            folder = str(PurePosixPath(n).parent)
            kind = meta.findtext('Kind')
            pname = meta.findtext('DisplayName') or PurePosixPath(folder).stem
            worksheets = meta.findall('./ItemGroup/CodeWorksheet')
            if pname in rejected:
                record(vendor, name + '!' + folder, 'omitted', rejected[pname])
                continue
            decl_name = folder + '/Variables.var'
            if decl_name not in files:
                record(vendor, name + '!' + folder, 'omitted', 'Missing declaration worksheet')
                continue
            returntype = meta.findtext('ReturnType')
            if kind not in {'Program','FunctionBlock','Function'} or (kind == 'Function' and not returntype):
                record(vendor, name + '!' + folder, 'omitted', 'Missing explicit POU kind or function return type')
                continue
            native = []
            for ws in worksheets:
                ws_name = folder + '/' + ws.get('Include')
                if ws_name not in files:
                    raise ValueError('Missing declared worksheet ' + ws_name)
                native.append((ws_name, dec(files[ws_name])))
            if not any(has_code(s) for _, s in native):
                record(vendor, name + '!' + folder, 'omitted', 'Empty/comment-only ST implementation')
                continue
            kw = {'Program':'PROGRAM','FunctionBlock':'FUNCTION_BLOCK','Function':'FUNCTION'}[kind]
            end = {'Program':'END_PROGRAM','FunctionBlock':'END_FUNCTION_BLOCK','Function':'END_FUNCTION'}[kind]
            text = comment('Source ' + name + '!' + folder)
            text += kw + ' ' + pname + (' : ' + returntype if kind == 'Function' else '') + '\n'
            text += dec(files[decl_name]).rstrip() + '\n\n'
            for source, body in native:
                text += comment('Original worksheet ' + source) + body.rstrip() + '\n'
                if has_code(body):
                    bodies.append((name + '!' + source, body))
            pieces.append(text + end + '\n')
        write(vendor, 'PACFramework_' + PurePosixPath(name).stem, '\n'.join(pieces), bodies, raw)


def logix_type(dt):
    name = dt.get('Name')
    members = dt.findall('./Members/Member')
    if any(m.get('Target') is not None or m.get('BitNumber') is not None for m in members):
        raise ValueError('UDT contains native bit overlay/shared storage')
    lines = [f'TYPE\n{name} : STRUCT\n']
    deps = set()
    for m in members:
        typ = m.get('DataType')
        if not typ:
            raise ValueError('UDT member missing explicit DataType')
        deps.add(typ)
        size = m.get('Dimension', '0')
        if size not in ('0',''):
            typ = f'ARRAY[0..{int(size)-1}] OF ' + typ
        attrs = '; '.join(f'{k}={v}' for k,v in m.attrib.items() if k not in {'Name','DataType','Dimension'})
        if attrs:
            lines.append('    ' + comment('Native Logix UDT member: ' + attrs))
        lines.append('    ' + m.get('Name') + ' : ' + typ + ';\n')
    lines.append('END_STRUCT;\nEND_TYPE\n')
    return ''.join(lines), deps


def decorated_value(node):
    if node.tag in ('DataValue','DataValueMember'):
        return node.get('Value')
    if node.tag in ('Structure','StructureMember'):
        parts = []
        for c in node:
            value = decorated_value(c)
            if value is None or not c.get('Name'):
                raise ValueError('Unsupported native decorated structure initializer')
            parts.append(c.get('Name') + ' := ' + value)
        return '(' + ', '.join(parts) + ')'
    if node.tag in ('Array','ArrayMember'):
        dims = [int(d) for d in node.get('Dimensions','').split(',') if d]
        if len(dims) != 1:
            raise ValueError('Native multidimensional array initializer not explicitly transcribed')
        elements = node.findall('Element')
        if len(elements) != dims[0] or [e.get('Index') for e in elements] != [f'[{i}]' for i in range(dims[0])]:
            raise ValueError('Sparse or out-of-order native array initializer')
        parts = []
        for e in elements:
            if e.get('Value') is not None:
                parts.append(e.get('Value'))
            elif len(e) == 1:
                parts.append(decorated_value(e[0]))
            else:
                raise ValueError('Unsupported native array element initializer')
        return '[' + ', '.join(parts) + ']'
    raise ValueError('Unsupported native decorated initializer ' + node.tag)


def logix_initial(node):
    native = node.findall('DefaultData') + node.findall('Data')
    decorated = next((x for x in native if x.get('Format') == 'Decorated'), None)
    if decorated is not None:
        if len(decorated) != 1:
            raise ValueError('Initializer has multiple native decorated roots')
        return decorated_value(decorated[0])
    string = next((x for x in native if x.get('Format') == 'String'), None)
    raw = next((x for x in native if x.get('Format') == 'L5K'), None)
    if string is not None:
        if node.get('DataType') == 'STRING':
            return ''.join(string.itertext()).strip()
        # The source explicitly supplies a LEN / DATA native string UDT value.
        # Decode its actual byte buffer; do not invent a STRING type conversion.
        s = ''.join(raw.itertext()).strip() if raw is not None else ''
        m = re.fullmatch(r"\[\s*(\d+)\s*,\s*'([^']*)'\s*\]", s)
        if not m:
            raise ValueError('Native custom string buffer initializer not explicitly readable')
        encoded = m.group(2)
        values = []
        i = 0
        while i < len(encoded):
            if encoded[i] == '$':
                if not re.fullmatch('[0-9A-Fa-f]{2}',encoded[i+1:i+3]):
                    raise ValueError('Unsupported native string byte escape')
                values.append(str(int(encoded[i+1:i+3],16)))
                i += 3
            elif ord(encoded[i]) < 128:
                values.append(str(ord(encoded[i])))
                i += 1
            else:
                raise ValueError('Native custom string contains unspecified non-ASCII byte encoding')
        return '(LEN := ' + m.group(1) + ', DATA := [' + ', '.join(values) + '])'
    if native:
        raise ValueError('Native initialization/configuration lacks a supported explicit value representation')
    return None


def logix_decl(node):
    if node.get('AliasFor') or node.get('TagType') == 'Alias':
        raise ValueError('Native Logix AliasFor storage relation cannot be represented as an independent ST variable')
    typ = node.get('DataType')
    if not typ:
        raise ValueError('Missing explicit DataType')
    dims = node.get('Dimensions', '')
    if dims:
        bounds = ','.join('0..' + str(int(n)-1) for n in re.split(r'[,\s]+',dims) if n)
        typ = 'ARRAY[' + bounds + '] OF ' + typ
    attrs = '; '.join(f'{k}={v}' for k,v in node.attrib.items() if k not in {'Name','DataType','Dimensions'})
    text = comment('Native Logix declaration: ' + attrs) if attrs else ''
    if node.findtext('Description'):
        text += comment(node.findtext('Description').strip())
    # Values are native L5K / decorated aggregates. Preserve exact source data
    # alongside each declaration; do not reinterpret AOI retain/initialization.
    for initial in node.findall('Data'):
        if initial.get('Format') == 'L5K':
            text += comment('Native Logix initial data (L5K): ' + ''.join(initial.itertext()).strip())
    for initial in node.findall('DefaultData'):
        if initial.get('Format') == 'L5K':
            text += comment('Native Logix default data (L5K): ' + ''.join(initial.itertext()).strip())
    initial_value = logix_initial(node)
    text += node.get('Name') + ' : ' + typ + (' := ' + initial_value if initial_value is not None else '') + ';\n'
    return ''.join('    ' + x + '\n' for x in text.rstrip().splitlines())


def logix_body(routine):
    st = routine.find('STContent')
    if st is None:
        return None
    return nl('\n'.join(''.join(line.itertext()).strip('\r\n') for line in st.findall('Line')))


def logix_aoi(aoi, types, reject):
    name = aoi.get('Name')
    routines = aoi.findall('./Routines/Routine')
    if any(r.get('Type') != 'ST' for r in routines):
        raise ValueError('AOI contains native non-ST routine')
    params = aoi.findall('./Parameters/Parameter')
    locals_ = aoi.findall('./LocalTags/LocalTag')
    deps = {n.get('DataType') for n in params + locals_ if n.get('DataType')}
    bad = sorted(deps & reject.keys())
    if bad:
        raise ValueError('; '.join(f'{k}: {reject[k]}' for k in bad))
    declarations = []
    for use, kw in (('Input','VAR_INPUT'),('Output','VAR_OUTPUT'),('InOut','VAR_IN_OUT')):
        group = [p for p in params if p.get('Usage') == use]
        if group:
            declarations.append(kw + '\n' + ''.join(logix_decl(p) for p in group) + 'END_VAR\n')
    if any(p.get('Usage') not in ('Input','Output','InOut') for p in params):
        raise ValueError('Unknown native parameter usage')
    if locals_:
        declarations.append('VAR\n' + ''.join(logix_decl(p) for p in locals_) + 'END_VAR\n')
    native = [(r.get('Name'), logix_body(r)) for r in routines]
    if not any(b and has_code(b) for _, b in native):
        raise ValueError('No active ST implementation')
    main = [(n,b) for n,b in native if n == 'Logic']
    if len(main) != 1:
        raise ValueError('No unique native Logic routine')
    attrs = '; '.join(f'{k}={v}' for k,v in aoi.attrib.items() if k in {'Revision','ExecutePrescan','ExecutePostscan','ExecuteEnableInFalse'})
    text = comment('Original Logix AOI attributes: ' + attrs) + f'FUNCTION_BLOCK {name}\n' + ''.join(declarations) + '\n'
    text += comment('Native AOI Logic routine; scan/Enable semantics remain those of the original Logix project.') + main[0][1].rstrip() + '\n'
    for rname, body in native:
        if rname == 'Logic' or not body or not has_code(body):
            continue
        text += '\n' + comment('Native additional AOI routine ' + rname + '; retained as a scoped method without adding a call or scheduling it.')
        text += 'METHOD ' + rname + '\n' + body.rstrip() + '\nEND_METHOD\n'
    text += 'END_FUNCTION_BLOCK\n'
    return text, deps, [(n,b) for n,b in native if b and has_code(b)]


def run_logix():
    vendor = '07_Rockwell_Logix'
    raw = next((BASE/vendor/'原始库文件').rglob('LogixLibraries*.zip'))
    groups = defaultdict(list)
    with zipfile.ZipFile(raw) as z:
        for n in z.namelist():
            if not n.lower().endswith('.l5x'):
                continue
            rel = '/'.join(n.split('/')[1:])
            if not rel.startswith('src/'):
                record(vendor, rel, 'omitted', 'Reference/sample export outside library src tree')
                continue
            # Preserve the native repository version boundary. Current releases
            # form one library; archived v1 objects form their own library.
            version = 'v1' if '/v1/' in rel else 'current'
            groups[version].append((rel, ET.fromstring(z.read(n))))
    for version, sources in groups.items():
        alltypes, badtypes = {}, {}
        variants = set()
        for source, root in sources:
            for dt in root.findall('./Controller/DataTypes/DataType'):
                name = dt.get('Name')
                try:
                    rendered = logix_type(dt)
                    if name in alltypes and alltypes[name] != rendered:
                        variants.add(name)
                    else:
                        alltypes[name] = rendered
                except ValueError as e:
                    badtypes[name] = str(e)
        badtypes.update({n:'Conflicting UDT definitions in the same original version group' for n in variants})
        reject = closure_status(alltypes, badtypes)
        aois, typeneeded, bodies = {}, set(), []
        conflicts = set()
        for source, root in sources:
            for program in root.findall('./Controller/Programs/Program'):
                record(vendor, source + '#Program/' + program.get('Name',''), 'omitted', 'Native Program has graphical routines; full program cannot be reconstructed as ST without graph conversion')
            for aoi in root.findall('./Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition'):
                name = aoi.get('Name')
                try:
                    text, deps, native = logix_aoi(aoi, alltypes, reject)
                except ValueError as e:
                    record(vendor, source + '#' + name, 'omitted', str(e))
                    continue
                if name in aois:
                    if aois[name][0] != text:
                        conflicts.add(name)
                    else:
                        record(vendor, source + '#' + name, 'deduplicated', 'Identical AOI already present in this whole library')
                    continue
                aois[name] = (text, source, deps, native)
        for name in conflicts:
            record(vendor, name, 'omitted', f'Conflicting AOI definitions within {version}; not silently combined')
            aois.pop(name, None)
        # Some AOIs depend on other AOIs. Omit the whole dependent AOI when its
        # available exported declaration is known to have been rejected.
        all_aoi_names = {a.get('Name') for _,r in sources for a in r.findall('./Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition')}
        while True:
            remove = [name for name,(_,_,deps,_) in aois.items() if any(d in all_aoi_names and d not in aois for d in deps)]
            if not remove:
                break
            for name in remove:
                record(vendor, aois[name][1] + '#' + name, 'omitted', 'Declaration depends on an omitted AOI')
                aois.pop(name)
        parts = []
        for name, (text, source, deps, native) in sorted(aois.items()):
            parts.append(comment('Source ' + source + '; AOI ' + name) + text)
            typeneeded.update(deps)
            bodies.extend((source + '#' + name + '/' + routine, body) for routine,body in native)
        intro = comment('LogixLibraries ' + version + ': complete representable ST AOIs assembled into one source. Parameters and UDTs transcribed from original XML; vendor instructions unchanged. Unsupported alias/shared-storage AOIs omitted, not replaced with stubs.')
        write(vendor, 'LogixLibraries_' + version, intro + '\n'.join(needed_types(typeneeded, alltypes)) + '\n' + '\n'.join(parts), bodies, raw)


def main():
    run_logix()
    run_unity()
    run_plcnext()
    report = WORK/'industrial_extraction_report.tsv'
    report.write_text('vendor\tsource\taction\treason\n' + '\n'.join('\t'.join(str(v).replace('\t',' ').replace('\n',' ') for v in row) for row in RECORDS) + '\n', encoding='utf-8')
    print('Outputs:')
    for row in RECORDS:
        if row[2] == 'output':
            print('\t'.join(row))
    print('Records:', Counter(row[2] for row in RECORDS))
    print('Exact original ST bodies checked:',len(ST_BODIES))

