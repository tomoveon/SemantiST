PROGRAM PLC_PRG
VAR_INPUT
    Kt : REAL;
    Tt : REAL;
    PI : BOOL;
    PID : BOOL;
    P_K : REAL;
    PI_K : REAL;
    PI_TN : REAL;
    PID_K : REAL;
    PID_TN : REAL;
    PID_TV : REAL;
END_VAR
VAR
    semantist_fb : CONTROL_SET1;
    semantist_out_KP : REAL;
    semantist_out_TN : REAL;
    semantist_out_TV : REAL;
    semantist_out_KI : REAL;
    semantist_out_KD : REAL;
END_VAR
semantist_fb(Kt := Kt, Tt := Tt, PI := PI, PID := PID, P_K := P_K, PI_K := PI_K, PI_TN := PI_TN, PID_K := PID_K, PID_TN := PID_TN, PID_TV := PID_TV);
semantist_out_KP := semantist_fb.KP;
semantist_out_TN := semantist_fb.TN;
semantist_out_TV := semantist_fb.TV;
semantist_out_KI := semantist_fb.KI;
semantist_out_KD := semantist_fb.KD;
END_PROGRAM
