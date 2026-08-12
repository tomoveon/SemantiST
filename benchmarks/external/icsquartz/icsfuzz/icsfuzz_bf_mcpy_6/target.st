FUNCTION STFZ_ICSF_ICSFUZZ_BF_MCPY_6 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	locbuffer2: WSTRING[256];
	mov_val_1: WORD;
	mov_val_2: WORD;
	cond: WORD;
	address: DWORD;
	iter: DINT;
END_VAR

locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

//address := REF(locbuffer);

mov_val_1 := input1/128 + 32;
mov_val_2 := input1/64 - 17;

cond := input1/512; 

FOR iter := 0 TO 100 BY 1 DO
	IF iter < mov_val_1 THEN
		SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_1);
	ELSE
		SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_2);
	END_IF
END_FOR



STFZ_ICSF_ICSFUZZ_BF_MCPY_6 := cond;
END_FUNCTION
