PROGRAM PLC_PRG
VAR_INPUT
    input1 : WORD;
END_VAR
VAR
    semantist_result : WORD;
END_VAR
semantist_result := STFZ_ICSF_ICSFUZZ_BF_MCPY_8(input1 := input1);
END_PROGRAM
