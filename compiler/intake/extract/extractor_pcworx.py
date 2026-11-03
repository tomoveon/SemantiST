"""PC WORX ST extraction from explicit named CFB streams, never string carving."""
from pathlib import PurePosixPath
import hashlib, re
from .extra_containers import cfb_streams
from .extractor_native import decode, mask


def h(data):return hashlib.sha256(data).hexdigest()


def extract(name, data, files):
    path=PurePosixPath(name.replace('\\','/'))
    if path.suffix.lower()=='.tyb':
        text,encoding,warning=decode(data)
        m=mask(text)
        if not re.search(r'(?im)^\s*TYPE\b',m):return {'units':[],'notes':['TYB does not contain explicit TYPE declaration.']}
        complete=len(re.findall(r'(?im)^\s*TYPE\b',m))==len(re.findall(r'(?im)^\s*END_TYPE\b',m))
        return {'units':[{'name':path.stem+'__types','st':text,
                         'status':'native_vendor_source' if complete and not warning else 'context_required',
                         'metadata':{'format':'PC WORX textual TYB','source':name,'source_sha256':h(data),'source_encoding':encoding,'encoding_warning':warning,'type_boundaries_complete':complete,'normalization_performed':False}}],
                'notes':[warning] if warning else []}
    if path.suffix.lower()!='.st1':return None
    streams=cfb_streams(data)
    if streams is None:return {'units':[],'notes':['ST1 is not a recognized CFB container.']}
    names=[m['name'].lower() for m,b in streams]
    if len(names)!=len(set(names)):
        return {'units':[],'notes':['CFB contains duplicate stream names in different storages; full storage-path binding required. Original retained.']}
    raw_by_name={meta['name'].lower():(meta,raw) for meta,raw in streams}
    bodies=[(meta,raw) for meta,raw in streams if meta['name'].lower().endswith('.stb')]
    all_vb=[(meta,raw) for meta,raw in streams if meta['name'].lower().endswith('.vb')]
    meta_path=str(path.parent/'tmp.sto')
    pt_text=None;pt_name=None;pt_sha=None;pt_info={};pt_error=None
    if meta_path in files:
        try:
            pt_streams=cfb_streams(files[meta_path]) or []
            pt_matches=[(m,b) for m,b in pt_streams if m['name'].lower().endswith('.pt')]
            if len(pt_matches)==1:
                m,b=pt_matches[0];pt_text,_,_=decode(b);pt_name=m['name'];pt_sha=h(b)
                for key in ['pouKind','pouName','pouReturnType']:
                    match=re.search(r'(?im)^'+key+r':[ \t]*([^\r\n]+)',pt_text)
                    if match:pt_info[key]=match.group(1).strip()
        except Exception as exc:pt_error=str(exc)
    docs=[]
    for sm,sb in streams:
        if sm['name'].lower().endswith('.txt'):
            try:
                dt,enc,w=decode(sb);docs.append({'name':sm['name'],'text':dt,'encoding':enc,'sha256':h(sb),'encoding_warning':w})
            except (ValueError,UnicodeError):pass
    out=[];notes=[];used_declarations=set()
    for sm,sb in bodies:
        body,enc,warning=decode(sb)
        pou_name=PurePosixPath(sm['name']).stem
        varname=pou_name.lower()+'v.vb'
        decl_entry=raw_by_name.get(varname)
        if decl_entry is None and len(all_vb)==1:decl_entry=all_vb[0]
        decl='';decl_meta=None;decl_warning=None
        if decl_entry:
            dm,db=decl_entry;decl,denc,decl_warning=decode(db)
            decl_meta={'name':dm['name'],'sha256':h(db),'encoding':denc,'bytes':len(db),'text':decl}
            used_declarations.add(dm['name'])
        kind=pt_info.get('pouKind','').upper()
        matched=pt_info.get('pouName','').casefold()==pou_name.casefold() and kind in {'FUNCTION','FUNCTION_BLOCK','PROGRAM'}
        if kind=='FUNCTION' and not pt_info.get('pouReturnType'):matched=False
        if matched and decl_entry:
            head=kind+' '+pt_info['pouName']
            if kind=='FUNCTION':head+=' : '+pt_info['pouReturnType']
            text=head+'\n'+decl.rstrip()+'\n\n'+body.rstrip()+'\nEND_'+kind+'\n'
            status='assembled_vendor_source'
        else:
            text=(decl.rstrip()+'\n\n' if decl else '')+body
            status='context_required'
        warnings=[w for w in [warning,decl_warning] if w]
        if warnings:status='context_required'
        meta={'format':'PC WORX CFB named STB/VB streams','source':name,'source_sha256':h(data),
              'body_stream':{'name':sm['name'],'sha256':h(sb),'bytes':len(sb),'encoding':enc,'text':body},
              'declaration_stream':decl_meta,'pou_metadata_file':meta_path if pt_text is not None else None,
              'pou_metadata_stream':pt_name,'pou_metadata_text':pt_text,'pou_metadata_stream_sha256':pt_sha,
              'pou_info':pt_info,'matching_declaration_and_pou_metadata':matched and decl_entry is not None,
              'pou_metadata_parse_error':pt_error,'documentation_streams':docs,
              'all_named_streams':[{'name':m['name'],'bytes':len(b),'sha256':h(b)} for m,b in streams],
              'encoding_warnings':warnings,'normalization_performed':False,
              'assembly_rule':'Use explicit pouKind/pouName/pouReturnType from sibling tmp.sto .pt; preserve original VB declarations and STB implementation.' if matched and decl_entry else 'Retain text with original container/scope; no guessed POU wrapper.'}
        out.append({'name':pou_name,'st':text,'status':status,'metadata':meta})
        notes.extend(warnings)
    for dm,db in all_vb:
        if dm['name'] in used_declarations:continue
        decl,denc,warning=decode(db)
        if not re.search(r'(?im)^\s*(?:VAR(?:_[A-Z_]+)?|TYPE)\b',mask(decl)):continue
        out.append({'name':str(path.parent).replace('/','__')+'__'+PurePosixPath(dm['name']).stem,
                    'st':decl,'status':'context_required',
                    'metadata':{'format':'PC WORX CFB named variable declaration stream','source':name,'source_sha256':h(data),
                                'scope_directory':str(path.parent),'declaration_stream':{'name':dm['name'],'sha256':h(db),'bytes':len(db),'encoding':denc,'text':decl},
                                'encoding_warning':warning,'normalization_performed':False,
                                'context_role':'Resource/global declarations or interface to non-ST source; original CFB and project scope retained.'}})
    if not out:notes.append('No explicit STB implementation or nonempty VB declaration stream in ST1; project/graphical/binary metadata retained in original.')
    return {'units':out,'notes':notes}
