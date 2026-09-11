FUNCTION STFZ_ICSF_ICSFUZZ_OOB_1_ARR_6 : WORD

VAR_INPUT
	input1: WORD;
END_VAR

VAR
	array1: ARRAY [0 .. 100] OF WORD;
	index1: WORD;
	index2: WORD;
	load_val: WORD;
	iter: DINT;
	cond: WORD;
END_VAR

load_val := 16#DEAD;

index1 := input1/256 + 32;
index2 := input1/512 + 67;

cond := input1/256;


(*  TODO - verify this *)
(*  arr_4 := index1; *)

FOR iter := 0 TO 100 BY 1 DO
	IF iter < index1 THEN
		(*  Manual throw *)
		(*  IF index1 > 100 THEN *)
		(*  	index1 := 100000; *)
		(*  END_IF *)
		array1[index1] := load_val;
	ELSE
		(*  Manual throw *)
		(*  IF index1 > 100 THEN *)
		(*  	index1 := 100000; *)
		(*  END_IF *)
		array1[index2] := load_val;
	END_IF
END_FOR

STFZ_ICSF_ICSFUZZ_OOB_1_ARR_6 := index1;
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_OOB_1_ARR_6(input1 := input1);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
