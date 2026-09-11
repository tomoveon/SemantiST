PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
END_VAR
VAR
    semantist_fb : TREND;
    semantist_out_Q : BOOL;
    semantist_out_TU : BOOL;
    semantist_out_TD : BOOL;
    semantist_out_D : REAL;
END_VAR
semantist_fb(X := X);
semantist_out_Q := semantist_fb.Q;
semantist_out_TU := semantist_fb.TU;
semantist_out_TD := semantist_fb.TD;
semantist_out_D := semantist_fb.D;
END_PROGRAM
