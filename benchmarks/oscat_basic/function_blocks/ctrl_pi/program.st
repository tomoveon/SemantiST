PROGRAM PLC_PRG
VAR_INPUT
    ACT : REAL;
    SET0 : REAL;
    SUP : REAL;
    OFS : REAL;
    M_I : REAL;
    MAN : BOOL;
    RST : BOOL;
    KP : REAL;
    KI : REAL;
    LL : REAL;
    LH : REAL;
END_VAR
VAR
    semantist_fb : CTRL_PI;
    semantist_out_Y : REAL;
    semantist_out_DIFF : REAL;
    semantist_out_LIM : BOOL;
END_VAR
semantist_fb(ACT := ACT, SET0 := SET0, SUP := SUP, OFS := OFS, M_I := M_I, MAN := MAN, RST := RST, KP := KP, KI := KI, LL := LL, LH := LH);
semantist_out_Y := semantist_fb.Y;
semantist_out_DIFF := semantist_fb.DIFF;
semantist_out_LIM := semantist_fb.LIM;
END_PROGRAM
