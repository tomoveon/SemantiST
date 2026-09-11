FUNCTION CLEAN : STRING
VAR_INPUT
	IN : STRING;
	CX : STRING;
END_VAR
VAR
	pos: INT := 1;
	stop: INT;
END_VAR


(*
version 1.0	18. jun. 2008
programmer 	hugo
tested by	oscat

Clean deletes all characters from a string except the ones specified in CX.

*)

(* @END_DECLARATION := '0' *)
(* copy input string *)
CLEAN := IN;
stop := LEN(in);
WHILE pos <= stop DO
	IF FIND(cx, MID(CLEAN, 1, pos)) > 0 THEN
		(* charcter found skip to next one *)
		pos := pos + 1;
	ELSE
		(* wrong chracter needs to be deleted *)
		CLEAN := DELETE(CLEAN, 1, pos);
		stop := stop - 1;	(* the string is one character shorter now *)
	END_IF;
END_WHILE;



(* revision history

hm		18. jun. 2008	rev 1.0		
	original version 


*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    IN : STRING;
    CX : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := CLEAN(IN := IN, CX := CX);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
