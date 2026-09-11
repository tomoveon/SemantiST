FUNCTION TRIM : STRING
VAR_INPUT
	str : STRING;
END_VAR
VAR
	pos: INT;
END_VAR

(*
version 1.2	29. mar. 2008
programmer 	hugo
tested by	tobias

the function deletes all blanks within a string.

*)
(* @END_DECLARATION := '0' *)
TRIM := str;
REPEAT
	pos := FIND(trim,' ');
	IF pos > 0 THEN TRIM := REPLACE(TRIM,'',1,pos); END_IF;
UNTIL pos = 0	END_REPEAT;


(* revision histroy
hm	6.10.2006		rev 1.0
	original release

hm	20. mar. 2008	rev 1.1
	avoid to call replace with pos = 0 because some systems will produce an error

hm	29. mar. 2008	rev 1.2
	changed STRING to STRING
*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := TRIM(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
