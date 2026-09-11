PROGRAM PLC_PRG
VAR_INPUT
    IN : BOOL;
    SIG : BYTE;
    TS : TIME;
END_VAR
VAR
    semantist_fb : SIGNAL;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(IN := IN, SIG := SIG, TS := TS);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM
