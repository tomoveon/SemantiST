FUNCTION STFZ_ICSF_ICSFUZZ_BF_MCPY_12 : WORD

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

//address := REF(locbuffer);

mov_val_1 := input1/128 + 43;
mov_val_2 := input1/64 - 21;

cond1 := input1/64;
cond2 := input1/128 + 83;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 102 THEN
			SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_1);
		END_IF
	ELSE
		IF cond2 < 129 THEN
			ret_val := func_12(input1);
		END_IF
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_BF_MCPY_12 := cond1;
END_FUNCTION

FUNCTION func_12 : WORD

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

mov_val_1 := input1/128 + 31;
mov_val_2 := input1/64 - 21;

cond1 := input1/64;
cond2 := input1/128 + 82;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 106 THEN
			SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_1);
		END_IF
	ELSE
		IF cond2 < 124 THEN
			SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val_2);
		END_IF
	END_IF
END_FOR

func_12 := cond1;
END_FUNCTION
