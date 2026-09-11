FUNCTION FINDP : INT
VAR_INPUT
	str : STRING;
	src : STRING;
	pos: INT;
END_VAR
VAR
	i: INT;
	ls: INT;
	lx: INT;
	stp: INT;
END_VAR

(*
version 1.2	29. mar. 2008
programmer 	hugo
tested by	tobias

the function findP searches a string str for the occurence of src beginning at the position pos.

*)
(* @END_DECLARATION := '0' *)
ls := LEN(str);
lx := LEN(src);
IF ls < lx OR lx = 0 THEN RETURN; END_IF;
stp := ls - lx + 1;
FOR i := MAX(pos,1) TO stp DO
	IF MID(str,lx,i) = src THEN
		FINDP := i;
		RETURN;
	END_IF;
END_FOR;
FINDP := 0;

(* revision histroy
hm	4. feb. 2008	rev 1.0
	original release

hm	29. feb 2008	rev 1.1
	ADDED MAX(pos,1) in loop initialization

hm	29. mar. 2008	rev 1.2
	changed STRING to STRING
*)	

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
    src : STRING;
    pos : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := FINDP(str := str, src := src, pos := pos);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
