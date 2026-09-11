FUNCTION FINDB_NUM : INT
VAR_INPUT
	str : STRING;
END_VAR
VAR
	pos: INT;
	pt : REF_TO BYTE;
END_VAR

(*
version 1.2	29. mar. 2008
programmer 	hugo
tested by	tobias

findB_Num searches str backward and returns the last position of a number
a number is characterized by a letter "0..9" or "."

*)
(* @END_DECLARATION := '0' *)
pt := REF(str) + LEN(str) - 1;
FOR pos := LEN(str) TO 1 BY -1 DO;
	IF (PT^ > 47 AND PT^ < 58) OR PT^ = 46 THEN
		FINDB_NUM := pos;
		RETURN;
	END_IF;
	PT := PT - 1;
END_FOR;
FINDB_NUM := 0;

(* revision history
hm	6. oct 2006		rev 1.0
	original version

hm	29. feb 2008	rev 1.1
	improved performance

hm	29. mar. 2008	rev 1.2
	changed STRING to STRING
*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FINDB_NUM(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
