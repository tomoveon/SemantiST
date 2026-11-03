FUNCTION STFZ_ICSF_ICSFUZZ_OOB_2_5 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	array1: ARRAY [0 .. 100] OF WORD;
	index: WORD;
	load_val: WORD;
	iter: DINT;
	cond: WORD;
END_VAR

load_val := 16#DEAD;

index := input1/256 + 32;

cond := input1/512;

FOR iter := 0 TO 100 BY 1 DO
	IF iter > cond THEN
		load_val:= array1[index];
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_OOB_2_5 := index;
END_FUNCTION
