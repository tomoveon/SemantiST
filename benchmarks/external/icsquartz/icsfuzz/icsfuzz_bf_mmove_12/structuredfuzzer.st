FUNCTION SysMemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemMove := __SEMANTIST_MEMMOVE(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION STFZ_ICSF_ICSFUZZ_BF_MMOVE_12 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	locbuffer2: WSTRING[256];
	mov_val_1: WORD;
	mov_val_2: WORD;
	cond1: WORD;
	cond2: WORD;
	address: DWORD;
	iter: DINT;
	ret_val: WORD;
END_VAR
locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

(* address := REF(locbuffer); *)

mov_val_1 := input1/128 + 398;
mov_val_2 := input1/64 + 289;

cond1 := input1/64;
cond2 := input1/128 + 83;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 102 THEN
			SysMemMove(REF(locbuffer2), REF(locbuffer), mov_val_1);
		END_IF
	ELSE
		IF cond2 < 129 THEN
			ret_val := func_12(input1);
		END_IF
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_BF_MMOVE_12 := cond1;
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_BF_MMOVE_12(input1 := input1);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
