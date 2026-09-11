FUNCTION_BLOCK LIST_NEXT
VAR_INPUT
	SEP : BYTE;
	RST : BOOL;
END_VAR
VAR_IN_OUT
	LIST : STRING;
END_VAR
VAR_OUTPUT
	LEL : STRING;
	NUL : BOOL;
END_VAR
VAR
	pos : INT := 1;
	pt : REF_TO ARRAY[1..250] OF BYTE;
	po : REF_TO ARRAY[1..250] OF BYTE;
	c: BYTE;
	write: INT;
END_VAR


(*
version 2.0	21. mar. 2011
programmer 	hugo
tested by		oscat

LIST_NEXT retrieves the next element of a list, starting from element 1 after reset or first init.
when the end of the lisat is reached a '' empty string is returned and NUL is set to true.
die liste liegt als string von elementen vor die mit den zeichen SEP am anfang markiert sind.

*)
(* @END_DECLARATION := '0' *)
pt := REF(LIST);
po := REF(LEL);

IF rst THEN
	pos := 1;
END_IF;

IF pt^[pos] = 0 OR pos = 250 THEN
	LEL := '';
	NUL := TRUE;
ELSE
	NUL := FALSE;
	write := 1;
	FOR pos := pos + 1 TO 250 DO
		c := pt^[pos];
		IF c = 0 OR c = SEP THEN
			po^[write] := 0;
			RETURN;
		ELSE
			po^[write] := pt^[pos];
			write := write + 1;
		END_IF;
	END_FOR;
END_IF;




(* revision histroy
hm	25. jun. 2008	rev 1.0
	original release

hm	19. jan. 2011	rev 1.1
	changed STRING to STRING	

hm	21. mar. 2011	rev 2.0
	all elements start with SEP

*)	

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    SEP : BYTE;
    RST : BOOL;
    LIST : STRING;
END_VAR
VAR
    semantist_fb : LIST_NEXT;
    semantist_out_LEL : STRING;
    semantist_out_NUL : BOOL;
END_VAR
semantist_fb(SEP := SEP, RST := RST, LIST := LIST);
semantist_out_LEL := semantist_fb.LEL;
semantist_out_NUL := semantist_fb.NUL;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
