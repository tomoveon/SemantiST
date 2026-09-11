FUNCTION INC2 : INT
VAR_INPUT
	X : INT;
	D : INT;
	L : INT;
	U : INT;
END_VAR
VAR
	tmp: INT;
END_VAR


(*
version 1.0	29. jun. 2008
programmer 	hugo
tested by	oscat

This function increments the input X by the value D and compares the result with U.
if the output exceeds U it will continue to count from L again.

*)
(* @END_DECLARATION := '0' *)
tmp := U - L + 1;
INC2 := (X + D - L + tmp) MOD tmp + L;



(* revision history
hm	29. jun. 2008		REV 1.0
	original version


*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    X : INT;
    D : INT;
    L : INT;
    U : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := INC2(X := X, D := D, L := L, U := U);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
