FUNCTION INC1 : INT
VAR_INPUT
	X : INT;
	N : INT;
END_VAR



(*
version 1.2	23. feb. 2009
programmer 	hugo
tested by	oscat

This is a increment function which increments the variable X by 1 and if N is reached, it begins with 0 instead of N again.
inc1(X,3) will generate 0,1,2,0,1,.....

*)
(* @END_DECLARATION := '0' *)
IF X >= N - 1 THEN
	INC1 := 0;
ELSE
	INC1 := X + 1;
END_IF;



(* revision history
hm	13. oct. 2008	rev 1.0
	original version

hm	22. oct. 2008	rev 1.1
	added statement to return value for compatibility reasons

hm	23. feb. 2009	rev 1.2
 	when inc1 is called with X >= N inc will continue with 0
*)
END_FUNCTION


FUNCTION_BLOCK FIFO_16
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
	fifo : ARRAY[0..16] OF DWORD;
	pr : INT;
	pw : INT;
END_VAR
VAR
	n : INT := 16;	(* changing this value will chage the number of stored elements in the fifo *)
END_VAR


(*
version 2.0	24. jul. 2009
programmer 	hugo
tested by		oscat

16 Dword FIFO memory
*)
(* @END_DECLARATION := '0' *)
IF RST THEN
	pw := pr;
	FULL := FALSE;
	EMPTY := TRUE;
	Dout := 0;
ELSIF E THEN
	IF NOT EMPTY AND RD THEN
		Dout := fifo[pr];
		pr := INC1(pr,n);
		EMPTY := pr = pw;
		FULL := FALSE;
	END_IF;
	IF NOT FULL AND WD THEN
		fifo[pw] := Din;
		pw := INC1(pw,n);
		FULL := pw = pr;
		EMPTY := FALSE;
	END_IF;
END_IF;


(* revision history

hm	4. aug. 2006	rev 1.0
	original version

hm	19. feb 2008	rev 1.1
	performance improvements

hm	17. oct. 2008	rev 1.2
	improved performance

ks	27. oct. 2008	rev 1.3
	optimized coding

hm	14. mar. 2009	rev 1.4
	removed double assignments

hm 24. jul. 2009	rev 2.0
	chaged inputs E and WR to E, WD and WR
	allow read and write in one cycle

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
    semantist_fb : FIFO_16;
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
