PROGRAM PLC_PRG
VAR_INPUT
    IN : BOOL;
    REQ : BOOL;
    ENQ : BOOL;
    RST : BOOL;
    T_AUTO : TIME;
    T_DELAY : TIME;
END_VAR
VAR
    semantist_fb : FLOW_CONTROL;
    semantist_out_Q : BOOL;
    semantist_out_STATUS : BYTE;
END_VAR
semantist_fb(IN := IN, REQ := REQ, ENQ := ENQ, RST := RST, T_AUTO := T_AUTO, T_DELAY := T_DELAY);
semantist_out_Q := semantist_fb.Q;
semantist_out_STATUS := semantist_fb.STATUS;
END_PROGRAM
