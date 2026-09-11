FUNCTION_BLOCK COUNT_DR
VAR_INPUT
	SET0 : BOOL;
	IN : DWORD;
	UP : BOOL;
	DN : BOOL;
	STEP : DWORD ;
	MX : DWORD ;
	RST : BOOL;
END_VAR
VAR_OUTPUT
	CNT : DWORD;
END_VAR
VAR
	last_up: BOOL;
	last_dn: BOOL;
END_VAR

(*
	version 1.1	20. jan. 2011
	programmer 	hugo
	tested BY		tobias

Count_DR is a DWORD counter with independen up and dn inputs. the counter counts from 0 to mx and continues at 0 after is has reached mx
a step input sets the counters stepping width.

*)
(* @END_DECLARATION := '0' *)
IF rst THEN
	cnt := 0;
ELSIF set0 THEN
	cnt := LIMIT(0,in,mx);
ELSIF up AND NOT last_up THEN
	IF STEP > MX - CNT THEN
		CNT := CNT - MX + STEP - 1;
	ELSE
		CNT := CNT + STEP;
	END_IF;
ELSIF dn AND NOT last_dn THEN
	IF STEP > CNT THEN
		CNT := CNT - STEP + MX + 1;
	ELSE
		CNT := CNT - STEP;
	END_IF;
END_IF;
last_up := up;
last_dn := dn;

(* revision history
hm	12. jun 2008	rev 1.0
	original version

hm	20. jan. 2011	rev 1.1
	corrected init value of MX to 16#FFFFFFFF

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    SET0 : BOOL;
    IN : DWORD;
    UP : BOOL;
    DN : BOOL;
    STEP : DWORD;
    MX : DWORD;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : COUNT_DR;
    semantist_out_CNT : DWORD;
END_VAR
semantist_fb(SET0 := SET0, IN := IN, UP := UP, DN := DN, STEP := STEP, MX := MX, RST := RST);
semantist_out_CNT := semantist_fb.CNT;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
