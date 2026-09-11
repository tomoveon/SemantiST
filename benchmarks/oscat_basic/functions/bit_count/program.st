PROGRAM PLC_PRG
VAR_INPUT
    IN : DWORD;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := BIT_COUNT(IN := IN);
END_PROGRAM
