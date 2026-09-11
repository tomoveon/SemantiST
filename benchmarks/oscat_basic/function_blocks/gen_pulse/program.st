PROGRAM PLC_PRG
VAR_INPUT
    ENQ : BOOL;
    PTH : TIME;
    PTL : TIME;
END_VAR
VAR
    semantist_fb : GEN_PULSE;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(ENQ := ENQ, PTH := PTH, PTL := PTL);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM
