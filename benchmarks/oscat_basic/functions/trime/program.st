PROGRAM PLC_PRG
VAR_INPUT
    str : STRING[STRING_LENGTH];
END_VAR
VAR
    semantist_result : STRING[string_length];
END_VAR
semantist_result := TRIME(str := str);
END_PROGRAM
