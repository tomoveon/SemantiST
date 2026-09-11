PROGRAM PLC_PRG
VAR_INPUT
    IN : STRING[12];
END_VAR
VAR
    semantist_result : BYTE;
END_VAR
semantist_result := FSTRING_TO_BYTE(IN := IN);
END_PROGRAM
