PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    Out1 : LREAL;
    In_Tmp : ARRAY[0..ArraySize] OF LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4;

END_VAR
semantist_fb(ssMethodType := ssMethodType, Out1 := Out1, In_Tmp := In_Tmp);
END_PROGRAM
