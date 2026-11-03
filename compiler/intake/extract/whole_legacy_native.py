"""Whole old-style library exports. Output trees contain ST only."""
from pathlib import Path, PurePosixPath
from collections import Counter, defaultdict
import csv
import hashlib
import io
import re
import struct
import zipfile

from . import extractor_native as native
from . import extractor_pcworx as pcworx
from . import naming, provenance

# Configured by the extraction driver: corpus root, work and output directories.
BASE = None
WORK = None
OUT = None
VENDORS = {'codesys':'02_CODESYS23','beckhoff':'04_TwinCAT2','tia':'05_Siemens_TIA_SCL','s7':'06_Siemens_Classic_SCL','pcworx':'16_Phoenix_PC_Worx'}
RECORDS = []
OUTPUT_SEEN = {}
ARCHIVES_SEEN = set()
SOURCE_SEEN = set()
FRAME_STATS = Counter()
SKIP_SOURCES = set()


def report(vendor, source, action, reason):
    RECORDS.append((vendor or 'unassigned', source, action, reason))


def nl(t):
    return t.replace('\r\r\n','\n').replace('\r\n','\n').replace('\r','\n')


def decode(b, source=''):
    if 'LIB_PLC' in source:
        if b.startswith((b'\xef\xbb\xbf',b'\xff\xfe',b'\xfe\xff')):
            t,_,_=native.decode(b)
            return nl(t)
        try:
            return nl(b.decode('utf-8'))
        except UnicodeDecodeError:
            # Source repository is Russian Windows SCL, confirmed against its
            # Russian comments/README. No replacement decoding is permitted.
            return nl(b.decode('cp1251'))
    t,_,_=native.decode(b)
    return nl(t)


def cmt(s):
    return '(* ' + s.replace('(*','( *').replace('*)','* )') + ' *)\n'


def strip_comments(text):
    """Remove nested ST and C-style comments while preserving literal values."""
    out=[];i=0
    while i<len(text):
        if text.startswith('(*',i) or text.startswith('/*',i):
            opener=text[i:i+2];closer='*)' if opener=='(*' else '*/';i+=2;depth=1
            while i<len(text) and depth:
                if opener=='(*' and text.startswith(opener,i):depth+=1;i+=2
                elif text.startswith(closer,i):depth-=1;i+=2
                else:i+=1
            out.append(' ')
        elif text.startswith('//',i):
            i=text.find('\n',i)
            if i<0:break
            out.append('\n');i+=1
        elif text[i] in '\'"':
            q=text[i];start=i;i+=1
            while i<len(text):
                if text[i]=='$':i+=2
                elif text[i]==q:
                    if i+1<len(text) and text[i+1]==q:i+=2
                    else:i+=1;break
                else:i+=1
            out.append(text[start:i])
        else:out.append(text[i]);i+=1
    return ''.join(out)


def canonical(text):
    tokens=re.findall(r"'(?:\$.|''|[^'])*'|\"(?:\$.|\"\"|[^\"])*\"|[A-Za-z_]\w*|\d+(?:\.\d+)?|\S",strip_comments(text))
    return '\x1f'.join(t if t[:1] in '\'"' else t.lower() for t in tokens)


START=re.compile(r'(?im)^[ \t]*(FUNCTION_BLOCK|FUNCTION|PROGRAM|DATA_BLOCK|ORGANIZATION_BLOCK|TYPE|VAR_GLOBAL)\b')


def units(text):
    masked=native.mask(text)
    result=[];last=0
    for m in START.finditer(masked):
        if m.start()<last:continue
        kind=m.group(1).upper()
        endkind='END_VAR' if kind=='VAR_GLOBAL' else 'END_'+kind
        end=re.search(r'(?im)^[ \t]*'+endkind+r'\b[^\n]*',masked[m.end():])
        if not end:return None
        stop=m.end()+end.end();last=stop
        header=text[m.end():text.find('\n',m.end()) if '\n' in text[m.end():] else len(text)].strip()
        nm=re.match(r'("[^"]+"|[A-Za-z_][\w]*)',header)
        name=nm.group(1).casefold() if nm else ''
        if kind=='VAR_GLOBAL':name=''
        part=text[m.start():stop]
        if kind=='VAR_GLOBAL' and re.fullmatch(r'(?is)\s*VAR_GLOBAL\s*END_VAR\s*;?\s*',strip_comments(part)):
            continue
        result.append((kind,name,part))
    return result


def complete(text):
    parsed=units(text)
    if not parsed:return False
    masked=native.mask(text)
    for kind in ('FUNCTION_BLOCK','FUNCTION','PROGRAM','DATA_BLOCK','ORGANIZATION_BLOCK','TYPE'):
        if len(re.findall(r'(?im)^[ \t]*'+kind+r'\b',masked))!=len(re.findall(r'(?im)^[ \t]*END_'+kind+r'\b',masked)):
            return False
    return True


def lib_fingerprint(text):
    parsed=units(text)
    if not parsed:return hashlib.sha256(canonical(text).encode()).hexdigest()
    return hashlib.sha256('\n'.join(sorted(k+'|'+n+'|'+canonical(t) for k,n,t in parsed)).encode()).hexdigest()


def safe_name(s):
    s=s.replace('!','_')
    s=re.sub(r'[^\w. -]+','_',s,flags=re.U).strip(' ._')
    if len(s)>150:s=s[:130]+'_'+hashlib.sha256(s.encode()).hexdigest()[:12]
    return 'native_'+s


def write(vendor,name,text,source,count=None):
    if not complete(text):
        report(vendor,source,'omitted','No complete source units after assembly')
        return
    fingerprint=lib_fingerprint(text)
    if (vendor,fingerprint) in OUTPUT_SEEN:
        report(vendor,source,'deduplicated','Same complete library source as '+OUTPUT_SEEN[vendor,fingerprint])
        return
    folder=OUT/vendor/'.st文件'
    base_name=safe_name(name)
    budget=naming.NAME_BUDGET
    if len(base_name)>budget:
        base_name=base_name[:max(12,budget-13)]+'_'+hashlib.sha256(base_name.encode()).hexdigest()[:12]
    path=folder/(base_name+'.st')
    if naming.exists(path):
        path=path.with_name(path.stem+'_'+hashlib.sha256(source.encode()).hexdigest()[:8]+'.st')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(cmt('Original library/project: '+source)+text.rstrip()+'\n',encoding='utf-8',newline='\n')
    members=re.findall(r'(?m)^\(\* Source member: (.*) \*\)$',text)
    provenance.register(path,[source+'!'+m for m in members] or [source])
    OUTPUT_SEEN[vendor,fingerprint]=str(path.relative_to(OUT))
    report(vendor,source,'output',str(path.relative_to(OUT))+f'; units={len(units(text))}; bytes={path.stat().st_size}')
    print('OUTPUT',vendor,path.name,flush=True)


def codesys_frames(data,source):
    """Read source records, not arbitrary printable strings.

    CoDeSys+ source records have the verified 16-byte field marker, a bounded
    uint32 declaration length, an explicit implementation kind (0x12 = ST),
    then a bounded uint32 implementation length. TYPE/GVL records are already
    complete declarations. Unsupported implementation kinds are rejected.
    """
    if not data.startswith(b'CoDeSys+'):
        return None
    parts=[];records=[]
    marker=b'\x02\x00'*8
    for m in re.finditer(re.escape(marker)+rb'(?!\x02\x00)',data):
        i=m.end()
        if i+4>len(data):continue
        size=struct.unpack_from('<I',data,i)[0];start=i+4;stop=start+size
        if not 0<size<=20_000_000 or stop>len(data):continue
        raw=data[start:stop]
        if not re.match(rb'\s*(?:FUNCTION_BLOCK|FUNCTION|PROGRAM|TYPE|VAR_GLOBAL)\b',raw):continue
        if b'\0' in raw:continue
        try:decl=decode(raw,source)
        except (ValueError,UnicodeError):continue
        kind=re.match(r'\s*(\w+)',decl).group(1).upper()
        if kind in ('TYPE','VAR_GLOBAL'):
            if complete(decl):
                parts.append(decl.rstrip()+'\n');records.append((start,stop,kind,'declaration'))
            continue
        if stop+5>len(data):continue
        codekind=data[stop]
        if codekind!=0x12:
            FRAME_STATS['non_st_pou']+=1
            report(None,source+f'@{start}','omitted',f'Native POU implementation kind 0x{codekind:02x} is not ST')
            continue
        bodysize=struct.unpack_from('<I',data,stop+1)[0];bodystart=stop+5;bodyend=bodystart+bodysize
        if bodyend>len(data) or bodysize>20_000_000:
            report(None,source+f'@{start}','omitted','Out-of-bounds explicit source-body length')
            continue
        bodyraw=data[bodystart:bodyend]
        if b'\0' in bodyraw:
            report(None,source+f'@{start}','omitted','Binary/NUL within supposed ST source body')
            continue
        try:body=decode(bodyraw,source)
        except (ValueError,UnicodeError):continue
        if not strip_comments(body).strip():
            FRAME_STATS['empty_st_pou']+=1
            continue
        text=decl.rstrip()+'\n\n'+body.rstrip()+'\nEND_'+kind+'\n'
        if not complete(text):
            report(None,source+f'@{start}','omitted','Native declaration/body record did not form complete POU')
            continue
        parts.append(text);records.append((start,bodyend,kind,'ST'))
    FRAME_STATS['complete_source_records']+=len(records)
    return '\n'.join(parts),records


def pc_description(text):
    patt=re.compile(r'(\(\*@KEY@:\s*DESCRIPTION\s*\*\))(.*?)(\(\*@KEY@:\s*END_DESCRIPTION\s*\*\))',re.I|re.S)
    return patt.sub(lambda m:m.group(1)+'\n'+cmt(m.group(2).strip())+m.group(3),text)


def assemble_group(vendor,name,items,source):
    if source in SKIP_SOURCES:
        report(vendor,source,'deduplicated','Previously completed whole library retained while resuming')
        return
    selected=[];seen={};unique=set();conflicts=[]
    for filename,text in sorted(items,key=lambda p:p[0].casefold()):
        if not complete(text):
            report(vendor,source+'!'+filename,'omitted','Comment-only, compilation list, fragment, or incomplete native source')
            continue
        parsed=units(text)
        codes=[(kind,nm,canonical(part)) for kind,nm,part in parsed]
        bad=any(nm and (kind,nm) in seen and seen[kind,nm]!=code for kind,nm,code in codes)
        if bad:
            conflicts.append(filename)
            report(vendor,source+'!'+filename,'omitted','Conflicting same-name definitions within source project; original retained')
            continue
        digest=hashlib.sha256('\n'.join(k+'|'+n+'|'+c for k,n,c in codes).encode()).hexdigest()
        if digest in unique:
            report(vendor,source+'!'+filename,'deduplicated','Identical complete source unit collection in same original project')
            continue
        # A file can re-export identical types plus a new implementation. Retain
        # only the novel full units in that case, never a duplicate definition.
        duplicate_any=any(nm and (kind,nm) in seen for kind,nm,code in codes)
        if duplicate_any:
            novel=[]
            for (kind,nm,part),(_,_,code) in zip(parsed,codes):
                if not nm or (kind,nm) not in seen:novel.append(part)
            text='\n\n'.join(novel)
        if not text.strip():continue
        unique.add(digest)
        for kind,nm,code in codes:
            if nm:seen[kind,nm]=code
        selected.append(cmt('Source member: '+filename)+text.rstrip()+'\n')
    if selected:write(vendor,name,'\n'.join(selected),source)


def route(chain,default=None):
    low=chain.lower()
    if 'beckhoff' in low:return VENDORS['beckhoff']
    if 'pcworx' in low or 'pc_worx' in low:return VENDORS['pcworx']
    if 'step7' in low or re.search(r'(?:^|[/ _])s7(?:[/ _ .]|$)',low):return VENDORS['s7']
    if 'codesys' in low:return VENDORS['codesys']
    return default


def scan_archive(source,data,default=None):
    digest=hashlib.sha256(data).hexdigest()
    if digest in ARCHIVES_SEEN:
        report(default,source,'deduplicated','Identical archive already inspected')
        return
    ARCHIVES_SEEN.add(digest)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files={n.replace('\\','/'):z.read(n) for n in z.namelist() if not n.endswith('/')}
    except zipfile.BadZipFile:
        report(default,source,'omitted','Unreadable original archive')
        return
    vendor=route(source,default)
    is_pc=vendor==VENDORS['pcworx']
    label=PurePosixPath(source.rsplit('!',1)[-1].replace('\\','/')).stem
    if label=='@zip' and '!' in source:
        label=PurePosixPath(source.rsplit('!',2)[-2]).stem
    groups=defaultdict(list)
    pc_native=[]
    has_native_pc_package=is_pc and any(PurePosixPath(n).suffix.lower()=='.zwt' for n in files)
    # Named PC WORX source records require the complete sibling map.
    if is_pc:
        for n,b in files.items():
            if PurePosixPath(n).suffix.lower() not in ('.st1','.tyb'):continue
            try:result=pcworx.extract(n,b,files)
            except Exception as e:
                report(vendor,source+'!'+n,'omitted','CFB source read failed: '+str(e));continue
            if result:
                for unit in result['units']:
                    if unit['metadata'].get('matching_declaration_and_pou_metadata') or unit['status']=='assembled_vendor_source' or (n.lower().endswith('.tyb') and complete(unit['st'])):
                        pc_native.append((n+'#'+unit['name'],nl(unit['st'])))
                    elif complete(unit['st']) and re.match(r'(?is)^\s*VAR_GLOBAL\b',strip_comments(unit['st'])):
                        pc_native.append((n+'#global',nl(unit['st'])))
                    elif strip_comments(unit['st']).strip():
                        report(vendor,source+'!'+n+'#'+unit['name'],'omitted','Unbound source fragment; signature/scope not explicitly available')
    if pc_native:
        assemble_group(vendor,label,pc_native,source)
    for n,b in files.items():
        ext=PurePosixPath(n).suffix.lower();full=source+'!'+n
        if ext in ('.zip','.zwt','.zwp') or (n=='@zip.zwp'):
            if zipfile.is_zipfile(io.BytesIO(b)):
                scan_archive(full,b,route(full,vendor))
            else:report(vendor,full,'omitted','Original project package is not an accessible ZIP container')
            continue
        if ext in ('.lib','.pro') and b.startswith(b'CoDeSys+'):
            if full in SKIP_SOURCES:continue
            result=codesys_frames(b,full)
            if result and result[0]:
                dest=VENDORS['beckhoff'] if 'beckhoff' in full.lower() else VENDORS['codesys']
                write(dest,PurePosixPath(n).stem,result[0],full)
            else:report(vendor,full,'omitted','No complete explicitly typed ST source records')
            continue
        if ext not in ('.scl','.udt','.db','.txt','.exp','.st'):continue
        try:text=decode(b,full)
        except (ValueError,UnicodeError) as e:
            report(vendor,full,'omitted','Source is binary or cannot be decoded losslessly: '+str(e));continue
        if ext in ('.scl','.udt','.db'):
            dest=VENDORS['tia'] if '/tiaportal/' in '/'+n.lower() or vendor==VENDORS['tia'] else VENDORS['s7']
            if '/s7asrcom/' in '/'+n.lower():
                prefix=re.split('(?i)/s7asrcom/',n)[0]
                key=(dest,label+'_'+PurePosixPath(prefix).name)
            elif 'Siemens_SCL_Source_Files' in source:
                # Four Timer helper names conflict with the independent
                # DoorLock example. Preserve that original project boundary;
                # all remaining examples share one whole source collection.
                key=(dest,'Siemens_SCL_Source_Files_Timer' if n.startswith('Timer/') else 'Siemens_SCL_Source_Files')
            elif 'LIB_PLC' in source and n.startswith('Porting/'):
                key=(dest,label+'_Porting')
            else:key=(dest,label)
            groups[key].append((n,text))
        elif ext=='.st' and is_pc:
            if has_native_pc_package:
                # The original ZWT supplies declarations, body, explicit POU
                # signature, types and global scope in one project. Avoid
                # exporting its parallel module_export copy a second time.
                continue
            text=pc_description(text)
            parent=str(PurePosixPath(n).parent)
            key=(vendor,label+'_'+parent.replace('/','_'))
            groups[key].append((n,text))
        elif ext in ('.txt','.exp'):
            if full in SKIP_SOURCES:continue
            if not complete(text):continue
            dest=route(full,VENDORS['codesys'])
            if ext=='.exp' and '/module_export/' in '/'+n.lower():
                key=(dest,label+'_micro')
                groups[key].append((n,text))
            else:write(dest,PurePosixPath(n).stem,text,full)
    for (dest,name),items in groups.items():assemble_group(dest,name,items,source)


def validate_codesys_records():
    selected=BASE/VENDORS['codesys']/'原始库文件/源码/selected'
    binary=selected/'oscat_basic_333.lib'
    export=selected/'OSCAT_BASIC_333.TXT'
    result=codesys_frames(binary.read_bytes(),str(binary))
    rawtext=decode(export.read_bytes(),str(export))
    sourcepous={(k,n):canonical(t) for k,n,t in units(rawtext) if k in ('FUNCTION','FUNCTION_BLOCK','PROGRAM')}
    parsedpous={(k,n):canonical(t) for k,n,t in units(result[0]) if k in ('FUNCTION','FUNCTION_BLOCK','PROGRAM')}
    common=sourcepous.keys() & parsedpous.keys()
    differences=[key for key in common if sourcepous[key]!=parsedpous[key]]
    missing=sourcepous.keys()-parsedpous.keys()
    if differences or missing:
        raise ValueError(f'CoDeSys source-record validation failed: differences={differences[:8]}, missing={list(missing)[:8]}')
    assert len(common)>=500,(len(common),len(sourcepous),len(parsedpous))
    report(VENDORS['codesys'],str(binary),'validated',f'{len(common)} complete POU declarations and implementations exactly match independent same-version textual export, preserving literal values')
    return len(common)


def main():
    checked=validate_codesys_records()
    # Resume already completed source files after the Windows long-path failure;
    # PC WORX is regenerated with one output per original native project.
    for key in ('codesys','s7'):
        vendor=VENDORS[key]
        for p in (OUT/vendor/'.st文件').glob('native_*.st'):
            text=p.read_text(encoding='utf-8')
            first=text.splitlines()[0]
            source=first.removeprefix('(* Original library/project: ').removesuffix(' *)')
            SKIP_SOURCES.add(source)
            OUTPUT_SEEN[vendor,lib_fingerprint(text)]=str(p.relative_to(OUT))
            report(vendor,source,'output_retained',str(p.relative_to(OUT)))
    pc_folder=(OUT/VENDORS['pcworx']/'.st文件').resolve()
    assert pc_folder.is_relative_to(OUT.resolve())
    for p in pc_folder.glob('native_*.st'):p.unlink()
    # Deliver the explicitly selected current TwinCAT/CODESYS source records
    # before traversing the historical archive.
    for key in ('codesys','beckhoff'):
        vendor=VENDORS[key]
        root=BASE/vendor/'原始库文件'
        paths=list((root/'源码/selected').glob('*.lib')) if key=='codesys' else list((root/'源码').glob('*.lib'))
        for p in paths:
            if str(p) in SKIP_SOURCES:continue
            result=codesys_frames(p.read_bytes(),str(p))
            if result and result[0]:
                dest=VENDORS['beckhoff'] if 'beckhoff' in p.name.lower() else vendor
                write(dest,p.stem,result[0],str(p))
    for p in sorted((BASE/VENDORS['tia']/'原始库文件').rglob('*.zip')):
        scan_archive(str(p),p.read_bytes(),VENDORS['tia'])
    big=BASE/VENDORS['codesys']/'原始库文件/源码/eclipse-oscat_oscat-libs-archive_e2f15cb7778f.zip'
    scan_archive(str(big),big.read_bytes())
    for ecosystem in ('tia','s7','pcworx'):
        vendor=VENDORS[ecosystem]
        for p in sorted((BASE/vendor/'原始库文件').rglob('*.zip')):
            if 'eclipse-oscat' in p.name:continue
            scan_archive(str(p),p.read_bytes(),vendor)
    # Selected/direct binaries are deduplicated by the complete-library source
    # fingerprint, including the explicitly Beckhoff-targeted source.
    for ecosystem in ('codesys','beckhoff'):
        vendor=VENDORS[ecosystem]
        root=BASE/vendor/'原始库文件'
        paths=list((root/'源码/selected').glob('*')) if ecosystem=='codesys' else list((root/'源码').glob('*'))
        for p in paths:
            if not p.is_file():continue
            if p.suffix.lower()=='.lib':
                result=codesys_frames(p.read_bytes(),str(p))
                if result and result[0]:
                    dest=VENDORS['beckhoff'] if 'beckhoff' in p.name.lower() else vendor
                    write(dest,p.stem,result[0],str(p))
            elif p.suffix.lower()=='.txt':
                t=decode(p.read_bytes(),str(p))
                if complete(t):write(vendor,p.stem,t,str(p))
    with (WORK/'legacy_native_report.tsv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f,delimiter='\t');writer.writerow(['vendor','source','action','reason']);writer.writerows(RECORDS)
    counts=Counter(row[0] for row in RECORDS if row[2]=='output')
    print('Validated .lib source records against independent textual export:',checked)
    print('Whole library outputs:',dict(counts))
    print('Actions:',dict(Counter(r[2] for r in RECORDS)))
    print('Frames:',dict(FRAME_STATS))

