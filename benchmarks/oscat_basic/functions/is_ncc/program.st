PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
    cmp : STRING[STRING_LENGTH];
END_VAR
VAR
    semantist_result : BOOL;
END_VAR
semantist_result := IS_NCC(str := str, cmp := cmp);
END_PROGRAM
