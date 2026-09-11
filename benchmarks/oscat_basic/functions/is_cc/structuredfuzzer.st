FUNCTION IS_CC : BOOL
VAR_INPUT
	str : STRING;
	cmp : STRING;
END_VAR
VAR
	L: INT;
	pos: INT;
END_VAR

(*
version 1.1	29. mar. 2008
programmer 	Tobias
tested by	hugo

ISC_CC testet ob ein string nur aus Zeichen des Strings CMP besteht.

*)
(* @END_DECLARATION := '0' *)
L := LEN(str);
FOR pos := 1 TO L DO
	IF FIND(CMP,MID(str,1,pos)) = 0 THEN RETURN; END_IF;
END_FOR;
IS_CC := L > 0;



(* revision history
hm	19. mar 2008	rev 1.0
	original version

hm	29. mar. 2008	rev 1.1
	changed STRING to STRING
*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
    cmp : STRING;
END_VAR
VAR
    semantist_result : BOOL;
END_VAR
semantist_result := IS_CC(str := str, cmp := cmp);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
