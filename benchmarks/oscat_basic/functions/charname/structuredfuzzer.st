FUNCTION CHR_TO_STRING : STRING
VAR_INPUT
	C : BYTE;
END_VAR
VAR
	PT : REF_TO BYTE;
END_VAR

(*
version 1.3	17. dec. 2008
programmer 	hugo
tested by	oscat

CHR creates a character from a byte input and stuffs it in a one character length string.

*)
(* @END_DECLARATION := '0' *)
PT := REF(CHR_TO_STRING);
PT^ := C;
pt := pt + 1;
pt^ := 0;

(* revision history
hm	16 jan 2007		rev 1.0
	original version

hm	4. feb. 2008	rev 1.1
	return string would not be terminated properly

hm	29. mar. 2008	rev 1.2
	changed STRING to STRING

hm	17. dec. 2008	rev 1.3
	changed name of function from chr to chr_to_string
*)
END_FUNCTION


FUNCTION CHARNAME : STRING
VAR_INPUT
	C : BYTE;
END_VAR
VAR
	pos: INT;
	i : INT;
END_VAR

(*
version 1.4		17. dec. 2008
programmer 		hugo
tested by		oscat

CHARNAME converts a Character code into its HTML character name
� is convterted to '�uml'
� is converted to 'euro'
the character itself is returned if no name is available for the character

*)
(* @END_DECLARATION := '0' *)
IF C <> 0 THEN
	(* construct search string from code followed by $ sign, also clear charname string*)
	CHARNAME := CHR_TO_STRING(C);
	CHARNAME := CONCAT(CHARNAME,'&');
	CHARNAME := CONCAT(';', CHARNAME);
	WHILE pos = 0 AND (i < 4) DO
		i := i + 1;
		pos := FIND(setup.CHARNAMES[i], CHARNAME);
	END_WHILE;
	IF pos > 0 THEN
		CHARNAME := MID(setup.CHARNAMES[i], 10, pos + 3);
		(* search for end of name and truncate *)
		pos := FIND(CHARNAME, ';');
		CHARNAME := LEFT(CHARNAME,pos - 1);
	ELSE
		CHARNAME := CHR_TO_STRING(C);
	END_IF;
ELSE
	CHARNAME := '';
END_IF;




(* revision history
hm	13. may. 2008	rev 1.0
	original version

hm	16. jun. 2008	rev 1.1
	changed nested call of concat for better compatibility

hm	19. oct. 2008	rev 1.2
	changes setup constants

hm	24. oct. 2008	rev 1.3
	new code for high performance

hm	17. dec. 2008	rev 1.4
	changed function chr to chr_to_string
*)

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    C : BYTE;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := CHARNAME(C := C);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
