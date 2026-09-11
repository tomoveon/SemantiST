FUNCTION STFZ_ICSF_ICSFUZZ_OOB_1_ARR_13 : WORD

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

STFZ_ICSF_ICSFUZZ_OOB_1_ARR_13 := cond1;

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_OOB_1_ARR_13(input1 := input1);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
