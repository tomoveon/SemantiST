PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
END_VAR
VAR
    semantist_result : STRING[STRING_LENGTH];
END_VAR
semantist_result := TRIM(str := str);
END_PROGRAM
