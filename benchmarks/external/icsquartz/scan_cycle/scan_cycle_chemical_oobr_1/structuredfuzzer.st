FUNCTION SysMemMove : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemMove := __SEMANTIST_MEMMOVE(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBR_1
VAR_IN_OUT
    ssMethodType: SINT;
    SetpointOffsetRecv: USINT;
    MeasOffestRecv: USINT;
    iter: INT;

	SetpointRecv: ARRAY[0..1] OF LREAL;
	MeasRecv: ARRAY[0..1] OF LREAL;
END_VAR
VAR
    DeltaOutput: LREAL;
    UnitDelay_DSTATE: LREAL;

    rtb_Sum: LREAL;

	Setpoint: ARRAY[0..20] OF LREAL := [0.0, 0.0];
    Meas: ARRAY[0..20] OF LREAL := [0.0, 0.0];

	dwSize : INT := 16;
	locBufferHandle: DWORD;
END_VAR

CASE ssMethodType OF
    0:
        (* InitializeConditions for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := 0.0;
        ssMethodType := 1;
    1:

		(*  Read Inputs from provided possibly out-of-bounds offsets *)
		(* Older: SysMemMove(REF(Setpoint), REF(SetpointRecv), 8); *)
		(* Older: SysMemMove(REF(Meas), REF(MeasRecv), 160); *)

        SysMemMove(REF(Setpoint), REF(SetpointRecv), SetpointOffsetRecv);
		SysMemMove(REF(Meas), REF(MeasRecv), MeasOffestRecv);

        iter := iter + 1.0;

        (* Sum: '<S1>/Sum' *)
        IF SetPointRecv[0] > 10.31 AND SetPointRecv[1] < 10.31 THEN
            rtb_Sum := Setpoint[0] - Meas[0];

            SetpointOffsetRecv := SetpointOffsetRecv + 30;
            MeasOffestRecv := MeasOffestRecv + 30;
        END_IF
        (* Outport: '<Root>/Delta Output' incorporates:
         *  Gain: '<S1>/Gain'
         *  Gain: '<S1>/Gain1'
         *  Sum: '<S1>/Sum1'
         *  UnitDelay: '<S1>/Unit Delay' *)
        IF SetPointRecv[0] <= -10.31 AND SetPointRecv[0] > 20.31 THEN
            DeltaOutput := (((30.0 * rtb_Sum) + rtb_Sum) - UnitDelay_DSTATE) * 0.0004;
            (* Update for UnitDelay: '<S1>/Unit Delay' *)
            UnitDelay_DSTATE := rtb_Sum;
        END_IF

        IF SetpointRecv[0] <= 3.14 AND SetPointRecv[0] > 0 THEN
            ssMethodType := 2;
        END_IF

    2:
        (*  DO nothing *)

END_CASE;

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    SetpointOffsetRecv : USINT;
    MeasOffestRecv : USINT;
    iter : INT;
    SetpointRecv : ARRAY[0..1] OF LREAL;
    MeasRecv : ARRAY[0..1] OF LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBR_1;

END_VAR
semantist_fb(ssMethodType := ssMethodType, SetpointOffsetRecv := SetpointOffsetRecv, MeasOffestRecv := MeasOffestRecv, iter := iter, SetpointRecv := SetpointRecv, MeasRecv := MeasRecv);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
