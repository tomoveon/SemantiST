FUNCTION STFZ_ICSF_ICSFUZZ_BF_MCPY_1 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	locbuffer: WSTRING[128];
	locbuffer2: WSTRING[256];
	mov_val: WORD;
	address: DWORD;
END_VAR

locbuffer := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
locbuffer2 := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

//address := REF(locbuffer);

mov_val := input1/128 + 124;

SysMemCpy(REF(locbuffer2), REF(locbuffer), mov_val);


STFZ_ICSF_ICSFUZZ_BF_MCPY_1 := mov_val;
END_FUNCTION
