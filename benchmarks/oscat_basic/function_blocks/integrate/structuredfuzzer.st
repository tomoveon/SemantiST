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


FUNCTION_BLOCK INTEGRATE
VAR_INPUT
	E : BOOL ;
	X : REAL;
	K : REAL ;
END_VAR
VAR_IN_OUT
	Y : REAL;
END_VAR
VAR
	X_last : REAL;
	init: BOOL;
	last: DWORD;
	tx: DWORD;
END_VAR


(*
version 1.0	3. nov. 2008
programmer 	hugo
tested by	oscat

integrate is a plain integrator with I/O for out.

 
*)
(* @END_DECLARATION := '0' *)
(*read system time *)
tx := T_PLC_MS();

IF NOT init THEN
	init := TRUE;
	X_last := X;
ELSIF E THEN
	Y := (X + X_LAST) * 0.5E-3 * DWORD_TO_REAL(tx-last) * K + Y;
	X_last := X;
END_IF;
last := tx;



(*
hm 3. nov. 2008	rev 1.0
original version
	
*)

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    E : BOOL;
    X : REAL;
    K : REAL;
    Y : REAL;
END_VAR
VAR
    semantist_fb : INTEGRATE;

END_VAR
semantist_fb(E := E, X := X, K := K, Y := Y);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
