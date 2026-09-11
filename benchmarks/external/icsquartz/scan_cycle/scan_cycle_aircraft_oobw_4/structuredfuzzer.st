FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4
VAR_IN_OUT
	ssMethodType: SINT;
	Out1: LREAL;
    In_Tmp: ARRAY[0..3] OF LREAL;
END_VAR
VAR
    UnitDelay_DSTATE: LREAL;
    rtb_Add1: LREAL;
    c_rtb_UpperLimitLowerLimitC: LREAL;
    CycleCount: INT := 0;
    iter: INT := 0;
	(*  In: ARRAY[0..3] OF LREAL; *)
END_VAR

(*  Copy variable length input array to locally initialized array. *)
(*  SysMemCpy(REF(In[0]), REF(In_Tmp[0]), SIZEOF(LREAL) * 3); *)

CASE ssMethodType OF
    0: 
        (* SystemInitialize for Atomic SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)'
         *
         * Block description for '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)':
         *  Rate Limiter *)
        (* InitializeConditions for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := 0.0;
        CycleCount := 0;
        ssMethodType := 1;
        (* End of SystemInitialize for SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)' *)
    1: 
        (* Outputs for Atomic SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)'
         *
         * Block description for '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)':
         *  Rate Limiter *)
        (* UnitDelay: '<S1>/Unit Delay' *)
        rtb_Add1 := UnitDelay_DSTATE;
        (* Sum: '<S1>/Add' incorporates:
         *  Abs: '<S1>/Abs'
         *  Gain: '<S1>/Gain1' *)
        Out1 := (0.01 * ABS(In_Tmp[1])) + rtb_Add1;
        (* Sum: '<S1>/Add1' incorporates:
         *  Abs: '<S1>/Abs1'
         *  Gain: '<S1>/Gain' *)
        rtb_Add1 := rtb_Add1 - (0.01 * ABS(In_Tmp[2]));
        (* Switch: '<S2>/Upper Limit (*   Lower Limit  Check' incorporates: *)
         *  RelationalOperator: '<S2>/If UL > LL' *)
        IF Out1 > rtb_Add1 THEN 
            c_rtb_UpperLimitLowerLimitC := Out1;
            Out1 := rtb_Add1;
            ssMethodType := 2;
        ELSE 
            c_rtb_UpperLimitLowerLimitC := rtb_Add1;
        END_IF;
        (* End of Switch: '<S2>/Upper Limit (*   Lower Limit  Check' *) *)
        
        (* Switch: '<S1>/If Init Then O(* p = I//p' incorporates: *)
         *  RelationalOperator: '<S2>/If Input >= LL'
         *  Switch: '<S2>/Select Input' *)
        IF In_Tmp[3] > 0.5 THEN 
            Out1 := In_Tmp[0];
            ssMethodType := 2;
        ELSIF In_Tmp[0] >= Out1 THEN 
            (* Switch: '<S2>/Select UL' incorporates:
             *  RelationalOperator: '<S2>/If UL < Input'
             *  Switch: '<S2>/Select Input' *)
            IF c_rtb_UpperLimitLowerLimitC < In_Tmp[0] THEN 
                Out1 := c_rtb_UpperLimitLowerLimitC;
                ssMethodType := 0;
            ELSE 
                Out1 := In_Tmp[0];
                ssMethodType := 2;
            END_IF;
            (* End of Switch: '<S2>/Select UL' *)
        END_IF;
        (* End of Switch: '<S1>/If Init Then O(* p = I//p' *) *)
        
        (* Update for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := Out1;
        (* End of Outputs for SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4 4)' *)

        (* Update Running Cycle Count *)
        CycleCount := CycleCount + 1;

        (* Magic values *)
        IF In_Tmp[0] < 1.4 AND In_Tmp[0] > 0.11 AND In_Tmp[1] > 10.9 AND In_Tmp[1] < 11.9 AND In_Tmp[2] > 34.1 AND In_Tmp[3] < 0.1 THEN
            (* Force a value clear every time we exhaust inputs *)
            IF (CycleCount MOD 9) = 0 THEN
                (* Reset values *)
                FOR iter := 1 TO 3+10 DO
                    (*  In_Tmp[iter] := 0.0; *)
                    UnitDelay_DSTATE := In_Tmp[iter];
                END_FOR;

                (* Reset cycle count *)
                CycleCount := 0;
            END_IF;

            (* Complete Process *)
            ssMethodType := 2;

        END_IF;
    2:
        (*  Do nothing *)
END_CASE;

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    Out1 : LREAL;
    In_Tmp : ARRAY[0..3] OF LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_4;

END_VAR
semantist_fb(ssMethodType := ssMethodType, Out1 := Out1, In_Tmp := In_Tmp);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
