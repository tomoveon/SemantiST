VAR_GLOBAL CONSTANT
    SS_INITIALIZE: SINT := 0;
    SS_STEP: SINT := 1;
    SS_FIN: SINT := 2;
    ArraySize: INT := 1;
    RecvArraySize: INT := 20;
END_VAR

FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBR_1
VAR_IN_OUT
    ssMethodType: SINT;
    SetpointOffsetRecv: USINT;
    MeasOffestRecv: USINT;
    iter: INT;
END_VAR
VAR_INPUT
	SetpointRecv: ARRAY[0..ArraySize] OF LREAL;
	MeasRecv: ARRAY[0..ArraySize] OF LREAL;
END_VAR
VAR
    DeltaOutput: LREAL;
    UnitDelay_DSTATE: LREAL;

    rtb_Sum: LREAL;

	Setpoint: ARRAY[0..RecvArraySize] OF LREAL := [0.0, 0.0];
    Meas: ARRAY[0..RecvArraySize] OF LREAL := [0.0, 0.0];

	dwSize : INT := 16;
	locBufferHandle: DWORD;
END_VAR

CASE ssMethodType OF
    SS_INITIALIZE:
        (* InitializeConditions for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := 0.0;
        ssMethodType := SS_STEP;
    SS_STEP:

		// Read Inputs from provided possibly out-of-bounds offsets
		//Older: SysMemMove(REF(Setpoint), REF(SetpointRecv), 8);
		//Older: SysMemMove(REF(Meas), REF(MeasRecv), 160);

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
            ssMethodType := SS_FIN;
        END_IF

    SS_FIN:
        // DO nothing

END_CASE;

END_FUNCTION_BLOCK
