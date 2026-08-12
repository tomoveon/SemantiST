FUNCTION STFZ_ICSF_ICSFUZZ_BF_MCPY_8 : WORD

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
END_VAR

locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

//address := REF(locbuffer);

mov_val_1 := input1/128 + 32;
mov_val_2 := input1/64 - 17;

cond1 := input1/128;
cond2 := input1/64 - 78;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 982 THEN
			SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_1);
		ELSE
			CONTINUE;
		END_IF
	ELSE
		IF cond2 < 472 THEN
			SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_2);
		ELSE
			CONTINUE;
		END_IF
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_BF_MCPY_8 := cond2;
END_FUNCTION
