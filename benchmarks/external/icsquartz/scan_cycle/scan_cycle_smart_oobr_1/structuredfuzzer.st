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


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1
VAR_IN_OUT
    ssMethodType: SINT;
    In_Recv: ARRAY[0..1] OF LREAL;
	In_Bit_Offset: WORD;
END_VAR
VAR_OUTPUT
    x: LREAL;
END_VAR
VAR
    Square: LREAL;
    Filter_DSTATE: LREAL;
    Integrator_DSTATE: LREAL;

    rtb_Subtract: LREAL;
    rtb_FilterCoefficient: LREAL;

	In1: ARRAY[0..1] OF LREAL;
END_VAR

CASE ssMethodType OF
    0:
        (* SystemInitialize for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)
        (* InitializeConditions for DiscreteIntegrator: '<S29>/Filter' *)
        Filter_DSTATE := 0.0;
        (* InitializeConditions for DiscreteIntegrator: '<S34>/Integrator' *)
        Integrator_DSTATE := 0.0;
        (* End of SystemInitialize for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)

        (* ConstCode for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)
        (* ConstCode for Math: '<S1>/Square' incorporates:
         *  Constant: '<S1>/Constant' *)
        Square := 458751.02394617227;
        (* End of ConstCode for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)
        ssMethodType := 1;
    1:
        BitCpy(REF(In1[0]), 0, REF(In_Recv), In_Bit_Offset, 8 * SIZEOF(LREAL));

        IF In_Recv[0] > 3.14 AND In_Recv[0] < 30.13 THEN
            In_Bit_Offset:= In_Bit_Offset + 400;
        END_IF
        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)
        (* Sum: '<S1>/Subtract' incorporates:
         *  Math: '<S1>/Square1' *)
        rtb_Subtract := Square - (In1[0] * In1[0]);
        (* Gain: '<S37>/Filter Coefficient' incorporates:
         *  DiscreteIntegrator: '<S29>/Filter'
         *  Gain: '<S28>/Derivative Gain'
         *  Sum: '<S29>/SumD' *)
        rtb_FilterCoefficient := ((0.10932922971946307 * rtb_Subtract) - Filter_DSTATE) * 3.8620401712747761;
        (* Outport: '<Root>/y' incorporates:
         *  DiscreteIntegrator: '<S34>/Integrator'
         *  Gain: '<S39>/Proportional Gain'
         *  Sum: '<S43>/Sum' *)
        x := ((2.5899754807860003 * rtb_Subtract) + Integrator_DSTATE) + rtb_FilterCoefficient;
        (* Update for DiscreteIntegrator: '<S29>/Filter' *)
        Filter_DSTATE := (5.0E-6 * rtb_FilterCoefficient) + Filter_DSTATE;
        (* Update for DiscreteIntegrator: '<S34>/Integrator' incorporates:
         *  Gain: '<S31>/Integral Gain' *)
        Integrator_DSTATE := ((19.365373103211319 * rtb_Subtract) * 5.0E-6) + Integrator_DSTATE;
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1 2' *)

        If In_Recv[0] <= 3.14 AND In_Recv[0] > 0.5 THEN
            ssMethodType := 2;
        END_IF
    2:
        (*  Do nothing *)
END_CASE;

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    In_Recv : ARRAY[0..1] OF LREAL;
    In_Bit_Offset : WORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1;
    semantist_out_x : LREAL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, In_Recv := In_Recv, In_Bit_Offset := In_Bit_Offset);
semantist_out_x := semantist_fb.x;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
