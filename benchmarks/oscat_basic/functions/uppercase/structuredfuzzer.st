FUNCTION TO_UPPER : BYTE
VAR_INPUT
	IN : BYTE;
END_VAR


(*
version 1.3	16. jan. 2009
programmer 	hugo
tested by		oscat

to_upper converts a character from lowercase to uppercase

*)
(* @END_DECLARATION := '0' *)
IF in > 96 AND in < 123 THEN
   TO_UPPER := in AND 16#DF;
ELSIF in > 223 AND in <> 247 AND in <> 255 AND setup.EXTENDED_ASCII THEN
   TO_UPPER := in AND 16#DF;
ELSE
   TO_UPPER := in;
END_IF;




(* revision history
hm	6. mar. 2008	rev 1.0
	original version

hm	19. oct. 2008	rev 1.1
	changed setup constants

ks	25. oct. 2008	rev 1.2
	optimized code

hm 16. jan 2009	rev 1.3
	corrected an error in module

*)
END_FUNCTION


FUNCTION UPPERCASE : STRING
VAR_INPUT
	str : STRING;
END_VAR
VAR
	pt : REF_TO BYTE;
	pos: INT;
	l: INT;
END_VAR

(*
version 1.3	29. mar. 2008
programmer 	hugo
tested by	tobias

uppercase returns str while all letters a..z and �,�,� are converted to uppercase

*)
(* @END_DECLARATION := '0' *)
pt := REF(UPPERCASE);
UPPERCASE := str;
l := LEN(str);
FOR pos := 1 TO l DO
	pt^ := TO_UPPER(pt^);
	pt := pt + 1;
END_FOR;

(* revision histroy
hm	6. oct. 2006	rev 1.0
	original release

hm	4. feb. 2008	rev 1.1
	improved performance
	added �,�,�

hm	6. mar. 2008	rev 1.2
	added support for exteded Ascii

hm	29. mar. 2008	rev 1.3
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
semantist_result := UPPERCASE(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
