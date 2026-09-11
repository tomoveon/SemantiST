FUNCTION_BLOCK STACK_16
VAR_INPUT
	Din : DWORD;
	E : BOOL ;
	RD : BOOL;
	WD : BOOL;
	RST : BOOL;
END_VAR
VAR_OUTPUT
	Dout : DWORD;
	EMPTY : BOOL := TRUE;
	FULL : BOOL;
END_VAR
VAR
	stack : ARRAY[0..15] OF DWORD;
	pt : INT;
END_VAR
VAR
	n : INT := 15;	(* changing this value will chage the number of stored elements in the fifo *)
END_VAR


(*
version 2.0	25. jul 2009
programmer 	hugo
tested by		oscat

16 Dword STACK memory

*)
(* @END_DECLARATION := '0' *)
IF RST THEN
	(* asynchronous reset for the fifo *)
	pt := 0;
	EMPTY := TRUE;
	FULL := FALSE;
	Dout := 0;
ELSIF E THEN
	IF NOT EMPTY AND RD THEN
		(* read one element *)
		pt := pt - 1;
		Dout := stack[pt];
		EMPTY := pt = 0;
		FULL := FALSE;
	END_IF;
	IF NOT FULL AND WD THEN
		(* write one element *)
		stack[pt] := Din;
		pt := pt + 1;
		FULL := pt > n;
		EMPTY := FALSE;
	END_IF;
END_IF;


(* revision history
hm	4. aug. 2006	rev 1.0
	original version

hm	19. feb 2008	rev 1.1
	performance improvements

hm	17. oct. 2008	rev 1.2
	deleted unnecessary init with 0

hm	27. oct. 2008	rev 1.3
	optimized performance

hm	25. jul 2009	rev 2.0
	changed inputs to allow simultsaneous read and write
*)


END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    Din : DWORD;
    E : BOOL;
    RD : BOOL;
    WD : BOOL;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : STACK_16;
    semantist_out_Dout : DWORD;
    semantist_out_EMPTY : BOOL;
    semantist_out_FULL : BOOL;
END_VAR
semantist_fb(Din := Din, E := E, RD := RD, WD := WD, RST := RST);
semantist_out_Dout := semantist_fb.Dout;
semantist_out_EMPTY := semantist_fb.EMPTY;
semantist_out_FULL := semantist_fb.FULL;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
