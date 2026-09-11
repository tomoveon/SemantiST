FUNCTION SysMemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemMove := __SEMANTIST_MEMMOVE(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_3
VAR_IN_OUT
	ssMethodType: SINT;
	In1_Size: WORD;

    In1_Ptr: POINTER TO LREAL;
END_VAR
VAR_OUTPUT
    Out1: LREAL;
END_VAR
VAR
	In1: LREAL;
END_VAR

CASE ssMethodType OF

    0:
        (*  Initialize *)
        In1 := 0.0;
        Out1 := 0.0;
        In1_Size := 1;
        ssMethodType := 1;

    1:

        (*  Move input. Using a large size results in an Out of Bounds read/write *)
        SysMemMove(REF(In1), In1_Ptr, In1_Size);
        (*  printf('In1_Size: %d$N', In1_Size); *)
        (*  In1_Size := In1_Size + 1; *)

        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_3 3' *)
        (* Gain: '<S1>/T-Sensor' *)
        IF In1 <= 0.0 AND In1 >= -10.14 THEN
            Out1 := 0.32 * In1;
            In1_Size := In1_Size + 1;
        END_IF

        Out1 := 0.32 * In1;
        (* Saturate: '<S1>/Sat Block (mA) - S' *)
        IF Out1 >= 4.8 THEN
            (* Outport: '<Root>/Out1' *)
            Out1 := 4.8;
        ELSIF Out1 <= -11.2 THEN
            (* Outport: '<Root>/Out1' *)
            Out1 := -11.2;
        END_IF;
        (* End of Saturate: '<S1>/Sat Block (mA) - S' *)
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_3 3' *)

        IF In1 < 3.14 AND In1 > 0 THEN
            ssMethodType := 2;
        END_IF

    2:
        (*  Done :) *)
END_CASE

END_FUNCTION_BLOCK


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


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
