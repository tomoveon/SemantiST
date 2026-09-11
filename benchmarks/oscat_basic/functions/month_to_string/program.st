PROGRAM PLC_PRG
VAR_INPUT
    MTH : INT;
    LANG : INT;
    LX : INT;
END_VAR
VAR
    semantist_result : STRING[10];
END_VAR
semantist_result := MONTH_TO_STRING(MTH := MTH, LANG := LANG, LX := LX);
END_PROGRAM
