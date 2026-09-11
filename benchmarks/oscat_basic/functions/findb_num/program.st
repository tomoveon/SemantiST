PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FINDB_NUM(str := str);
END_PROGRAM
