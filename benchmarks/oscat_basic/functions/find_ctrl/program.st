PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
    pos : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FIND_CTRL(str := str, pos := pos);
END_PROGRAM
