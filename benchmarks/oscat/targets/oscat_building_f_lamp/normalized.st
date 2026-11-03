FUNCTION_BLOCK F_LAMP
VAR_INPUT
	SWITCH : BOOL;
	DIMM : BYTE := 255;
	RST : BOOL;
END_VAR
VAR_OUTPUT
	LAMP : BYTE;
	STATUS : BYTE;
END_VAR
VAR_IN_OUT
	ONTIME : UDINT;
	CYCLES : UDINT;
END_VAR
VAR_INPUT (* CONSTANT *)
	T_NO_DIMM : UINT := 100;
	T_MAINTENANCE : UINT := 15000;
END_VAR
VAR
	runtime : ONTIME;
END_VAR

(*
version 1.2	22. oct. 2008
programmer 	hugo
tested by	oscat

F_Lamp is an interface to flourescent lamps. its main purpose is to protect flourescent lamps from dimming within the first T_NO_DIMM Time.
If Flourescent Lamps are dimmed during the first 100 hours the lifetime of the lamp is reduced dramatically.
The interface also counts ontime and cycles of the lamp for maintanance purposes.

*)

runtime(in := switch, seconds := ontime, cycles := cycles);

IF RST THEN
	ONTIME := 0;
	CYCLES := 0;
END_IF;
IF SWITCH THEN
	IF ontime < T_NO_DIMM * 3600 THEN
		LAMP := 255;
		STATUS := 111;
	ELSE
		LAMP := DIMM;
		STATUS := 112;
	END_IF;
ELSE
	LAMP := 0;
	STATUS := 110;
END_IF;


IF ontime >= T_maintenance * 3600 AND T_Maintenance > 0 THEN
	status := 120;
END_IF;



(* revision history
hm	8. feb. 2007	rev 1.1
	original version

hm	22. oct. 2008	rev 1.2
	changed use of ontime for ontime 2.0
	ontime is now in seconds not hours
*)
END_FUNCTION_BLOCK
