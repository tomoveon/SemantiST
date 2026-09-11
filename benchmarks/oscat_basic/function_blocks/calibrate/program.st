PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
    CO : BOOL;
    CS : BOOL;
    Y_Offset : REAL;
    Y_Scale : REAL;
END_VAR
VAR
    semantist_fb : CALIBRATE;
    semantist_out_Y : REAL;
END_VAR
semantist_fb(X := X, CO := CO, CS := CS, Y_Offset := Y_Offset, Y_Scale := Y_Scale);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM
