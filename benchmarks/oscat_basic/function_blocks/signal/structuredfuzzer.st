FUNCTION T_PLC_MS : DWORD
VAR
	debug : BOOL := 0;
	N : INT := 0;
	offset : DWORD := 0;
END_VAR
VAR
	tx : TIME;
END_VAR

(*
version 1.2	16. nov 2008
programmer 	hugo
tested by	oscat

T_PLC_MS reads the internal PLC timer and return the time, it has the advantage to be able to set a debug mode 
and speed up the counter to test the plc timer overrun which occurs every 50 days respectively 25 days at siemens S7
this routine also allows to correct the behavior of s7 where the internal plc counter will not count all 32 bits.

*)
(* @END_DECLARATION := '0' *)
tx := TIME();
T_PLC_MS := TIME_TO_DWORD(Tx);
(* hier muss die korrektur f�r step7 stattfinden
plctime muss den vollen wertebereich von time ausnutzen:
wenn bei step7 time -24tage bis plus 24 tage ist dann muss der timer auch beim �berlauf auf -24tage springen 
und auf keinen fall auf 0 !!!!
f�r siemens muss ein weiterer fb im main eingebunden werden der sicherstellt das alle 32 bits durchgez�hlt werden.
es kann nur ein fb sein den er muss sich das oberste (32te) bit merken.
oder etwa spring s7 bei �berlauf auf -24 tage????? dann w�re keine korrektur n�tig.
*)
IF debug THEN
	T_PLC_MS := (SHL(T_PLC_MS,N) OR SHL(DWORD#1,N)-1) + OFFSET;
END_IF;

(* revision history
hm	14.9.2007	rev 1.0
	original version

hm	12. nov 2007	rev 1.1
	added temporaray variable tx because some compilers could not handle time() as an argument

hm	16. nov. 2008	rev 1.2
	initialized constants with 0 for compatibility reasons
*)
END_FUNCTION


FUNCTION_BLOCK SIGNAL
VAR_INPUT
	IN : BOOL;
	SIG : BYTE;
	TS : TIME;
END_VAR
VAR_OUTPUT
	Q : BOOL;
END_VAR
VAR
	tx: DWORD;
	step: BYTE;
END_VAR
VAR
	one : BYTE := 1;
END_VAR

(*
version 1.0	13 dec 2007
programmer 	hugo
tested by		tobias

this function generates an output signal according to a bit pattern SIG.
the patter is shifted to the output in time steps of TS.
ts defaults to 128 ms if not specified.
*)

(* @END_DECLARATION := '0' *)
IF in THEN
	(* an alarm is present read system time first *)
	tx := T_PLC_MS();
	(* calculate the step counter which is the lowest 3 bits of (time / ts) *)
	IF ts > t#0s THEN
		step := DWORD_TO_BYTE(tx / TIME_TO_DWORD(ts) AND 16#0000_0007);
	ELSE
		step := DWORD_TO_BYTE(SHR(tx,7) AND 16#0000_0007);
	END_IF;
	(* convert the value 0-7 in step into one bit only (bit 0-7) *)
	step := SHL(one,step);
	(* generate the output signal *)
	Q := (step AND sig) > 0;
ELSE
	Q := FALSE;
END_IF;

(* revision history
hm	13.12.2007		rev 1.0
	original version

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    IN : BOOL;
    SIG : BYTE;
    TS : TIME;
END_VAR
VAR
    semantist_fb : SIGNAL;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(IN := IN, SIG := SIG, TS := TS);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
