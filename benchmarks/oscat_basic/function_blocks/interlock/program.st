PROGRAM PLC_PRG
VAR_INPUT
    I1 : BOOL;
    I2 : BOOL;
    TL : TIME;
END_VAR
VAR
    semantist_fb : INTERLOCK;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
END_VAR
semantist_fb(I1 := I1, I2 := I2, TL := TL);
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
END_PROGRAM
