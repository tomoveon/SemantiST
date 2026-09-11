FUNCTION SysMemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemMove := __SEMANTIST_MEMMOVE(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION STFZ_ICSF_ICSFUZZ_BF_MMOVE_1 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	locbuffer2: WSTRING[256];
	mov_val: WORD;
	address: DWORD;
END_VAR

locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbb";

(* address := REF(locbuffer); *)

mov_val := input1/128 + 395;

SysMemMove(REF(locbuffer2), REF(locbuffer), mov_val);

STFZ_ICSF_ICSFUZZ_BF_MMOVE_1 := mov_val;
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_BF_MMOVE_1(input1 := input1);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
