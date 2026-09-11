PROGRAM PLC_PRG
VAR_INPUT
    WDAY : INT;
    LANG : INT;
    LX : INT;
END_VAR
VAR
    semantist_result : STRING[10];
END_VAR
semantist_result := WEEKDAY_TO_STRING(WDAY := WDAY, LANG := LANG, LX := LX);
END_PROGRAM
