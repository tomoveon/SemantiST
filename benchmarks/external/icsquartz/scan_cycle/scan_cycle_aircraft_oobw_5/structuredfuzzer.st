FUNCTION SysMemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemCpy := __SEMANTIST_MEMCPY(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5
VAR_IN_OUT
	ssMethodType: SINT;
	Out1: LREAL;
    In_Tmp: ARRAY[0..20] OF LREAL;
    bytesToCopy: DWORD;
END_VAR
VAR

	In: ARRAY[0..9] OF LREAL;

    UnitDelay_DSTATE: LREAL;

    rtb_Add1: LREAL;
    c_rtb_UpperLimitLowerLimitC: LREAL;
    iter: INT;
END_VAR

CASE ssMethodType OF
    0:
        (* SystemInitialize for Atomic SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)'
         *
         * Block description for '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)':
         *  Rate Limiter *)
        (* InitializeConditions for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := 0.0;
        ssMethodType := 1;
        (* End of SystemInitialize for SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)' *)
    1:
        (*  Copy variable length input array to locally initialized array. *)
        (*  This causes an out of bounds write if the input array length is greater than the local one. (Correct patch input: 40) *)
        iter := iter + 1;
        SysMemCpy(REF(In[0]), REF(In_Tmp[0]), bytesToCopy);

        (* Outputs for Atomic SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)'
         *
         * Block description for '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)':
         *  Rate Limiter *)
        (* UnitDelay: '<S1>/Unit Delay' *)
        rtb_Add1 := UnitDelay_DSTATE;
        (* Sum: '<S1>/Add' incorporates:
         *  Abs: '<S1>/Abs'
         *  Gain: '<S1>/Gain1' *)
        Out1 := (0.01 * ABS(In[1])) + rtb_Add1;
        (* Sum: '<S1>/Add1' incorporates:
         *  Abs: '<S1>/Abs1'
         *  Gain: '<S1>/Gain' *)
        rtb_Add1 := rtb_Add1 - (0.01 * ABS(In[2]));
        (* Switch: '<S2>/Upper Limit (*   Lower Limit  Check' incorporates: *)
         *  RelationalOperator: '<S2>/If UL > LL' *)
        IF Out1 > rtb_Add1 THEN
            c_rtb_UpperLimitLowerLimitC := Out1;
            Out1 := rtb_Add1;
        ELSE
            c_rtb_UpperLimitLowerLimitC := rtb_Add1;
        END_IF;
        (* End of Switch: '<S2>/Upper Limit (*   Lower Limit  Check' *) *)

        (* Switch: '<S1>/If Init Then O(* p = I//p' incorporates: *)
         *  RelationalOperator: '<S2>/If Input >= LL'
         *  Switch: '<S2>/Select Input' *)
        IF In[3] > 0.5 THEN
            Out1 := In[0];
        ELSIF In[0] >= Out1 THEN
            (* Switch: '<S2>/Select UL' incorporates:
             *  RelationalOperator: '<S2>/If UL < Input'
             *  Switch: '<S2>/Select Input' *)
                IF In[1] > 3.14 AND In[1] < 30.28 THEN
                    bytesToCopy := SIZEOF(LREAL) * (iter + 1);
                END_IF
            IF c_rtb_UpperLimitLowerLimitC < In[0] THEN
                Out1 := c_rtb_UpperLimitLowerLimitC;
            ELSE
                Out1 := In[0];
            END_IF;
            (* End of Switch: '<S2>/Select UL' *)
        END_IF;
        (* End of Switch: '<S1>/If Init Then O(* p = I//p' *) *)

        (* Update for UnitDelay: '<S1>/Unit Delay' *)
        UnitDelay_DSTATE := Out1;
        (* End of Outputs for SubSystem: '<Root>/RATEL (STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5 4)' *)
    2:
        (*  Do nothing *)
END_CASE;

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    Out1 : LREAL;
    In_Tmp : ARRAY[0..20] OF LREAL;
    bytesToCopy : DWORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBW_5;

END_VAR
semantist_fb(ssMethodType := ssMethodType, Out1 := Out1, In_Tmp := In_Tmp, bytesToCopy := bytesToCopy);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
