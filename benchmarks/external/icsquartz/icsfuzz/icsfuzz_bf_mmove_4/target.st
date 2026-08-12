FUNCTION STFZ_ICSF_ICSFUZZ_BF_MMOVE_4 : WORD

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
END_VAR

locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

//address := REF(locbuffer);

mov_val_1 := input1/128 + 401;
mov_val_2 := input1/64 + 311;

cond := input1/256;

IF cond < 100 THEN
	SysMemMove(REF(locbuffer2), REF(locbuffer), mov_val_1);
ELSE
	SysMemMove(REF(locbuffer2), REF(locbuffer), mov_val_2);
END_IF

STFZ_ICSF_ICSFUZZ_BF_MMOVE_4 := cond;
END_FUNCTION
