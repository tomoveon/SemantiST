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


FUNCTION_BLOCK FILTER_I
VAR_INPUT
	X : INT;
	T : TIME;
END_VAR
VAR_OUTPUT
	Y : INT;
END_VAR
VAR
	Yi : DINT;
	last : DWORD;
	tx: DWORD;
	init: BOOL;
END_VAR

(*
version 1.0	8. nov. 2008
programmer 	hugo
tested by	oscat

FILTER_I is a low pass filter with a programmable time T used for INT format.
 
*)
(* @END_DECLARATION := '0' *)
(* read system time *)
tx := T_PLC_MS();

(* startup initialisation *)
IF NOT init OR T = t#0s THEN
	init := TRUE;
	Yi := INT_TO_DINT(X) * 1000;
ELSE
	(* to increase accuracy of the filter we calculate internal Yi wich is Y * 1000 *)
	Yi := Yi + INT_TO_DINT(X - Y) * DWORD_TO_DINT(tx - last) * 1000 / TIME_TO_DINT(T);
END_IF;
last := tx;
Y := DINT_TO_INT(yi / 1000);



(*
hm 8. nov. 2008	rev 1.0
	original version

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    X : INT;
    T : TIME;
END_VAR
VAR
    semantist_fb : FILTER_I;
    semantist_out_Y : INT;
END_VAR
semantist_fb(X := X, T := T);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
