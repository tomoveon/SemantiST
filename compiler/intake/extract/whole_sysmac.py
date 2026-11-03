"""Transcribe source-bearing Sysmac libraries through explicit OEM GUID links.

The OEM entity tree supplies scope, kind, declaration/body GUIDs. Formal
variables and return types come from the main SLR. Only matching unencrypted
SMC2 bodies with identical variable declarations are used. Nothing is decrypted.
"""
import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from . import whole_native_vendors as common
from . import provenance

BASE, OUT = common.BASE, common.OUT
VENDOR = "18_Omron_Sysmac"
LOG = []


def children(entity):
    return entity.findall("./ChildEntities/Entity")


def namespace(value):
    # OEM string properties escape the backslash separator, unlike datatype
    # XML attributes. Decode only the documented-in-container string escapes.
    return value.replace("\\r\\n", "\n").replace("\\\\", "\\").strip()


class Container:
    def __init__(self, data, origin):
        self.origin = origin
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.names = set(self.z.namelist())
        oems = [n for n in self.names if n.endswith(".oem")]
        assert len(oems)==1,(origin,oems)
        self.oem_name=oems[0]
        self.folder=PurePosixPath(oems[0]).parent
        self.oem=ET.fromstring(self.z.read(oems[0]))
        self.entities={e.get("id"):e for e in self.oem.iter("Entity")}

    def raw(self, entity):
        return self.z.read(str(self.folder/(entity.get("id")+".xml")))

    def xml(self, entity):
        return ET.fromstring(self.raw(entity))

    def body(self, entity):
        body_nodes=[c for c in children(entity) if c.get("type")=="PouBody"]
        assert len(body_nodes)==1,(self.origin,entity.attrib)
        body_node=body_nodes[0]
        text="".join(self.xml(body_node).itertext())
        if "<EncryptedFile>" in text:return None,body_node.get("id")
        return common.decode(text.encode("utf-8")),body_node.get("id")

    def variables(self, entity):
        var_nodes=[c for c in children(entity) if c.get("type")=="Variables"]
        assert len(var_nodes)==1,(self.origin,entity.attrib)
        vnode=var_nodes[0]
        raw=self.raw(vnode)
        if raw.lstrip().startswith(b"[SLWD"):
            # The SMC2 compact label table is compared with the formal SLR XML.
            text=common.decode(raw)
            records=[]
            for line in text.splitlines():
                if not line.startswith("++"):continue
                record={}
                for cell in line[2:].split("\t"):
                    key,sep,value=cell.partition("=")
                    if sep:record[key]=value
                records.append({"Name":record["N"],"DataTypeName":record["D"],
                                "Group":record["G"],"InitialValue":record.get("IV", ""),
                                "IsConstant":"true" if record.get("Const")=="1" else "false",
                                "Retained":"true" if record.get("Ret")=="1" else "false",
                                "IsReadOnly":"true" if record.get("RO")=="1" else "false"})
            return records,vnode.get("id")
        root=ET.fromstring(raw)
        return [dict(v.attrib) for v in root.iter("Variable")],vnode.get("id")

    def counts(self):
        result=Counter()
        for e in self.entities.values():
            if e.get("type")=="PouBody" and e.get("subtype")=="StructuredText":
                txt="".join(self.xml(e).itertext())
                result["encrypted" if "<EncryptedFile>" in txt else "plaintext"]+=1
        return result


def variable_signature(records):
    fields=["Name","DataTypeName","Group","InitialValue","IsConstant","Retained","IsReadOnly"]
    return sorted(tuple(str(v.get(k,"false" if k in fields[4:] else "")).casefold()
                        if k in fields[3:] else v.get(k,"") for k in fields) for v in records)


def declarations(records):
    groups=[]
    returns=[]
    for v in records:
        if v["Group"]=="RETURN":returns.append(v);continue
        if v.get("Edge","NoEdge")!="NoEdge" or v.get("Length") or v.get("InitialValueArrayLength"):
            raise ValueError("Unsupported formal Sysmac variable attribute: "+repr(v))
        group=v["Group"]
        assert group in ("VAR","VAR_INPUT","VAR_OUTPUT","VAR_IN_OUT","VAR_EXTERNAL","VAR_GLOBAL"),group
        if v.get("Retained")=="true":group+=" RETAIN"
        if v.get("IsConstant")=="true":group+=" CONSTANT"
        location=" AT "+v["Address"] if v.get("Address") else ""
        initial=" := "+v["InitialValue"] if v.get("InitialValue") else ""
        line=f"    {v['Name']}{location} : {v['DataTypeName']}{initial};"
        if v.get("Comment"):line+=" "+common.comment(v["Comment"])
        # Preserve the actual parameter order across interleaved INPUT/IN_OUT
        # groups. Repeated declaration blocks preserve the OEM signature;
        # aggregating every INPUT first would change positional arguments.
        if not groups or groups[-1][0]!=group:groups.append((group,[]))
        groups[-1][1].append(line)
    text="\n\n".join(g+"\n"+"\n".join(lines)+"\nEND_VAR" for g,lines in groups)
    return text,returns


def structured_type(element):
    name,base=element.get("Name"),element.get("BaseType")
    assert name and base,(name,base)
    if base=="STRUCT":
        fields=[]
        for field in element.findall("DataType"):
            typ=field.get("BaseType")
            assert typ not in ("STRUCT","ENUM") and not field.get("ArrayType"),(name,field.attrib)
            initial=" := "+field.get("InitialValue") if field.get("InitialValue") else ""
            line=f"    {field.get('Name')} : {typ}{initial};"
            if field.get("Comment"):line+=" "+common.comment(field.get("Comment"))
            fields.append(line)
        return f"TYPE\n{name} : STRUCT\n"+"\n".join(fields)+"\nEND_STRUCT;\nEND_TYPE"
    raise ValueError("Unsupported Sysmac type: "+repr(element.attrib))


def scope(text, ns, using=""):
    usings=[v.strip() for v in namespace(using).splitlines() if v.strip()]
    imports="\n".join("USING "+v+";" for v in usings)
    body=(imports+"\n" if imports else "")+text
    return "NAMESPACE "+namespace(ns)+"\n"+body+"\nEND_NAMESPACE" if ns else body


def main():
    produced=0
    primary_counts=[]
    for archive in common.archives(VENDOR):
        if archive.name.startswith(("jiecunit_","jieclib_")):continue
        library_name=archive.name.split("_")[0]
        with zipfile.ZipFile(archive) as repository:
            nested=[n for n in repository.namelist() if n.lower().endswith((".slr",".smc2"))]
            primary=next(n for n in nested if n.endswith("/"+library_name+".slr") and len(PurePosixPath(n).parts)==2)
            lib=Container(repository.read(primary),archive.name+"!"+primary)
            primary_counts.append((library_name,dict(lib.counts())))
            LOG.append(f"PRIMARY {lib.origin}: {dict(lib.counts())}")
            seen=set()
            for member in nested:
                data=repository.read(member)
                digest=hashlib.sha256(data).hexdigest()
                if digest in seen:
                    LOG.append(f"DUPLICATE CONTAINER {archive.name}!{member}")
                    continue
                seen.add(digest)
                if member!=primary:
                    c=Container(data,archive.name+"!"+member)
                    LOG.append(f"CONTAINER {c.origin}: {dict(c.counts())}")
            pous=[e for e in lib.entities.values() if e.get("type") in ("Function","FunctionBlock")]
            candidates=defaultdict(list)
            for member in nested:
                if not member.endswith(".smc2"):continue
                project=Container(repository.read(member),archive.name+"!"+member)
                for original in pous:
                    entity=project.entities.get(original.get("id"))
                    if entity is None:continue
                    if entity.get("type")!=original.get("type") or entity.get("name")!=original.get("name"):
                        LOG.append(f"REJECT GUID name/kind mismatch {project.origin}!{original.get('id')}")
                        continue
                    body,body_guid=project.body(entity)
                    if body is None:continue
                    vars0,var_guid0=lib.variables(original)
                    vars1,var_guid1=project.variables(entity)
                    if variable_signature(vars0)!=variable_signature(vars1) or var_guid0!=var_guid1:
                        LOG.append(f"REJECT declaration mismatch {project.origin}!{original.get('name')}")
                        continue
                    if namespace(original.get("namespace",""))!=namespace(entity.get("namespace","")):
                        LOG.append(f"REJECT namespace mismatch {project.origin}!{original.get('name')}")
                        continue
                    if namespace(original.get("usingNamespaces",""))!=namespace(entity.get("usingNamespaces","")):
                        LOG.append(f"REJECT namespace-import mismatch {project.origin}!{original.get('name')}")
                        continue
                    if original.get("dateLastModified")!=entity.get("dateLastModified"):
                        LOG.append(f"REJECT implementation modification-date mismatch {project.origin}!{original.get('name')}")
                        continue
                    candidates[original.get("id")].append((project,entity,body,body_guid))
            missing=[e for e in pous if not candidates[e.get("id")]]
            if missing:
                LOG.append(f"SKIP WHOLE LIBRARY {library_name}: {len(missing)}/{len(pous)} POU implementations remain encrypted or lack matching formal declarations")
                LOG.extend("  MISSING "+e.get("name")+" GUID="+e.get("id") for e in missing)
                continue
            sections=[]
            for e in lib.entities.values():
                if e.get("type")!="DataType":continue
                root=lib.xml(e)
                holder=root.find("DataType")
                if holder is None:continue
                for dtype in holder.findall("DataType"):
                    body=structured_type(dtype)
                    sections.append(scope(body,e.get("namespace","")))
                    LOG.append(f"TYPE {library_name}/{dtype.get('Name')}: formal declaration GUID={e.get('id')}")
            body_check=[]
            pou_origins=[]
            for e in pous:
                options=candidates[e.get("id")]
                unique={opt[2] for opt in options}
                assert len(unique)==1,(library_name,e.get("name"),"conflicting project bodies")
                project,project_entity,body,body_guid=options[0]
                records,var_guid=lib.variables(e)
                decls,returns=declarations(records)
                kind="FUNCTION" if e.get("type")=="Function" else "FUNCTION_BLOCK"
                suffix=""
                if kind=="FUNCTION":
                    assert len(returns)==1 and returns[0]["Name"]==e.get("name"),(e.attrib,returns)
                    suffix=" : "+returns[0]["DataTypeName"]
                else:assert not returns,(e.attrib,returns)
                text=f"{kind} {e.get('name')}{suffix}\n"+decls+"\n\n"+body.strip()+f"\nEND_{kind}"
                sections.append(scope(text,e.get("namespace",""),project_entity.get("usingNamespaces","")))
                body_check.append(body.strip())
                pou_origins.append({"name":e.get("name"),"kind":kind,"source":str(archive)+"!"+project.origin.split("!",1)[1],
                                    "element":f"POU GUID {e.get('id')}; declaration {primary}; body GUID {body_guid}"})
                LOG.append(f"MATCH {library_name}/{e.get('name')}: POU GUID={e.get('id')}; formal Variables GUID={var_guid}; plaintext body GUID={body_guid}; {project.origin}; {len(records)} variables including RETURN; signature/namespace/imports/modification date identical")
            header=common.comment(f"Sysmac whole-library source transcription: {library_name}. Namespace/USING boundaries are transcribed from container metadata; original backslash namespace separators require dialect normalization. External RingBufferLib and Omron system libraries remain dependencies.")
            output=header+"\n\n"+"\n\n".join(sections)+"\n"
            assert all(body in output for body in body_check)
            assert "EncryptedFile" not in output and "EncryptionLock" not in output
            target=OUT/VENDOR/".st文件"/("sysmac_"+library_name+".st")
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(output,encoding="utf-8",newline="\n")
            provenance.register(target,[str(archive)+"!"+primary]+sorted({o["source"] for o in pou_origins}),pous=pou_origins)
            LOG.append(f"OUTPUT {target.name}: all {len(pous)} POU implementations matched; {len(sections)-len(pous)} types; {len(output)} characters")
            produced+=1
    LOG.append("PRIMARY ENCRYPTION CENSUS "+repr(primary_counts))
    LOG.append(f"OUTPUT COUNT {produced}; no encryption bypass; no per-POU fragment files or auxiliary output files")
    (BASE/"sysmac_extraction.log").write_text("\n".join(LOG)+"\n",encoding="utf-8")
    print("Sysmac whole-library files:",produced)
    print(primary_counts)

