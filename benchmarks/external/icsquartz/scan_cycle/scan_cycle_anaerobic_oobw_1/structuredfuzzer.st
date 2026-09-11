FUNCTION MemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
MemSet := SysMemSet(pDest, udiValue, udiCount);
END_FUNCTION


FUNCTION SysMemSet : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    udiValue : UDINT;
    udiCount : UDINT;
END_VAR
SysMemSet := __SEMANTIST_MEMSET(pDest, udiValue, udiCount);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1
VAR_IN_OUT
	ssMethodType: SINT;
	In1: LREAL;
	In2: LREAL;
END_VAR
VAR_OUTPUT
	y: LREAL;
END_VAR
VAR
	Integrator_DSTATE: LREAL;
	rtb_Sum4: LREAL;
	size: INT;
    cycle: DINT;
END_VAR

CASE ssMethodType OF
    0:
        (* SystemInitialize for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1 1' *)
        (* InitializeConditions for DiscreteIntegrator: '<S34>/Integrator' *)
		MemSet(REF(Integrator_DSTATE), 0, size);

        ssMethodType := 1;

        (* Integrator_DSTATE := 0.0; *)
        (* End of SystemInitialize for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1 1' *)
    1:
        cycle := cycle + 1;
        
        IF In1 > 3.14 AND In1 < 3.68 AND In2 < 3.14 AND In2 > 2.5 THEN
            size := size + 1;
        END_IF

        (*  Dead end *)
        IF In2 > 0.91 AND In2 < 2.17 THEN
            ssMethodType := 2;
            RETURN
        END_IF

        (*  Bug *)
        IF In1 > 2.0 AND In1 < 3.5 THEN
            ssMethodType := 0;
        END_IF

        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1 1' *)
        (* Gain: '<S1>/TT - SP Transducer' *)
        rtb_Sum4 := 0.32 * In1;
        (* Saturate: '<S1>/Sat Block (mA)' *)

        IF rtb_Sum4 >= 4.8 THEN
            rtb_Sum4 := 4.8;
        ELSIF rtb_Sum4 <= -11.2 THEN
            rtb_Sum4 := -11.2;
        END_IF;

        (* End of Saturate: '<S1>/Sat Block (mA)' *)


        (* Sum: '<S1>/Sum4' *)
        rtb_Sum4 := rtb_Sum4 - In2;
        (* Outport: '<Root>/y' incorporates:
         *  DiscreteIntegrator: '<S34>/Integrator'
         *  Gain: '<S39>/Proportional Gain'
         *  Sum: '<S43>/Sum' *)
        y := (18.7463 * rtb_Sum4) + Integrator_DSTATE;
        (* Update for DiscreteIntegrator: '<S34>/Integrator' incorporates:
         *  Gain: '<S31>/Integral Gain' *)
        Integrator_DSTATE := ((1.60192 * rtb_Sum4) * 0.05) + Integrator_DSTATE;
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1 1' *)

    2:
        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_1 1' *)
        (* Gain: '<S1>/TT - SP Transducer' *)
END_CASE;

END_FUNCTION_BLOCK


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


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
