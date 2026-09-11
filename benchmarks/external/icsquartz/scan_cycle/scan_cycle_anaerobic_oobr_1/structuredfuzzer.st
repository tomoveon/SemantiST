FUNCTION BitCpy : BOOL
VAR_INPUT
    pDest : REF_TO BYTE;
    wDstStartBit : WORD;
    pSource : REF_TO BYTE;
    wSrcStartBit : WORD;
    wSize : WORD;
END_VAR
BitCpy := __SEMANTIST_BITCPY(pDest, wDstStartBit, pSource, wSrcStartBit, wSize);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1
VAR_IN_OUT
    ssMethodType: SINT;
    In_Recv: ARRAY[0..7] OF DINT;
	In_Bit_Offset: WORD;
END_VAR
VAR_OUTPUT
    y: LREAL;
END_VAR
VAR
	In: ARRAY[0..7] OF LREAL;
    Integrator_DSTATE: LREAL;
    rtb_Sum4: LREAL;
    CycleCount: DINT;
END_VAR

CASE ssMethodType OF
    0:
        (* SystemInitialize for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1 1' *)
        (* InitializeConditions for DiscreteIntegrator: '<S34>/Integrator' *)
        Integrator_DSTATE := 0.0;
        (* End of SystemInitialize for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1 1' *)
    1:
        (*  Copy input using pointer and given offset. If offset is out of the input array bounds, this results in an Out-of-Bounds read. *)
        BitCpy(REF(In[0]), 0, REF(In_Recv), In_Bit_Offset, 2 * 8 * SIZEOF(LREAL));
        CycleCount := CycleCount + 1;

        (*  We also increment In_Bit_Offset which can also cause oob read if base value is high enough *)
        IF In_Recv[0] >= 375 AND In_Recv[0] < 400 AND In_Recv[3] < 1000 AND In_Recv[3] > 250 AND In_Recv[4] < -10 AND In_Recv[4] > -100 THEN
            IF CycleCount MOD 20 = 0 THEN
                In_Bit_Offset := In_Bit_Offset + 256;
            END_IF
        END_IF

        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1 1' *)
        (* Gain: '<S1>/TT - SP Transducer' *)
        rtb_Sum4 := 0.32 * In[0];
        (* Saturate: '<S1>/Sat Block (mA)' *)
        IF rtb_Sum4 >= 4.8 THEN
            rtb_Sum4 := 4.8;
        ELSIF rtb_Sum4 <= -11.2 THEN
            rtb_Sum4 := -11.2;
        END_IF;
        (* End of Saturate: '<S1>/Sat Block (mA)' *)

        IF In_Recv[1] < 30 AND In_Recv[1] > 15 THEN
            ssMethodType := 2;
        END_IF
        (* Sum: '<S1>/Sum4' *)
        rtb_Sum4 := rtb_Sum4 - In[1];
        (* Outport: '<Root>/y' incorporates:
         *  DiscreteIntegrator: '<S34>/Integrator'
         *  Gain: '<S39>/Proportional Gain'
         *  Sum: '<S43>/Sum' *)
        y := (18.7463 * rtb_Sum4) + Integrator_DSTATE + 1;
        (* Update for DiscreteIntegrator: '<S34>/Integrator' incorporates:
         *  Gain: '<S31>/Integral Gain' *)
        Integrator_DSTATE := ((1.60192 * rtb_Sum4) * 0.05) + Integrator_DSTATE;
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1 1' *)
    2:
        (*  Done! *)
END_CASE;

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    In_Recv : ARRAY[0..7] OF DINT;
    In_Bit_Offset : WORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_1;
    semantist_out_y : LREAL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, In_Recv := In_Recv, In_Bit_Offset := In_Bit_Offset);
semantist_out_y := semantist_fb.y;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
