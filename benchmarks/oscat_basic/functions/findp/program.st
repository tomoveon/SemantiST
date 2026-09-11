PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
    src : STRING[STRING_LENGTH];
    pos : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FINDP(str := str, src := src, pos := pos);
END_PROGRAM
