PROGRAM PLC_PRG
VAR_INPUT
    CLK : BOOL;
    rst : BOOL;
END_VAR
VAR
    semantist_fb : TOGGLE;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(CLK := CLK, rst := rst);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM
