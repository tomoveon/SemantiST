PROGRAM PLC_PRG
VAR_INPUT
    A0 : BOOL;
    A1 : BOOL;
    X01 : REAL;
    X02 : REAL;
    X03 : REAL;
    X04 : REAL;
    X11 : REAL;
    X12 : REAL;
    X13 : REAL;
    X14 : REAL;
    X21 : REAL;
    X22 : REAL;
    X23 : REAL;
    X24 : REAL;
    X31 : REAL;
    X32 : REAL;
    X33 : REAL;
    X34 : REAL;
    TC : TIME;
END_VAR
VAR
    semantist_fb : PARSET;
    semantist_out_P1 : REAL;
    semantist_out_P2 : REAL;
    semantist_out_P3 : REAL;
    semantist_out_P4 : REAL;
END_VAR
semantist_fb(A0 := A0, A1 := A1, X01 := X01, X02 := X02, X03 := X03, X04 := X04, X11 := X11, X12 := X12, X13 := X13, X14 := X14, X21 := X21, X22 := X22, X23 := X23, X24 := X24, X31 := X31, X32 := X32, X33 := X33, X34 := X34, TC := TC);
semantist_out_P1 := semantist_fb.P1;
semantist_out_P2 := semantist_fb.P2;
semantist_out_P3 := semantist_fb.P3;
semantist_out_P4 := semantist_fb.P4;
END_PROGRAM
