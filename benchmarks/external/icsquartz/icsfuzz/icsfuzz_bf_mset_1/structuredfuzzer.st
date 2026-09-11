FUNCTION SysMemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
SysMemSet := __SEMANTIST_MEMSET(pDest, udiValue, udiCount);
END_FUNCTION


FUNCTION STFZ_ICSF_ICSFUZZ_BF_MSET_1 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	mov_val: WORD;
	address: DWORD;
END_VAR


locbuffer := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

(* address := REF(locbuffer); *)

mov_val := input1/128 + 214;

SysMemSet(REF(locbuffer), 6666, mov_val);

STFZ_ICSF_ICSFUZZ_BF_MSET_1 := mov_val;
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_BF_MSET_1(input1 := input1);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
