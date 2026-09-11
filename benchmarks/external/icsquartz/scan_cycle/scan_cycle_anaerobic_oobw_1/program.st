PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    In1 : LREAL;
    In2 : LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1;
    semantist_out_y : LREAL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, In1 := In1, In2 := In2);
semantist_out_y := semantist_fb.y;
END_PROGRAM
