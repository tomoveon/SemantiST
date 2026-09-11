PROGRAM PLC_PRG
VAR_INPUT
    Set0 : BOOL;
    D0 : BOOL;
    D1 : BOOL;
    D2 : BOOL;
    D3 : BOOL;
    D4 : BOOL;
    D5 : BOOL;
    D6 : BOOL;
    D7 : BOOL;
    Clr : BOOL;
    Rst : BOOL;
END_VAR
VAR
    semantist_fb : STORE_8;
    semantist_out_Q0 : BOOL;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
    semantist_out_Q3 : BOOL;
    semantist_out_Q4 : BOOL;
    semantist_out_Q5 : BOOL;
    semantist_out_Q6 : BOOL;
    semantist_out_Q7 : BOOL;
END_VAR
semantist_fb(Set0 := Set0, D0 := D0, D1 := D1, D2 := D2, D3 := D3, D4 := D4, D5 := D5, D6 := D6, D7 := D7, Clr := Clr, Rst := Rst);
semantist_out_Q0 := semantist_fb.Q0;
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
semantist_out_Q3 := semantist_fb.Q3;
semantist_out_Q4 := semantist_fb.Q4;
semantist_out_Q5 := semantist_fb.Q5;
semantist_out_Q6 := semantist_fb.Q6;
semantist_out_Q7 := semantist_fb.Q7;
END_PROGRAM
