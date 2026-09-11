PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
    src : STRING[STRING_LENGTH];
    rep : STRING[STRING_LENGTH];
END_VAR
VAR
    semantist_result : STRING[STRING_LENGTH];
END_VAR
semantist_result := REPLACE_ALL(str := str, src := src, rep := rep);
END_PROGRAM
