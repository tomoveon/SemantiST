FUNCTION STFZ_ICSF_ICSFUZZ_OOB_2_13 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	array1: ARRAY [0 .. 100] OF WORD;
	ret_val1: WORD;
	cond1: WORD;
	cond2: WORD;
	iter : DINT;
END_VAR


cond1 := input1/128;
cond2 := input1/256 + 89;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 100 THEN
			ret_val1 := func_13_1(input1);
		END_IF
	ELSE
		IF cond2 < 345 THEN
			ret_val1 := func_13_2(input1);
		END_IF
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_OOB_2_13 := cond1;
END_FUNCTION


FUNCTION func_13_1 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	array1: ARRAY [0 .. 100] OF WORD;
	index1: WORD;
	index2: WORD;
	load_val: WORD;
	cond1: WORD;
	cond2: WORD;
END_VAR

load_val := 16#DEAD;

index1 := input1/128 + 32;
index2 := input1/256 + 67;

cond1 := input1/64;
cond2 := input1/128 + 178;

IF cond1 < 1000 THEN
	IF cond2 > 1000 THEN
		load_val:= array1[index1];
	END_IF
ELSE
	IF cond2 < 167 THEN
		load_val:= array1[index2];
	END_IF
END_IF

func_13_1 := index1;

END_FUNCTION


FUNCTION func_13_2 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	array1: ARRAY [0 .. 100] OF WORD;
	index1: WORD;
	index2: WORD;
	load_val: WORD;
	cond1: WORD;
	cond2: WORD;
	iter : DINT;
END_VAR

load_val := 16#DEAD;

index1 := input1/256 + 32;
index2 := input1/128 + 67;

cond1 := input1/64;
cond2 := input1/128 + 89;

FOR iter:=0 TO 100	BY 1 DO
	IF cond1 < iter THEN
		IF cond2 > 100 THEN
			load_val:= array1[index1];
		END_IF
	ELSE
		IF cond2 < 149 THEN
			load_val:= array1[index2];
		END_IF
	END_IF
END_FOR

func_13_2 := index1;
END_FUNCTION
