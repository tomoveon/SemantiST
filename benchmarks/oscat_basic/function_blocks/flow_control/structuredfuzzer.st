FUNCTION_BLOCK FLOW_CONTROL
VAR_INPUT
	IN : BOOL;
	REQ : BOOL;
	ENQ : BOOL;
	RST : BOOL;

	T_AUTO : TIME ;
	T_DELAY : TIME ;
END_VAR
VAR_OUTPUT
	Q : BOOL;
	STATUS : BYTE;
END_VAR
VAR
	timer : TP_1D;
END_VAR


(*
version 1.0	28. jun. 2008
programmer 	hugo
tested by	oscat

FLOW_CONTROL switches a valves depending on the input in.
flow control also limits the maximum ontime of the valve and controls pressure on the output side.

*)

(* @END_DECLARATION := '0' *)
STATUS := 100;
IF RST THEN
	Q := FALSE;
	timer(rst := TRUE);
	timer.RST := FALSE;
	STATUS := 103;
ELSIF ENQ THEN
	IF IN THEN
		status := 101;
	END_IF;
	IF REQ THEN
		(* timer will generate a timed pulse after TP goes high *)
		timer.PT1 := T_AUTO;
		timer.PTD := T_DELAY;
		timer.IN := TRUE;
		STATUS := 102;
	END_IF;
END_IF;


(* set output and run timer *)
timer();
timer.IN := FALSE;
Q := (IN AND ENQ) OR timer.Q;



(* revision history
hm 	28. jun. 2008 	rev 1.0
	original version

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    IN : BOOL;
    REQ : BOOL;
    ENQ : BOOL;
    RST : BOOL;
    T_AUTO : TIME;
    T_DELAY : TIME;
END_VAR
VAR
    semantist_fb : FLOW_CONTROL;
    semantist_out_Q : BOOL;
    semantist_out_STATUS : BYTE;
END_VAR
semantist_fb(IN := IN, REQ := REQ, ENQ := ENQ, RST := RST, T_AUTO := T_AUTO, T_DELAY := T_DELAY);
semantist_out_Q := semantist_fb.Q;
semantist_out_STATUS := semantist_fb.STATUS;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
