PROGRAM PLC_PRG
VAR_INPUT
    M1 : REAL;
    M2 : REAL;
    I1 : BOOL;
    I2 : BOOL;
    D : REAL;
    RST : BOOL;
    MX : REAL;
END_VAR
VAR
    semantist_fb : METER;

END_VAR
semantist_fb(M1 := M1, M2 := M2, I1 := I1, I2 := I2, D := D, RST := RST, MX := MX);
END_PROGRAM
