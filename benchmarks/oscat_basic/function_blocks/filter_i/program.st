PROGRAM PLC_PRG
VAR_INPUT
    X : INT;
    T : TIME;
END_VAR
VAR
    semantist_fb : FILTER_I;
    semantist_out_Y : INT;
END_VAR
semantist_fb(X := X, T := T);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM
