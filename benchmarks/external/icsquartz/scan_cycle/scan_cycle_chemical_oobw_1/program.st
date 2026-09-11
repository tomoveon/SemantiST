PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    iter : INT;
    size : INT;
    In1_Arg : ARRAY[0..40] OF LREAL;
    In2_Arg : ARRAY[0..40] OF LREAL;
    perhr : LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBW_1;

END_VAR
semantist_fb(ssMethodType := ssMethodType, iter := iter, size := size, In1_Arg := In1_Arg, In2_Arg := In2_Arg, perhr := perhr);
END_PROGRAM
