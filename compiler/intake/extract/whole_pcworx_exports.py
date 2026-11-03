"""Complete old PC WORX export bundles with original project types/globals."""
import io,re,zipfile,collections
from pathlib import PurePosixPath
from . import whole_legacy_native as w


def readzip(b):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        return {n:z.read(n) for n in z.namelist() if not n.endswith('/')}


def native_projects(files,source):
    projects=[]
    if any(n.lower().endswith('.st1') for n in files):
        items=[];names=set();supp=[]
        for n,b in files.items():
            if not n.lower().endswith(('.st1','.tyb')):continue
            try:r=w.pcworx.extract(n,b,files)
            except Exception:continue
            for u in (r or {}).get('units',[]):
                t=w.nl(u['st']);names.add(u['name'].casefold())
                if n.lower().endswith('.tyb') or re.match(r'(?is)^\s*VAR_GLOBAL\b',w.strip_comments(t)):
                    if w.complete(t):supp.append((n,t))
                elif w.complete(t):items.append((n+'#'+u['name'],t))
        projects.append((source,names,items,supp))
    for n,b in files.items():
        if n.lower().endswith(('.zwt','.zwp')) and zipfile.is_zipfile(io.BytesIO(b)):
            projects.extend(native_projects(readzip(b),source+'!'+n))
    return projects


def rebuild(source,b):
    files=readzip(b);groups=collections.defaultdict(list)
    for n,raw in files.items():
        if n.lower().endswith('.st'):
            try:t=w.pc_description(w.decode(raw,source+'!'+n))
            except Exception:continue
            langs=re.findall(r'IEC_LANGUAGE:[ \t]*(\w+)',t)
            if langs and any(x.upper()!='ST' for x in langs):continue
            if w.complete(t):groups[str(PurePosixPath(n).parent)].append((n,t))
    if not groups:return
    projects=native_projects(files,source)
    folder=w.OUT/w.VENDORS['pcworx']/'.st文件';old=[]
    for p in folder.glob('native_*.st'):
        t=p.read_text(encoding='utf-8');first=t.splitlines()[0]
        oldsource=first.removeprefix('(* Original library/project: ').removesuffix(' *)')
        if oldsource.startswith(source+'!'):old.append((p,t))
    replaced=set()
    for group,items in groups.items():
        names={nm for _,t in items for k,nm,_ in w.units(t) if k in ('FUNCTION','FUNCTION_BLOCK','PROGRAM')}
        # Normalize original exported and CFB POU names without guessing kinds.
        names={n.strip('"') for n in names}
        ranked=sorted(projects,key=lambda p:len(names & p[1]),reverse=True)
        additions=[]
        if ranked and names & ranked[0][1]:
            _,_,complete,supp=ranked[0]
            additions.extend(supp)
            for n,t in complete:
                defined={nm for k,nm,_ in w.units(t) if k in ('FUNCTION','FUNCTION_BLOCK','PROGRAM')}
                if not (defined & names):additions.append((n,t))
        for p,t in old:
            defined={nm for k,nm,_ in w.units(t) if k in ('FUNCTION','FUNCTION_BLOCK','PROGRAM')}
            if defined & names:replaced.add(p)
        stem=PurePosixPath(source.rsplit('!',1)[-1].replace('\\','/')).stem
        label=stem if len(groups)==1 else stem+'_'+PurePosixPath(group).name
        before=len(w.RECORDS)
        w.assemble_group(w.VENDORS['pcworx'],label,additions+items,source+'!whole_export/'+group)
        print('PCWORX WHOLE',stem,group,'exported units',len(names),'supplements',len(additions),flush=True)
    for p in replaced:p.unlink()


def main():
    big=w.BASE/w.VENDORS['codesys']/'原始库文件/源码/eclipse-oscat_oscat-libs-archive_e2f15cb7778f.zip'
    seen=set()
    with zipfile.ZipFile(big) as z:
        for n in z.namelist():
            if not n.lower().endswith('.zip') or not ('pcworx' in n.lower() or 'pc_worx' in n.lower()):continue
            b=z.read(n);seen.add(w.hashlib.sha256(b).hexdigest());rebuild(str(big)+'!'+n,b)
    for p in (w.BASE/w.VENDORS['pcworx']/'原始库文件').rglob('*.zip'):
        if 'eclipse-oscat' in p.name:continue
        b=p.read_bytes();digest=w.hashlib.sha256(b).hexdigest()
        if digest not in seen:seen.add(digest);rebuild(str(p),b)
    with (w.WORK/'pcworx_whole_export_report.tsv').open('w',encoding='utf-8',newline='') as f:
        writer=w.csv.writer(f,delimiter='\t');writer.writerow(['vendor','source','action','reason']);writer.writerows(w.RECORDS)

