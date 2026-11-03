"""Read-only supplemental archive inspection; no vendor code is executed."""
import struct


def cfb_streams(data):
    """Read named streams from standard Compound File Binary containers.

    This decodes the container only, never a vendor-specific binary stream.
    """
    if data[:8] != bytes.fromhex('D0CF11E0A1B11AE1'):return None
    if len(data)<512:raise ValueError('Truncated CFB header')
    if data[28:30]!=b'\xfe\xff':raise ValueError('Unsupported CFB byte order')
    sector=1<<struct.unpack_from('<H',data,30)[0]
    mini=1<<struct.unpack_from('<H',data,32)[0]
    if sector not in {512,4096} or mini!=64:raise ValueError('Unexpected CFB sector size')
    # A final data sector can be physically short in historical exported files.
    # Never pad it: the declared stream-size check below must still pass.
    count=(len(data)+sector-1)//sector-1
    def sec(sid):
        if not 0<=sid<count:raise ValueError('CFB sector out of bounds')
        return data[(sid+1)*sector:(sid+2)*sector]
    fat_count=struct.unpack_from('<I',data,44)[0]
    fatids=list(struct.unpack_from('<109I',data,76))
    did,dnum=struct.unpack_from('<II',data,68)
    seen=set()
    for _ in range(dnum):
        if did in seen:raise ValueError('CFB DIFAT loop')
        seen.add(did);ds=struct.unpack('<'+'I'*(sector//4),sec(did));fatids.extend(ds[:-1]);did=ds[-1]
    fatids=[x for x in fatids if x<0xFFFFFFFA][:fat_count]
    if len(fatids)!=fat_count:raise ValueError('Incomplete CFB FAT')
    fat=[]
    for sid in fatids:fat.extend(struct.unpack('<'+'I'*(sector//4),sec(sid)))
    def chain(start, table, read, max_count):
        pieces=[];seen=set();sid=start
        while sid!=0xFFFFFFFE:
            if sid>=0xFFFFFFFA or sid in seen or sid>=len(table) or len(seen)>=max_count:raise ValueError('Invalid CFB chain')
            seen.add(sid);pieces.append(read(sid));sid=table[sid]
        return b''.join(pieces)
    firstdir=struct.unpack_from('<I',data,48)[0]
    directory=chain(firstdir,fat,sec,count)
    entries=[]
    for i in range(0,len(directory),128):
        e=directory[i:i+128]
        if len(e)!=128:break
        nlen=struct.unpack_from('<H',e,64)[0]
        typ=e[66]
        if not typ:continue
        if not 2<=nlen<=64:raise ValueError('Invalid CFB entry name')
        name=e[:nlen-2].decode('utf-16-le')
        start=struct.unpack_from('<I',e,116)[0]
        size=struct.unpack_from('<Q',e,120)[0]
        if struct.unpack_from('<H',data,26)[0]==3:size &= 0xFFFFFFFF
        entries.append({'name':name,'type':typ,'start':start,'size':size,'directory_index':i//128})
    root=next(x for x in entries if x['type']==5)
    rootdata=chain(root['start'],fat,sec,count)[:root['size']] if root['size'] else b''
    mfstart,mfcount=struct.unpack_from('<II',data,60)
    mfraw=chain(mfstart,fat,sec,count) if mfcount else b''
    minifat=list(struct.unpack('<'+'I'*(len(mfraw)//4),mfraw))
    cutoff=struct.unpack_from('<I',data,56)[0]
    out=[]
    for e in entries:
        if e['type']!=2:continue
        if not e['size']:raw=b''
        elif e['size']<cutoff:
            def readmini(sid):
                chunk=rootdata[sid*mini:(sid+1)*mini]
                if len(chunk)!=mini:raise ValueError('CFB mini sector out of bounds')
                return chunk
            raw=chain(e['start'],minifat,readmini,len(minifat))[:e['size']]
        else:raw=chain(e['start'],fat,sec,count)[:e['size']]
        if len(raw)!=e['size']:raise ValueError('Truncated CFB stream')
        out.append((e,raw))
    return out

