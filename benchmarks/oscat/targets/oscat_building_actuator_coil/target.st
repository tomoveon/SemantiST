FUNCTION_BLOCK ACTUATOR_COIL
VAR_INPUT
	IN : BOOL;
END_VAR
VAR_INPUT CONSTANT
	self_act_cycle : TIME := t#10d;
	self_act_time : TIME := t#1s;
END_VAR
VAR_OUTPUT
	OUT :BOOL;
	STATUS : BYTE;
END_VAR
VAR
	last : DWORD;
	init: BOOL;
	tx: DWORD;
	now: DWORD;
END_VAR


(*
version 1.0	1. jun. 2008
programmer 	hugo
tested by		tobias

this is an intelligent actuator interface for any coil actuator.
the coil actuator can only switch on/off.
coil actuators like vents can get stuck if they are not u8sed for a long time and need to be autoactivated on ce in a while.

*)

(* read system time *)
now := OSCAT_BASIC.T_PLC_MS();

(* initialize for first cycle *)
IF NOT init THEN
	last := now;
	init := TRUE;
ELSIF IN THEN
	OUT := TRUE;
	STATUS := 101; (* activated by input *)
	LAST := now;
ELSE
	OUT := FALSE;
	STATUS := 100; (* disabled *)
	(* now we need to check for self activation *)
	tx := now - last;
	IF (self_act_cycle > T#0s) AND (tx >= TIME_TO_DWORD(self_act_cycle)) THEN
		OUT := TRUE;
		STATUS := 102; (* auto activation *)
		IF tx >= TIME_TO_DWORD(self_act_cycle + self_act_time) THEN
			last := now;
			OUT := FALSE;
			STATUS := 100; (* idle *)
		END_IF;
	END_IF;
END_IF;



(*
revision history:

hm		1. jun. 2008 	rev 1.0
	original version

*)
END_FUNCTION_BLOCK
