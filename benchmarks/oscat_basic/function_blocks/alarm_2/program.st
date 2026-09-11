PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
    LO_1 : REAL;
    HI_1 : REAL;
    LO_2 : REAL;
    HI_2 : REAL;
    HYS : REAL;
END_VAR
VAR
    semantist_fb : ALARM_2;
    semantist_out_Q1_LO : BOOL;
    semantist_out_Q1_HI : BOOL;
    semantist_out_Q2_LO : BOOL;
    semantist_out_Q2_HI : BOOL;
END_VAR
semantist_fb(X := X, LO_1 := LO_1, HI_1 := HI_1, LO_2 := LO_2, HI_2 := HI_2, HYS := HYS);
semantist_out_Q1_LO := semantist_fb.Q1_LO;
semantist_out_Q1_HI := semantist_fb.Q1_HI;
semantist_out_Q2_LO := semantist_fb.Q2_LO;
semantist_out_Q2_HI := semantist_fb.Q2_HI;
END_PROGRAM
