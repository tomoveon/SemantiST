FUNCTION WEEKDAY_TO_STRING : STRING
VAR_INPUT
	WDAY : INT;
	LANG : INT;
	LX : INT;
END_VAR
VAR
	ly : INT;
END_VAR

(*
version 1.1	19. oct. 2008
programmer 	hugo
tested by	oscat

WEEKDAY_TO_STRING converts an Integer 1..7 into a String with the Names of the corresponding weekday.
the decoding is according to the language setup in global vars

*)
(* @END_DECLARATION := '0' *)
IF LANG = 0 THEN ly := LANGUAGE.DEFAULT; ELSE ly := MIN(LANG, LANGUAGE.LMAX); END_IF;
IF wday < 1 OR wday > 7 THEN
	RETURN;
ELSIF LX = 0 THEN
	WEEKDAY_TO_STRING := language.WEEKDAYS[ly, WDAY];
ELSIF Lx = 2 THEN
	WEEKDAY_TO_STRING := language.WEEKDAYS2[ly, WDAY];
END_IF;


(* revisison history

hm	21. sep. 2008	rev 1.0
	original version

hm	19. oct. 2008	rev 1.1
	changed language setup constants
*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    WDAY : INT;
    LANG : INT;
    LX : INT;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := WEEKDAY_TO_STRING(WDAY := WDAY, LANG := LANG, LX := LX);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
