FUNCTION STFZ_ICSF_ICSFUZZ_OOB_1_ARR_1 : WORD
VAR_INPUT
	input1: WORD;
END_VAR
VAR
	array1: ARRAY [0 .. 100] OF WORD;
	index: WORD;
	load_val: WORD;
END_VAR
load_val := 16#DEAD;
index := input1/256 + 32;
array1[index] := load_val;
STFZ_ICSF_ICSFUZZ_OOB_1_ARR_1 := index;
END_FUNCTION
