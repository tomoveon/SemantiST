PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    In1_Size : WORD;
    In1_Ptr : POINTER TO LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_3;
    semantist_out_Out1 : LREAL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, In1_Size := In1_Size, In1_Ptr := In1_Ptr);
semantist_out_Out1 := semantist_fb.Out1;
END_PROGRAM
