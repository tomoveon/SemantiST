PROGRAM PLC_PRG
VAR_INPUT
    F : REAL;
    DC : REAL;
END_VAR
VAR
    semantist_fb : PWM_DC;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(F := F, DC := DC);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM
