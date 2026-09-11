PROGRAM PLC_PRG
VAR_INPUT
    VX : REAL;
    E : BOOL;
    RST : BOOL;
    PULSE_MODE : BOOL;
    UPDATE_TIME : TIME;
    X : REAL;
    Y : UDINT;
END_VAR
VAR
    semantist_fb : FLOW_METER;
    semantist_out_F : REAL;
END_VAR
semantist_fb(VX := VX, E := E, RST := RST, PULSE_MODE := PULSE_MODE, UPDATE_TIME := UPDATE_TIME, X := X, Y := Y);
semantist_out_F := semantist_fb.F;
END_PROGRAM
