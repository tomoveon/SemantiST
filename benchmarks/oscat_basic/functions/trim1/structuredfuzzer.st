FUNCTION TRIM1 : STRING
VAR_INPUT
	str : STRING;
END_VAR
VAR
	pos: INT;
END_VAR

(*
version 1.2	29. mar. 2008
programmer 	hugo
tested by		tobias

the function replaces all multiple blanks within a string by only one blank.
leading and ending blanks will be deleted

*)
(* @END_DECLARATION := '0' *)
TRIM1 := str;
REPEAT
	pos := FIND(trim1,'  ');
	IF pos > 0 THEN TRIM1 := REPLACE(TRIM1,' ',2,pos); END_IF;
UNTIL pos = 0	END_REPEAT;
(* beginning and ending blanks need to be stripped off *)
IF LEFT(trim1,1) = ' ' THEN trim1 := DELETE(trim1,1,1); END_IF;
IF RIGHT(trim1,1) = ' ' THEN trim1 := DELETE(trim1,1,LEN(trim1)); END_IF;


(* revision histroy
hm	4. feb. 2008    rev 1.0
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
semantist_result := TRIM1(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
