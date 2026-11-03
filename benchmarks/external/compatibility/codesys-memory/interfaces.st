(* Deterministic CODESYS memory compatibility backed by runtime.c. *)

@EXTERNAL FUNCTION __SEMANTIST_MEMSET : REF_TO BYTE
VAR_INPUT
    DEST : REF_TO BYTE;
    VALUE : UDINT;
    COUNT : UDINT;
END_VAR
END_FUNCTION

@EXTERNAL FUNCTION __SEMANTIST_MEMCPY : REF_TO BYTE
VAR_INPUT
    DEST : REF_TO BYTE;
    SOURCE : REF_TO BYTE;
    COUNT : UDINT;
END_VAR
END_FUNCTION

@EXTERNAL FUNCTION __SEMANTIST_MEMMOVE : REF_TO BYTE
VAR_INPUT
    DEST : REF_TO BYTE;
    SOURCE : REF_TO BYTE;
    COUNT : UDINT;
END_VAR
END_FUNCTION

FUNCTION SysMemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
SysMemSet := __SEMANTIST_MEMSET(pDest, udiValue, udiCount);
END_FUNCTION

FUNCTION SysMemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemCpy := __SEMANTIST_MEMCPY(pDest, pSrc, udiCount);
END_FUNCTION

FUNCTION SysMemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemMove := __SEMANTIST_MEMMOVE(pDest, pSrc, udiCount);
END_FUNCTION


@EXTERNAL FUNCTION __SEMANTIST_BITCPY : BOOL
VAR_INPUT
    DEST : REF_TO BYTE;
    DEST_START_BIT : WORD;
    SOURCE : REF_TO BYTE;
    SOURCE_START_BIT : WORD;
    BIT_COUNT : WORD;
END_VAR
END_FUNCTION

FUNCTION MemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
MemSet := SysMemSet(pDest, udiValue, udiCount);
END_FUNCTION

FUNCTION MemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
MemCpy := SysMemCpy(pDest, pSrc, udiCount);
END_FUNCTION

FUNCTION MemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
MemMove := SysMemMove(pDest, pSrc, udiCount);
END_FUNCTION

FUNCTION __memcpy : REF_TO BYTE
VAR_INPUT
    dest : REF_TO BYTE;
    src : REF_TO BYTE;
    size : DINT;
END_VAR
__memcpy := __SEMANTIST_MEMCPY(dest, src, DINT_TO_UDINT(size));
END_FUNCTION

FUNCTION BitCpy : BOOL
VAR_INPUT
    pDest : REF_TO BYTE;
    wDstStartBit : WORD;
    pSource : REF_TO BYTE;
    wSrcStartBit : WORD;
    wSize : WORD;
END_VAR
BitCpy := __SEMANTIST_BITCPY(pDest, wDstStartBit, pSource, wSrcStartBit, wSize);
END_FUNCTION
