PROGRAM PLC_PRG
VAR_INPUT
    in_1 : REAL;
    in_2 : REAL;
    in_3 : REAL;
    default : REAL;
    in_min : REAL;
    in_max : REAL;
    mode : BYTE;
END_VAR
VAR
    semantist_result : REAL;
END_VAR
semantist_result := MULTI_IN(in_1 := in_1, in_2 := in_2, in_3 := in_3, default := default, in_min := in_min, in_max := in_max, mode := mode);
END_PROGRAM
