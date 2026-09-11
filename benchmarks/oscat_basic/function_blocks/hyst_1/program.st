PROGRAM PLC_PRG
VAR_INPUT
    In : REAL;
    high : REAL;
    low : REAL;
END_VAR
VAR
    semantist_fb : HYST_1;
    semantist_out_Q : BOOL;
    semantist_out_win : BOOL;
END_VAR
semantist_fb(In := In, high := high, low := low);
semantist_out_Q := semantist_fb.Q;
semantist_out_win := semantist_fb.win;
END_PROGRAM
