"""Traceable source extraction, not dialect normalization or compile validation.

Standard library only. Original bytes must be retained by the caller. Metadata
retains XML extensions and scope that cannot be represented as stand-alone ST.
"""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath


def local(e):
    return e.tag.rsplit('}', 1)[-1] if isinstance(e.tag, str) else ''


def child(e, tag):
    return next((x for x in e if local(x) == tag), None) if e is not None else None


def children(e, tag):
    return [x for x in e if local(x) == tag] if e is not None else []


def txt(e):
    return ''.join(e.itertext()) if e is not None else ''


def xml(e):
    return ET.tostring(e, encoding='unicode') if e is not None else None


def safe_name(s):
    return re.sub(r'[^\w.\-]+', '_', str(s), flags=re.UNICODE).strip('_') or 'unnamed'


def decode(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    return data.decode('utf-8-sig')


def strip_comments(s):
    # Only used to identify the opening declaration, never to modify emitted ST.
    return re.sub(r'\(\*.*?\*\)|//[^\n]*|\{[^}]*\}', '', s, flags=re.S)


def finish(decl, body='', kind=None):
    source = decl.rstrip() + ('\n\n' + body.rstrip() if body.strip() else '')
    probe = strip_comments(decl)
    m = re.search(r'(?im)^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM|INTERFACE|METHOD|PROPERTY|ACTION|TYPE)\b', probe)
    opening = kind or (m.group(1).upper() if m else None)
    closer = 'END_' + opening if opening else None
    added = []
    if closer and not re.search(r'(?im)\b' + closer + r'\b\s*;?\s*$', strip_comments(source)):
        source += '\n' + closer
        added.append(closer)
    return source.strip() + '\n', added


def unit(name, source, meta, context=False):
    return {'name': safe_name(name), 'st': source,
            'status': 'context_required' if context else 'assembled_vendor_source',
            'metadata': meta}


def unique_names(result):
    """XML may split one GVL into multiple blocks or repeat source versions."""
    seen, used = {}, set()
    for u in result['units']:
        n = u['name']
        seen[n] = seen.get(n, 0) + 1
        if n in used:
            candidate = n + '__part_' + str(seen[n])
            while candidate in used:
                seen[n] += 1
                candidate = n + '__part_' + str(seen[n])
            u['name'] = candidate
            u['metadata']['repeated_object_name'] = n
        used.add(u['name'])
    return result


def extract_twin(root, name):
    units, notes = [], []
    index = 0
    def visit(e, scope):
        nonlocal index
        tag = local(e)
        ename = e.get('Name', e.get('name', tag))
        path = scope + [ename]
        decl_node = child(e, 'Declaration')
        impl = child(e, 'Implementation')
        st = child(impl, 'ST')
        decl, body = txt(decl_node), txt(st)
        members = [x for x in e if local(x) in {'Method', 'Property', 'Action', 'Get', 'Set', 'Transition'}]
        non_st = [local(x) for x in (impl if impl is not None else []) if local(x) != 'ST']
        if decl_node is not None or st is not None:
            index += 1
            source, added = finish(decl, body)
            context = bool(scope or members or non_st or tag in {'GVL', 'Itf', 'Interface', 'Property', 'Get', 'Set'})
            meta = {'format': 'TwinCAT XML', 'source': name, 'node_kind': tag,
                    'scope': scope, 'object_name': ename, 'object_attributes': dict(e.attrib),
                    'xml_node': xml(e), 'declaration': decl, 'implementation': body,
                    'synthetic_closing_tokens': added,
                    'members': [{'kind': local(m), 'name': m.get('Name', local(m))} for m in members],
                    'non_st_implementations': non_st,
                    'binding_required': bool(scope or members)}
            if non_st:
                notes.append('/'.join(path) + ': non-ST implementation retained only in XML metadata: ' + ', '.join(non_st))
            if not source.strip():
                source = '(* Empty source member; scope and original XML are in metadata. *)\n'
            units.append(unit('__'.join(path), source, meta, context))
        for m in members:
            visit(m, path)
    for e in root:
        if local(e) in {'POU', 'DUT', 'GVL', 'Itf', 'Interface', 'Method', 'Property'}:
            visit(e, [])
    if not units:
        notes.append('TwinCAT XML contains no readable Declaration or ST node; original file retained.')
    return {'units': units, 'notes': notes}


def unwrap(v):
    if isinstance(v, dict) and '@Value' in v:
        return unwrap(v['@Value'])
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    if isinstance(v, str):
        return re.sub(r'^\((?:string|Guid|long|int|bool|DateTime)\)', '', v)
    return v


def forge_text(section):
    if not isinstance(section, dict):
        return None
    doc = section.get('TextDocument', section)
    if isinstance(doc, dict) and 'TextLines' in doc:
        lines = doc['TextLines']
        if isinstance(lines, list):
            return '\n'.join(str(x.get('Text', '')) for x in lines if isinstance(x, dict))
    return None


def extract_forge(obj, name):
    payload = obj.get('payload', {})
    if not isinstance(payload, dict) or 'object' not in payload:
        return None
    meta = unwrap(payload.get('meta', {}).get('Graph', {}))
    graph = unwrap(payload['object'].get('Graph', {}))
    if not isinstance(graph, dict):
        return {'units': [], 'notes': ['CODESYS Forge object contains no text graph.']}
    decl = forge_text(graph.get('Interface'))
    impl = forge_text(graph.get('Implementation'))
    # Some Forge type/GVL objects directly store their TextDocument.
    if decl is None:
        decl = forge_text(graph)
    if decl is None and impl is None:
        return {'units': [], 'notes': ['CODESYS Forge metadata/non-ST object; retained in original source.']}
    decl, impl = decl or '', impl or ''
    oname = meta.get('Name', PurePosixPath(name).stem)
    gid = meta.get('Guid', PurePosixPath(name).stem)
    source, added = finish(decl, impl)
    props = meta.get('Properties', {})
    metadata = {'format': 'CODESYS Forge JSON', 'source': name, 'graph_meta': meta,
                'object_name': oname, 'object_guid': gid, 'parent_guid': meta.get('ParentGuid'),
                'parent_properties': props, 'child_guids': graph.get('ChildObjectGuids', []),
                'graph_keys': list(graph), 'declaration': decl, 'implementation': impl,
                'synthetic_closing_tokens': added,
                'source_directory_scope': str(PurePosixPath(name).parent),
                'graph_nontext_metadata': {k:v for k,v in graph.items() if k not in {'Interface', 'Implementation', 'TextDocument'}},
                'binding_required': True}
    # Independent objects use Guid/ParentObjects links; no assumed flat scope.
    if not source.strip():
        source = '(* Empty property accessor/interface member; see scope metadata. *)\n'
    notes = []
    if 'Implementation' in graph and forge_text(graph['Implementation']) is None:
        notes.append('Non-text implementation retained in original object; emitted declaration needs project context.')
        metadata['nontext_implementation'] = graph['Implementation']
    return {'units': [unit(str(oname)+'__'+str(gid)[:8], source, metadata, True)], 'notes': notes}


def extract_exportfile(root, name):
    """CODESYS native textual ExportFile, with explicit named serialization fields."""
    def field(e,n):
        return next((x for x in e if x.get('Name')==n),None) if e is not None else None
    def textdoc(e):
        d=field(e,'TextDocument')
        return field(d,'TextBlobForSerialisation')
    units,notes=[],[]
    profile=next((txt(e) for e in root.iter() if e.get('Name')=='ProfileName'),None)
    for entry in root.iter():
        meta,obj=field(entry,'MetaObject'),field(entry,'Object')
        if meta is None or obj is None:continue
        interface,implementation=field(obj,'Interface'),field(obj,'Implementation')
        decl_node=textdoc(interface)
        if decl_node is None:decl_node=textdoc(obj)
        impl_node=textdoc(implementation)
        if decl_node is None and impl_node is None:continue
        decl,body=txt(decl_node),txt(impl_node)
        oname=txt(field(meta,'Name')) or 'unnamed'
        source,added=finish(decl,body)
        kind=re.search(r'(?im)^\s*(METHOD|PROPERTY|INTERFACE|ACTION)\b',strip_comments(decl))
        unsupported=implementation is not None and impl_node is None
        context=bool(kind or unsupported or txt(field(obj,'ChildObjectGuids')).strip())
        path_node=field(entry,'Path')
        metadata={'format':'CODESYS native ExportFile XML','source':name,'object_name':oname,
                  'object_guid':txt(field(meta,'Guid')),'parent_guid':txt(field(meta,'ParentGuid')),
                  'project_profile':profile,'scope_path':[txt(x) for x in (path_node if path_node is not None else [])],
                  'declaration':decl,'implementation':body,'synthetic_closing_tokens':added,
                  'original_entry_xml':xml(entry),'original_project_export_xml':xml(root),
                  'normalization_performed':False,'nontext_implementation':unsupported}
        if unsupported:notes.append(oname+': implementation is not a textual ST blob; preserve original XML context.')
        units.append(unit(oname,source,metadata,context))
    if not units:notes.append('Native ExportFile XML has no explicit ST declaration/implementation text blob.')
    return {'units':units,'notes':notes}


class PLCOpen:
    def __init__(self, root, name):
        self.root, self.name = root, name
        self.units, self.notes, self.handled_st = [], [], set()
        self.parent = {c:p for p in root.iter() for c in p}
        self.paths = {}
        def paths(e, prefix):
            label = local(e)
            ident = e.get('name', e.get('Name'))
            if ident:
                label += '[@name=' + repr(ident) + ']'
            self.paths[e] = prefix + '/' + label
            for c in e: paths(c, self.paths[e])
        paths(root, '')
        self.header = dict(child(root, 'fileHeader').attrib) if child(root, 'fileHeader') is not None else {}

    def attributes(self, e):
        lines = []
        add = child(e, 'addData')
        if add is not None:
            for x in add.iter():
                if local(x) == 'Attribute' and x.get('Name'):
                    n, v = x.get('Name'), x.get('Value')
                    lines.append("{attribute '"+n.replace("'", "$'")+"'" + (" := '"+v.replace("'", "$'")+"'" if v else '') + '}')
        return lines

    def init_value(self, e, issues):
        if e is None: return None
        tag = local(e)
        if tag in {'initialValue', 'value'} and len(e):
            result = self.init_value(e[0], issues)
            if e.get('repetitionValue'):
                result = e.get('repetitionValue') + '(' + str(result) + ')'
            return result
        if tag == 'simpleValue': return e.get('value', '')
        if tag == 'arrayValue':
            return '[' + ', '.join(self.init_value(v, issues) or '' for v in e) + ']'
        if tag == 'structValue':
            return '(' + ', '.join(v.get('member', '') + ' := ' + (self.init_value(v, issues) or '') for v in e) + ')'
        issues.append('Unsupported initializer: ' + tag)
        return '__UNRESOLVED_XML_INITIALIZER__'

    def type_text(self, e, issues):
        if e is None:
            issues.append('Missing type node')
            return '__UNRESOLVED_XML_TYPE__'
        tag = local(e)
        if tag in {'type', 'baseType', 'returnType'}:
            return self.type_text(e[0], issues) if len(e) else '__UNRESOLVED_XML_TYPE__'
        if tag == 'derived': return e.get('name', '__UNRESOLVED_XML_TYPE__')
        if tag in {'string', 'wstring'}:
            return tag.upper() + ('(' + e.get('length') + ')' if e.get('length') else '')
        if tag in {'pointer', 'reference'}:
            return ('POINTER TO ' if tag == 'pointer' else 'REFERENCE TO ') + self.type_text(child(e, 'baseType'), issues)
        if tag == 'array':
            dims = [d.get('lower', '?')+'..'+d.get('upper', '?') for d in children(e, 'dimension')]
            return 'ARRAY['+', '.join(dims)+'] OF '+self.type_text(child(e, 'baseType'), issues)
        if tag == 'struct':
            return 'STRUCT\n'+'\n'.join('    '+self.variable(v, issues) for v in children(e, 'variable'))+'\nEND_STRUCT'
        if tag == 'enum':
            vals = children(child(e, 'values'), 'value')
            s = '(' + ', '.join(v.get('name', '?') + (' := '+v.get('value') if v.get('value') is not None else '') for v in vals) + ')'
            base = child(e, 'baseType')
            if base is not None: s += ' '+self.type_text(base, issues)
            return s
        if tag in {'subrangeSigned', 'subrangeUnsigned'}:
            rg = child(e, 'range')
            if rg is None:
                issues.append('Subrange without range')
                return '__UNRESOLVED_XML_TYPE__'
            return self.type_text(child(e, 'baseType'), issues)+' ('+rg.get('lower', '?')+'..'+rg.get('upper', '?')+')'
        primitives = {'BOOL','BYTE','WORD','DWORD','LWORD','SINT','INT','DINT','LINT','USINT','UINT','UDINT','ULINT','REAL','LREAL','TIME','LTIME','DATE','LDATE','TOD','LTOD','DT','LDT','TIME_OF_DAY','DATE_AND_TIME','CHAR','WCHAR','ANY','ANY_NUM','ANY_INT','ANY_REAL','ANY_BIT','ANY_STRING','ANY_DATE','ANY_DERIVED','ANY_ELEMENTARY','VOID'}
        if tag.upper() in primitives: return tag.upper()
        issues.append('Unsupported type: ' + tag)
        return '__UNRESOLVED_XML_TYPE__'

    def variable(self, v, issues):
        attrs = self.attributes(v)
        name = v.get('name', '__UNNAMED_VARIABLE__')
        address = ' AT '+v.get('address') if v.get('address') else ''
        type_s = self.type_text(child(v, 'type'), issues)
        add = child(v, 'addData')
        if add is not None:
            # Constructor input assignments must remain distinct from initial values.
            assigns = [x for x in add.iter() if local(x) == 'InputAssignment']
            if assigns:
                issues.append('FB constructor/input assignments retained in XML metadata for '+name)
        val = self.init_value(child(v, 'initialValue'), issues)
        out = name+address+' : '+type_s+(' := '+val if val is not None else '')+';'
        doc = txt(child(v, 'documentation')).strip()
        if doc: out = '(* '+doc.replace('*)', '* )')+' *)\n'+out
        return '\n'.join(attrs+[out])

    def variables(self, interface, issues):
        out = []
        if interface is None: return out
        mapping = {'inputVars':'VAR_INPUT','outputVars':'VAR_OUTPUT','inOutVars':'VAR_IN_OUT','localVars':'VAR','tempVars':'VAR_TEMP','externalVars':'VAR_EXTERNAL','globalVars':'VAR_GLOBAL','accessVars':'VAR_ACCESS'}
        for block in interface:
            tag = local(block)
            if tag not in mapping: continue
            kw = mapping[tag]
            for a, word in [('constant','CONSTANT'),('retain','RETAIN'),('nonretain','NON_RETAIN'),('persistent','PERSISTENT')]:
                if block.get(a, '').lower() == 'true': kw += ' '+word
            out.extend(self.attributes(block))
            out.append(kw)
            out.extend(self.variable(v, issues) for v in children(block, 'variable'))
            out.append('END_VAR')
        return out

    def metadata(self, e, issues, kind):
        return {'format': 'PLCopen XML', 'source': self.name, 'source_node': self.paths[e],
                'node_kind': kind, 'object_attributes': dict(e.attrib), 'file_header': self.header,
                'original_node_xml': xml(e), 'unresolved': list(issues),
                'scope_xml_path': self.paths.get(self.parent.get(e), ''),
                'dialect_normalized': False}

    def add(self, e, name, source, issues, kind, context=False):
        self.units.append(unit(name, source.rstrip()+'\n', self.metadata(e, issues, kind), context or bool(issues)))
        self.notes.extend(name+': '+x for x in issues)

    def scope_name(self, e):
        out = []
        p = self.parent.get(e)
        while p is not None:
            if local(p) in {'pou','Interface','Property','Method','configuration','resource'}:
                out.insert(0, p.get('name', p.get('Name', local(p))))
            p = self.parent.get(p)
        return out

    def body(self, e, issues):
        body = child(e, 'body')
        if body is None: return ''
        st = child(body, 'ST')
        if st is not None:
            self.handled_st.add(st)
            return txt(st)
        langs = [local(x) for x in body]
        issues.append('Non-ST body retained in XML metadata: '+', '.join(langs))
        return '(* Original body is '+', '.join(langs)+'. See XML metadata; no behavioral conversion performed. *)'

    def callable(self, e):
        tag = local(e)
        issues = []
        name = e.get('name', e.get('Name', tag))
        scope = self.scope_name(e)
        kind = {'Method':'METHOD','Property':'PROPERTY','Interface':'INTERFACE','action':'ACTION','transition':'TRANSITION'}.get(tag)
        if tag == 'pou':
            kind = {'function':'FUNCTION','functionBlock':'FUNCTION_BLOCK','program':'PROGRAM'}.get(e.get('pouType'))
        if kind is None:
            kind = 'PROGRAM'; issues.append('Unknown POU type; placeholder PROGRAM wrapper requires review')
        iface = child(e, 'interface')
        head = kind+' '+name
        rt = child(iface, 'returnType')
        if rt is not None: head += ' : '+self.type_text(rt, issues)
        add = child(iface, 'addData')
        if add is not None:
            for x in add.iter():
                if local(x) == 'Extends' and txt(x).strip(): head += ' EXTENDS '+txt(x).strip()
                if local(x) == 'Implements' and txt(x).strip(): head += ' IMPLEMENTS '+txt(x).strip()
            for x in add.iter():
                if local(x) == 'AccessModifiers': issues.append('Access modifiers retained in XML metadata')
        parts = self.attributes(e) + self.attributes(iface) + [head] + self.variables(iface, issues)
        parts.append(self.body(e, issues))
        parts.append('END_'+kind)
        nested = [x for x in e.iter() if x is not e and local(x) in {'Method','Property','action','transition'}]
        if nested: issues.append('Member objects emitted separately; use their scope metadata')
        self.add(e, '__'.join(scope+[name]), '\n'.join(p for p in parts if p), issues, kind, bool(scope) or tag == 'Interface')

    def run(self):
        for e in self.root.iter():
            tag = local(e)
            if tag == 'dataType':
                issues=[]; name=e.get('name','unnamed')
                s='\n'.join(self.attributes(e)+['TYPE '+name+' : '+self.type_text(child(e,'baseType'),issues)])
                val=self.init_value(child(e,'initialValue'),issues)
                if val is not None:s+=' := '+val
                self.add(e,name,s+';\nEND_TYPE',issues,'TYPE')
            elif tag in {'pou','Method','Property','Interface'} or (tag == 'action' and (e.get('name') or child(e,'body') is not None)):
                self.callable(e)
            elif tag == 'globalVars':
                issues=[]
                wrap=ET.Element('interface');wrap.append(e)
                name=e.get('name','GlobalVars')
                self.add(e,'__'.join(self.scope_name(e)+[name]),'\n'.join(self.variables(wrap,issues)),issues,'VAR_GLOBAL',True)
        # Capture every remaining explicit ST node, including transition guards
        # or vendor extensions, without pretending it is a standalone POU.
        for idx, e in enumerate(self.root.iter()):
            if local(e) == 'ST' and e not in self.handled_st:
                self.add(e,'__'.join(self.scope_name(e)+['ST_fragment_'+str(idx)]),txt(e),
                         ['ST fragment needs surrounding XML/project context'],'ST_fragment',True)
                self.handled_st.add(e)
        non_st = [self.paths[e] for e in self.root.iter() if local(e) in {'FBD','LD','SFC','IL'}]
        if non_st:self.notes.append('Non-ST networks are not converted: '+str(len(non_st))+' nodes. Original XML retains them.')
        self.notes.append('PLCopen extraction preserves project/configuration and dependency semantics in original XML; emitted units are not a rebuilt runnable project.')
        return {'units':self.units,'notes':self.notes}


def extract(name: str, data: bytes, files: dict[str, bytes]) -> dict | None:
    suffix = PurePosixPath(name.replace('\\','/')).suffix.lower()
    if suffix in {'.object','.meta'} and data.startswith(b'\x02 \x09('):
        return {'units': [], 'notes': ['CODESYS/TwinCAT binary serialized object; vendor IDE text/PLCopen export required. Binary strings are not treated as source.']}
    if suffix in {'.object','.json','.txt'}:
        try:
            s = decode(data)
            if s.lstrip().startswith('{'):
                obj = json.loads(s)
                if isinstance(obj,dict):
                    result=extract_forge(obj,name)
                    if result is not None:return result
        except (UnicodeError,ValueError): pass
    # Parse only formats potentially containing XML, never decompress here.
    if suffix not in {'.xml','.tcpou','.tcdut','.tcgvl','.tcio','.tcitf','.export','.plcopenxml'}:
        return None
    try: root=ET.fromstring(data)
    except (ET.ParseError,ValueError):return None
    if local(root)=='TcPlcObject':return unique_names(extract_twin(root,name))
    if local(root)=='ExportFile':return unique_names(extract_exportfile(root,name))
    if local(root)=='project' and ('plcopen.org/xml' in root.tag or child(root,'types') is not None):
        return unique_names(PLCOpen(root,name).run())
    return None
