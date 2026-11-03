"""Rebuild whole ST source projects from explicit TwinCAT/PLCopen/Forge text.

Only .st files are published under outputs. Diagnostic data stays beside this
script, never in the delivery directory. There is no binary-string guessing.
"""
from __future__ import annotations
import collections, hashlib, io, json, posixpath, re, zipfile
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

from .extractor_codesys import local,child,children,txt,unwrap,forge_text,PLCOpen,strip_comments
from . import naming, provenance

# Configured by the extraction driver: corpus root and the work directory.
BASE=None
HERE=None
VENDORS=['01_CODESYS3','03_TwinCAT3','09_Schneider_Machine_Expert','12_ABB_AC500','13_WAGO','14_Bosch_ctrlX','26_Festo','32_Beremiz_MATIEC','33_OpenPLC_v3']
TWIN={'.tcpou','.tcdut','.tcgvl','.tcio','.tcitf'}
WANTED=TWIN|{'.object','.xml','.export','.plcopenxml','.plcproj'}
ARCHIVES={'.zip','.library','.project','.package'}
OPEN=re.compile(r'(?im)^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM|INTERFACE|METHOD|PROPERTY|ACTION|TRANSITION|TYPE)\b')

def safe(s):
    return re.sub(r'[^\w.\-]+','_',str(s),flags=re.U).strip('_.')[:150] or 'library'

def norm(s):return posixpath.normpath(str(s).replace('\\','/')).casefold()
def sha(data):return hashlib.sha256(data).hexdigest()
def comment(s):return '(* '+s.replace('*)','* )')+' *)'
def kindof(s):
    m=OPEN.search(strip_comments(s));return m.group(1).upper() if m else None
def uncommented(s):return re.sub(r'\{[^}]*\}','',strip_comments(s)).strip()

def lexical_error(s):
    """Reject open comments/strings that would consume the next merged object."""
    i=0
    while i<len(s):
        if s.startswith('//',i):
            n=s.find('\n',i);i=len(s) if n<0 else n;continue
        if s.startswith('(*',i):
            depth=1;i+=2
            while i<len(s) and depth:
                if s.startswith('(*',i):depth+=1;i+=2
                elif s.startswith('*)',i):depth-=1;i+=2
                else:i+=1
            if depth:return 'unterminated block comment'
        elif s[i] in "'\"":
            q=s[i];i+=1;closed=False
            while i<len(s):
                if s[i]=='$':i+=2
                elif s[i]==q:
                    if i+1<len(s) and s[i+1]==q:i+=2
                    else:i+=1;closed=True;break
                else:i+=1
            if not closed:return 'unterminated quoted literal'
        else:i+=1
    return None

def close_text(decl,body='',members=(),kind=None):
    """Put member definitions before their parent's final closing token."""
    k=kind or kindof(decl)
    closer='END_'+k if k else None
    d=decl.rstrip()
    if closer:
        d=re.sub(r'(?im)\s*\b'+closer+r'\b\s*;?\s*$', '', d).rstrip()
    parts=[d]
    if body.strip():parts.append(body.rstrip())
    parts.extend(x.rstrip() for x in members if x.strip())
    if closer:parts.append(closer)
    return '\n\n'.join(x for x in parts if x).strip()+'\n'

def parse(data):
    try:return ET.fromstring(data)
    except (ET.ParseError,ValueError,UnicodeError):return None

def read_vfs(base):
    """Files, plus virtual archive members. Byte duplicates are later collapsed."""
    files={}; errors=[]; archives_seen=set()
    def put(name,data):
        if PurePosixPath(name).suffix.lower() in WANTED:files[norm(name)]=(name,data)
    def archive(name,data,depth=0):
        if depth>3:return
        key=sha(data)
        if key in archives_seen:return
        archives_seen.add(key)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.is_dir():continue
                    ext=PurePosixPath(info.filename).suffix.lower()
                    if ext not in WANTED|ARCHIVES:continue
                    if info.file_size>64*1024*1024:continue
                    inner=z.read(info)
                    virtual=name+'!/'+info.filename.replace('\\','/')
                    if ext in WANTED:put(virtual,inner)
                    elif inner.startswith(b'PK'):archive(virtual,inner,depth+1)
        except (zipfile.BadZipFile,RuntimeError,OSError) as e:errors.append([name,str(e)])
    for p in sorted(base.rglob('*')):
        if not p.is_file() or '_调查与历史记录' in p.parts:continue
        ext=p.suffix.lower()
        if ext not in WANTED|ARCHIVES:continue
        try:
            data=p.read_bytes(); name=p.relative_to(base).as_posix()
            if ext in WANTED:put(name,data)
            elif data.startswith(b'PK'):archive(name,data)
        except OSError as e:errors.append([str(p),str(e)])
    return files,errors

def twin_node(e,stats,issues,path=''):
    tag=local(e); name=e.get('Name',tag); label=path+'/'+name
    decl=txt(child(e,'Declaration')); impl=child(e,'Implementation')
    langs=[local(x) for x in (impl if impl is not None else ()) if local(x)!='ST']
    if langs:
        issues.append(label+': omitted non-ST implementation '+','.join(langs));stats['non_st_objects_omitted']+=1
        return ''
    st=child(impl,'ST'); body=txt(st)
    # TwinCAT Actions have an explicit Name and implementation, and normally
    # have no Declaration element. This is complete serialized source.
    if not decl.strip() and tag in {'Action','Transition'} and st is not None:
        decl=tag.upper()+' '+name
    problem=lexical_error(decl) or lexical_error(body)
    if problem:
        issues.append(label+': omitted original lexical error: '+problem);stats['lexically_invalid_objects_omitted']+=1;return ''
    members=[]
    for m in e:
        if local(m) in {'Method','Property','Action','Get','Set','Transition'}:
            s=twin_node(m,stats,issues,label)
            if s:members.append(s)
    if tag in {'Get','Set'}:
        # An empty accessor is an explicit interface contract, not a placeholder.
        k=tag.upper();stats['accessors']+=1
        return k+'\n'+close_text(decl,body,members,kind=None).rstrip()+'\nEND_'+k+'\n'
    if not uncommented(decl):
        if body.strip():issues.append(label+': missing declaration; object omitted')
        return ''
    k=kindof(decl)
    if k in {'FUNCTION','FUNCTION_BLOCK','PROGRAM','METHOD','ACTION','TRANSITION'}:
        # Explicit abstract/interface contracts are valid with no implementation.
        if impl is None and tag=='POU' and not re.search(r'\bABSTRACT\b',decl,re.I) and not members:
            issues.append(label+': no implementation node; object omitted');stats['missing_implementation_omitted']+=1;return ''
    stats['objects']+=1
    if tag in {'GVL','DUT','Itf'}:stats[tag]+=1
    if tag in {'Method','Property','Action'}:stats[tag]+=1
    return close_text(decl,body,members)

def twin_source(root,name,stats,issues):
    out=[]
    for e in root:
        if local(e) not in {'POU','DUT','GVL','Itf','Interface'}:continue
        s=twin_node(e,stats,issues)
        if s:out.append(comment('Object: '+e.get('Name',local(e)))+'\n'+s)
    return '\n'.join(out)

class WholePLCOpen(PLCOpen):
    def __init__(self,root,name,stats):
        super().__init__(root,name);self.stats=stats;self.issues=[];self.used=set();self.emitted_pous=0
    def variable(self,v,issues):
        name=v.get('name','__UNNAMED_VARIABLE__')
        address=' AT '+v.get('address') if v.get('address') else ''
        type_s=self.type_text(child(v,'type'),issues)
        add=child(v,'addData');assignments=[]
        if add is not None:
            for x in add.iter():
                if local(x)=='InputAssignment':
                    n=txt(child(x,'Name'));value=txt(child(x,'Value'))
                    if not value:issues.append('Unsupported empty FB input assignment for '+name)
                    else:assignments.append(n+' := '+value if n else value)
        if assignments:type_s+=' ('+', '.join(assignments)+')'
        value=self.init_value(child(v,'initialValue'),issues)
        source=name+address+' : '+type_s+(' := '+value if value is not None else '')+';'
        doc=txt(child(v,'documentation')).strip()
        if doc:source=comment(doc)+'\n'+source
        return '\n'.join(self.attrs_direct(v)+[source])
    def attrs_direct(self,e):
        # Attributes on descendant methods must not leak onto the enclosing POU.
        lines=[];add=child(e,'addData')
        if add is not None:
            for d in add:
                for x in d:
                    if local(x)=='Attributes':
                        for a in x:
                            n=a.get('Name');v=a.get('Value')
                            if n:lines.append("{attribute '"+n.replace("'","$'")+"'"+(" := '"+v.replace("'","$'")+"'" if v else '')+'}')
        return lines
    def access(self,e,iface):
        result=[]
        for owner in [e,iface]:
            add=child(owner,'addData')
            if add is None:continue
            for d in add:
                for x in d:
                    if local(x)=='AccessModifiers':
                        for a in x:
                            tag=local(a).upper()
                            if tag in {'PUBLIC','PRIVATE','PROTECTED','INTERNAL','ABSTRACT','FINAL'}:
                                result.append(tag)
                        for a,v in x.attrib.items():
                            if v.lower()=='true' and a.upper() in {'PUBLIC','PRIVATE','PROTECTED','INTERNAL','ABSTRACT','FINAL'}:result.append(a.upper())
        return list(dict.fromkeys(result))
    def nested_members(self,e):
        allowed={'Method','Property','Interface','action','transition','Get','Set'}
        result=[]
        def visit(x):
            for c in x:
                if local(c) in allowed:result.append(c)
                elif local(c) not in {'body','interface','dataType','pou','globalVars'}:visit(c)
        visit(e);return result
    def callable_text(self,e,parentkind=None):
        tag=local(e);name=e.get('name',e.get('Name',tag));self.used.add(e)
        iface=child(e,'interface');body=child(e,'body');st=child(body,'ST')
        if body is not None and st is None and len(body):
            self.issues.append(name+': omitted non-ST body '+','.join(local(x) for x in body));self.stats['non_st_objects_omitted']+=1;return ''
        problem=lexical_error(txt(st))
        if problem:
            self.issues.append(name+': omitted original lexical error: '+problem);self.stats['lexically_invalid_objects_omitted']+=1;return ''
        kind={'Method':'METHOD','Property':'PROPERTY','Interface':'INTERFACE','action':'ACTION','transition':'TRANSITION','Get':'GET','Set':'SET'}.get(tag)
        if tag=='pou':kind={'function':'FUNCTION','functionBlock':'FUNCTION_BLOCK','program':'PROGRAM'}.get(e.get('pouType'))
        if kind is None:self.issues.append(name+': unsupported object kind');return ''
        issues=[]
        head=kind+(' '+name if kind not in {'GET','SET'} else '')
        access=self.access(e,iface)
        if access and kind not in {'GET','SET'}:head=kind+' '+' '.join(access)+' '+name
        rt=child(iface,'returnType')
        if rt is not None:head+=' : '+self.type_text(rt,issues)
        add=child(iface,'addData')
        if add is not None:
            for d in add:
                for x in d.iter():
                    if local(x) in {'Extends','Implements'} and txt(x).strip():head+=' '+local(x).upper()+' '+txt(x).strip()
        parts=self.attrs_direct(e)+self.attrs_direct(iface)+[head]+self.variables(iface,issues)
        if st is not None:parts.append(txt(st));self.handled_st.add(st)
        for m in self.nested_members(e):
            s=self.callable_text(m,kind)
            if s:parts.append(s)
        # Interface and abstract members may intentionally have no body.
        if tag=='pou' and body is None and kind!='INTERFACE' and not self.nested_members(e) and 'ABSTRACT' not in access:
            self.issues.append(name+': missing POU body; omitted');self.stats['missing_implementation_omitted']+=1;return ''
        critical=[x for x in issues if x.startswith(('Unsupported','Missing','Subrange','FB constructor'))]
        if critical:
            self.issues.extend(name+': '+x for x in critical);self.stats['unsupported_objects_omitted']+=1;return ''
        parts.append('END_'+kind);self.stats['objects']+=1;self.stats[kind]+=1
        return '\n'.join(p for p in parts if p)+'\n'
    def whole(self):
        out=[]
        for e in self.root.iter():
            tag=local(e)
            if tag=='dataType':
                issues=[];name=e.get('name','unnamed')
                s='\n'.join(self.attrs_direct(e)+['TYPE '+name+' : '+self.type_text(child(e,'baseType'),issues)])
                val=self.init_value(child(e,'initialValue'),issues)
                if val is not None:s+=' := '+val
                if issues:self.issues.extend(name+': '+x for x in issues);self.stats['unsupported_objects_omitted']+=1;continue
                out.append(s+';\nEND_TYPE\n');self.stats['objects']+=1;self.stats['TYPE']+=1
            elif tag in {'pou','Interface'} and e not in self.used:
                # Nested interfaces are handled by their enclosing callable.
                if any(local(p) in {'pou','Method','Property','Interface'} for p in self.ancestors(e)):continue
                s=self.callable_text(e)
                if s:
                    out.append(s)
                    if tag=='pou':self.emitted_pous+=1
            elif tag=='globalVars':
                if any(local(p)=='globalVars' for p in self.ancestors(e)):continue
                if not children(e,'variable'):continue
                issues=[];w=ET.Element('interface')
                mixed=[x for x in e.iter() if local(x)=='MixedAttrsVarList']
                if mixed:
                    for m in mixed:
                        for block in m:
                            if local(block)=='globalVars':w.append(block)
                else:w.append(e)
                s='\n'.join(self.variables(w,issues))
                if issues:self.issues.extend(e.get('name','GVL')+': '+x for x in issues);continue
                scope='/'.join(p.get('name','') for p in reversed(list(self.ancestors(e))) if local(p) in {'configuration','resource'})
                out.append(comment('Global variable list: '+scope+'/'+e.get('name','GlobalVars'))+'\n'+s+'\n');self.stats['objects']+=1;self.stats['GVL']+=1
        if not self.emitted_pous and any(local(e)=='pou' for e in self.root.iter()):
            self.issues.append('Project omitted: no ST POU remains; declarations alone would form a non-ST project shell.')
            return '',self.issues
        return '\n'.join(out),self.issues
    def ancestors(self,e):
        p=self.parent.get(e)
        while p is not None:yield p;p=self.parent.get(p)

def forge_record(name,data):
    if not data.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'{'):return None
    try:
        o=json.loads(data);p=o.get('payload',{});m=unwrap(p['meta']['Graph']);g=unwrap(p['object']['Graph'])
        if not isinstance(g,dict) or not isinstance(m,dict):return None
        d=forge_text(g.get('Interface'));i=forge_text(g.get('Implementation'))
        if d is None:d=forge_text(g)
        return dict(name=name,guid=m.get('Guid'),parent=m.get('ParentGuid'),meta=m,graph=g,decl=d or '',body=i or '',nonst=('Implementation' in g and i is None),hastext=(d is not None or i is not None))
    except (ValueError,KeyError,TypeError):return None

def forge_whole(records,stats):
    byid={r['guid']:r for r in records if r['guid']};bydir={str(PurePosixPath(r['name']).parent):r for r in records};kid=collections.defaultdict(list);issues=[];used=set()
    for r in records:
        parent=byid.get(r['parent'])
        if parent is None:
            # Forge ParentGuid may refer to a non-text folder. Filesystem nesting
            # is the authoritative fallback for those exports.
            pp=PurePosixPath(r['name']).parent.parent
            while str(pp) not in {'.','/'}:
                candidate=bydir.get(str(pp))
                if candidate is not None:parent=candidate;break
                pp=pp.parent
        if parent is not None:kid[parent['name']].append(r);r['parent_record']=parent
    def emit(r,parentkind=None):
        if r['name'] in used:return ''
        used.add(r['name'])
        if r['nonst']:
            issues.append(r['name']+': non-ST graph omitted');stats['non_st_objects_omitted']+=1;return ''
        d=r['decl'];k=kindof(d)
        problem=lexical_error(d) or lexical_error(r['body'])
        if problem:
            issues.append(r['name']+': omitted original lexical error: '+problem);stats['lexically_invalid_objects_omitted']+=1;return ''
        accessor=r['meta'].get('Name','').upper() in {'GET','SET'} and parentkind=='PROPERTY'
        members=[emit(x,k or parentkind) for x in sorted(kid[r['name']],key=lambda x:x['name'])]
        if accessor:
            a=r['meta']['Name'].upper();stats['accessors']+=1
            return a+'\n'+close_text(d,r['body'],members).rstrip()+'\nEND_'+a+'\n'
        if k or re.match(r'(?is)^\s*(?:\{[^}]*\}\s*)*VAR_GLOBAL\b',strip_comments(d)):
            stats['objects']+=1;stats[k or 'GVL']+=1
            return close_text(d,r['body'],members)
        if not r['hastext']:return '\n'.join(x for x in members if x)
        if uncommented(d+r['body']):issues.append(r['name']+': non-POU text omitted');stats['non_code_objects_omitted']+=1
        return '\n'.join(x for x in members if x)
    roots=[r for r in records if 'parent_record' not in r]
    out=[]
    for r in sorted(roots,key=lambda x:x['name']):
        s=emit(r)
        if s:out.append(s)
    return '\n'.join(out),issues

class Builder:
    def __init__(self,vendor):
        self.vendor=vendor;self.stats=collections.Counter();self.events=[];self.signatures=set();self.files={};self.consumed=set();self.outputs=[]
    def publish(self,label,text,sources,issues=()):
        if not uncommented(text):return
        # Per vendor text-source signature prevents ZIP/unpacked duplicate output.
        sig=sha(text.replace('\r\n','\n').encode('utf-8'))
        if sig in self.signatures:self.stats['duplicate_projects_skipped']+=1;return
        self.signatures.add(sig)
        dest=HERE/'outputs'/self.vendor/'.st文件';dest.mkdir(parents=True,exist_ok=True)
        name='xml_'+safe(label)+'.st'
        header=comment('Library / project: '+label)+'\n'
        if issues:header+=comment('ST source subset; non-ST or unsupported objects remain in original project.')+'\n'
        delivered=header+'\n'+text.rstrip()+'\n'
        if naming.exists(dest/name) and (dest/name).read_text(encoding='utf-8')!=delivered:name='xml_'+safe(label)+'_'+sig[:8]+'.st'
        target=dest/name;target.write_text(delivered,encoding='utf-8',newline='\n')
        provenance.register(target,sources,prefix=self.vendor+'/原始库文件/')
        self.outputs.append(dict(file=str(target),label=label,source_count=len(sources),issues=list(issues),source_files=sources,sha256=sig))
        self.stats['libraries_or_projects']+=1;self.stats['source_files']+=len(sources)
    def run(self):
        self.files,errors=read_vfs(BASE/self.vendor/'原始库文件');self.events+=errors
        # Physical paths sort before archives; identical project source sets are
        # deduplicated on final reconstructed text.
        for key,(name,data) in sorted(self.files.items(),key=lambda x:('!/' in x[1][0],x[1][0])):
            if PurePosixPath(name).suffix.lower()!='.plcproj':continue
            root=parse(data)
            if root is None:continue
            project_name=next((txt(x) for x in root.iter() if local(x)=='Name'),PurePosixPath(name).stem)
            source=[];parts=[];issues=[];before=self.stats.copy()
            for e in root.iter():
                if local(e)!='Compile' or not e.get('Include'):continue
                fn=norm(posixpath.join(posixpath.dirname(name),e.get('Include').replace('\\','/')))
                if PurePosixPath(fn).suffix.lower() not in TWIN:continue
                if fn not in self.files:issues.append('Missing Compile source: '+e.get('Include'));self.stats['missing_project_sources']+=1;continue
                sn,sd=self.files[fn];sr=parse(sd)
                if sr is None or local(sr)!='TcPlcObject':issues.append('Unreadable Compile source: '+sn);continue
                s=twin_source(sr,sn,self.stats,issues)
                self.consumed.add(fn)
                if s:parts.append(s);source.append(sn)
            # A project itself is the boundary; include subdirectory component
            # when two projects in the same repository share an identical name.
            repo=PurePosixPath(name).parts[1] if len(PurePosixPath(name).parts)>1 else ''
            label=safe(repo)+'__'+safe(project_name)
            if project_name=='OC_Core':
                self.events.append([name,'Whole project excluded: original unterminated pragma string and GVL-owned property cannot be faithfully represented in this plain ST export.'])
                self.stats['projects_excluded_for_incomplete_representation']+=1
                continue
            self.publish(label,'\n'.join(parts),source,issues)
            if issues:self.events.append([name,issues])
        # Group unreferenced TwinCAT objects by their source tree, not per POU.
        loose=collections.defaultdict(list)
        for key,(name,data) in self.files.items():
            if key in self.consumed or PurePosixPath(name).suffix.lower() not in TWIN:continue
            if any(x in name.casefold() for x in ['/output/','/fixtures/','.expected.']):
                self.stats['unprojected_test_snapshots_excluded']+=1;continue
            root=parse(data)
            if root is None or local(root)!='TcPlcObject':continue
            pp=PurePosixPath(name)
            # Exact project directory, if one exists, gives the proper boundary.
            candidates=[(k,n) for k,(n,d) in self.files.items() if n.lower().endswith('.plcproj') and norm(name).startswith(norm(str(PurePosixPath(n).parent))+'/')]
            if candidates:continue # excluded by that project's own Compile list
            grouping=pp.parts[:2] if '!/' not in name else name.split('!/',1)[0]
            grouping='/'.join(grouping) if isinstance(grouping,tuple) else grouping
            loose[grouping].append((name,root))
        for grouping,items in loose.items():
            parts=[];issues=[]
            for name,root in sorted(items):
                s=twin_source(root,name,self.stats,issues)
                if s:parts.append(s)
            self.publish(safe(grouping)+'__unprojected_source_tree','\n'.join(parts),[n for n,r in items],issues)
        forge=collections.defaultdict(list)
        for key,(name,data) in sorted(self.files.items()):
            ext=PurePosixPath(name).suffix.lower()
            if ext=='.object':
                record=forge_record(name,data)
                if record:
                    pp=PurePosixPath(name)
                    if '!/' in name:group=name.split('!/',1)[0]
                    else:
                        group='/'.join(pp.parts[:2])
                    forge[group].append(record)
                elif data and not data.lstrip().startswith(b'<'):self.stats['binary_objects_unreadable']+=1
            elif ext in {'.xml','.export','.plcopenxml'}:
                root=parse(data)
                if root is None:continue
                if local(root)=='project' and ('plcopen.org/xml' in root.tag or child(root,'types') is not None):
                    # Test compiler suites intentionally include invalid samples.
                    if '/matiec' in name.casefold() or 'matiec_src/' in name.casefold():
                        self.stats['compiler_fixture_xml_excluded']+=1;continue
                    if self.vendor=='26_Festo' and ('fixtures/' in name.casefold() or 'knowledge/' in name.casefold()):
                        self.stats['synthetic_fixture_xml_excluded']+=1;continue
                    processor=WholePLCOpen(root,name,self.stats);s,issues=processor.whole()
                    label=safe(PurePosixPath(name).parent.name)+'__'+safe(PurePosixPath(name).stem)
                    if self.vendor=='32_Beremiz_MATIEC':label=safe(str(PurePosixPath(name).parent).replace('/','__'))
                    self.publish(label,s,[name],issues)
                    if issues:self.events.append([name,issues])
                elif local(root)=='ExportFile':
                    records=exportfile_records(root,name)
                    if records:
                        s,issues=forge_whole(records,self.stats);self.publish(PurePosixPath(name).stem,s,[name],issues)
                        if issues:self.events.append([name,issues])
        for group,records in forge.items():
            s,issues=forge_whole(records,self.stats)
            self.publish(safe(group)+'__Forge_project',s,[r['name'] for r in records],issues)
            if issues:self.events.append([group,issues])
        return dict(vendor=self.vendor,stats=dict(self.stats),outputs=self.outputs,events=self.events)

def exportfile_records(root,name):
    def field(e,n):return next((x for x in e if x.get('Name')==n),None) if e is not None else None
    def doc(e):return field(field(e,'TextDocument'),'TextBlobForSerialisation')
    records=[]
    for entry in root.iter():
        meta,obj=field(entry,'MetaObject'),field(entry,'Object')
        if meta is None or obj is None:continue
        dn=doc(field(obj,'Interface'))
        if dn is None:dn=doc(obj)
        inn=field(obj,'Implementation');bn=doc(inn)
        gid=txt(field(meta,'Guid'));oname=txt(field(meta,'Name'))
        pathnode=field(entry,'Path');pathparts=[txt(x) for x in (pathnode if pathnode is not None else ())]
        records.append(dict(name=name+'!/'+('/'.join(pathparts) or gid)+'/'+gid+'.object',guid=gid,parent=txt(field(meta,'ParentGuid')),meta={'Name':oname},graph={},decl=txt(dn),body=txt(bn),nonst=(inn is not None and bn is None),hastext=(dn is not None or bn is not None)))
    return records
