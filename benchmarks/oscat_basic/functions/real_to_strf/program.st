PROGRAM PLC_PRG
VAR_INPUT
    IN : REAL;
    N : INT;
    D : STRING[1];
END_VAR
VAR
    semantist_result : STRING[20];
END_VAR
semantist_result := REAL_TO_STRF(IN := IN, N := N, D := D);
END_PROGRAM
