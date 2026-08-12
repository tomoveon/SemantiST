FUNCTION STFZ_ICSF_ICSFUZZ_BF_MSET_5 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	mov_val: WORD;
	cond: WORD;
	address: DWORD;
END_VAR


locbuffer := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

mov_val := input1/64 + 115;

//address := REF(locbuffer);

cond := input1/128;

IF cond > 100 THEN
	SysMemSet(REF(locbuffer), 6666, mov_val);
END_IF

STFZ_ICSF_ICSFUZZ_BF_MSET_5 := mov_val;

END_FUNCTION
