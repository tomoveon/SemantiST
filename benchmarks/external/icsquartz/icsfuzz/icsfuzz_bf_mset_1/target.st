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

//address := REF(locbuffer);

mov_val := input1/128 + 214;

SysMemSet(REF(locbuffer), 6666, mov_val);

STFZ_ICSF_ICSFUZZ_BF_MSET_1 := mov_val;
END_FUNCTION
