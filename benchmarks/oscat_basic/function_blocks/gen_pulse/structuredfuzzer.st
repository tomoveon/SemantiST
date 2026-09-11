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


FUNCTION_BLOCK GEN_PULSE
VAR_INPUT
	ENQ : BOOL ;
	PTH : TIME;
	PTL : TIME;
END_VAR
VAR_OUTPUT
	Q : BOOL;
END_VAR
VAR
	tx: TIME;
	tn: TIME;
	init: BOOL;
END_VAR

(*
version 1.5	8. apr. 2011
programmer 	hugo
tested by		oscat

GEN_PULSE uses the internal sps timer to generate a continuous output waveform with programmable high and low time.
the accuracy of gen_pulse is depending on the system timer.
when time is 0 the high and low times are exactly one cycle.
ENQ = TRUE will start and ENQ = FALSE will stop the generator.

*)
(* @END_DECLARATION := '0' *)
IF enq THEN
	tx := DWORD_TO_TIME(T_PLC_MS());
	IF NOT init THEN init := TRUE; tn := tx; END_IF;
	IF tx - tn >= SEL(Q, PTL, PTH) THEN
		tn := tn + SEL(Q, PTL, PTH);
		Q := NOT Q;
	END_IF;
ELSE
	Q := FALSE;
	init := FALSE;
END_IF;


(* revision history
hm	29. jun. 2008	rev 1.0
	original version

hm	23. nov. 2008	rev 1.1
	set default for enq to be true

hm	18. jul. 2009	rev 1.2
	improved performance

hm	13. nov. 2009	rev 1.3
	corrected error

hm	16. feb. 2011	rev 1.4
	corrected an error when timer overflows

hm	8. apr. 2011	rev 1.5
	ptl and pth was exchanged
*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ENQ : BOOL;
    PTH : TIME;
    PTL : TIME;
END_VAR
VAR
    semantist_fb : GEN_PULSE;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(ENQ := ENQ, PTH := PTH, PTL := PTL);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
