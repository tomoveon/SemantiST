FUNCTION FIND_CHAR : INT
VAR_INPUT
	str : STRING;
	pos : INT;
END_VAR
VAR
	i: INT;
	pt : REF_TO ARRAY[1..255] OF BYTE;
	stop: INT;
	X: BYTE;
END_VAR

(*
version 1.3	21. oct. 2008
programmer 	hugo
tested by	tobias

find_char searches str and returns the starting position of the first character that is not a control character.
control characters are the ascii character 00 .. 31 and 127

*)
(* @END_DECLARATION := '0' *)
pt := REF(str);
stop := LEN(str);
FOR i := MAX(pos,1) TO stop DO;
	X := pt^[i];
	IF X > 31 AND ((setup.EXTENDED_ASCII AND X <> 127) OR (NOT setup.EXTENDED_ASCII AND X < 127)) THEN
		FIND_CHAR := i;
		RETURN;
	END_IF;
END_FOR;
FIND_CHAR := 0;


(* revision history
hm	29. feb 2008	rev 1.0
	original version

hm	26. mar. 2008	rev 1.1
	char will now accept extended ascii

hm	29. mar. 2008	rev 1.2
	changed STRING to STRING

hm	21. oct. 2008	rev 1.3
	changes setup constants
	optimized code
*)


END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
    pos : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FIND_CHAR(str := str, pos := pos);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
