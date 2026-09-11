FUNCTION LIST_GET : STRING
VAR_INPUT
	SEP : BYTE;
	POS : INT;
END_VAR
VAR_IN_OUT
	LIST : STRING;
END_VAR
VAR
	i : INT := 1;
	o : INT := 1;
	pt : REF_TO ARRAY[1..250] OF BYTE;
	po : REF_TO ARRAY[1..250] OF BYTE;
	cnt: INT := 0;
	c: BYTE;
END_VAR


(*
version 2.0	21. mar. 2011
programmer 	hugo
tested by		oscat

LIST_GET liefert das element an der stelle pos einer liste.
die liste liegt als string von elementen vor die mit den zeichen SEP am anfang makiert sind.

*)
(* @END_DECLARATION := '0' *)
pt := REF(LIST);							(* load pointers *)
po := REF(LIST_GET);

REPEAT
	c := pt^[i];
	IF cnt = pos THEN						(* copy element to output string *)
		IF c = SEP  THEN EXIT; END_IF;		(* next element stop copy and finish *)
		po^[o] := c;
		o := o + 1;
	ELSIF c = SEP THEN
		cnt := cnt + 1;
	END_IF;
	i := i + 1;
UNTIL
	(i = 250) OR (c = 0)
END_REPEAT;
po^[o] := 0;						(* close the output string *)



(* revision histroy
hm	20. jun. 2008	rev 1.0
	original release

hm	19. jan. 2011	rev 1.1
	changed STRING to strring(250)

hm	21. mar. 2011	rev 2.0
	all list elements start with SEP
*)	

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    SEP : BYTE;
    POS : INT;
    LIST : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := LIST_GET(SEP := SEP, POS := POS, LIST := LIST);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
