PROGRAM PLC_PRG
VAR_INPUT
    E : BOOL;
    X : REAL;
    K : REAL;
    Y : REAL;
END_VAR
VAR
    semantist_fb : INTEGRATE;

END_VAR
semantist_fb(E := E, X := X, K := K, Y := Y);
END_PROGRAM
