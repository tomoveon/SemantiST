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


FUNCTION_BLOCK DELAY
VAR_INPUT
	IN : REAL;
	N : INT;
	RST : BOOL;
END_VAR
VAR_OUTPUT
	OUT : REAL;
END_VAR
VAR
	buf : ARRAY[0..31] OF REAL;
	i : INT;
	init: BOOL;
	stop: INT;
END_VAR

(*
version 1.5	23. mar. 2009
programmer 	hugo
tested by		oscat

this function block delays input values by each programm cycle
after N+1 cycles the in value has shifted to the out.
N can be any alue from 0 .. 32
if n = 0 the input will be present on the output without a delay.
f N > 32 then the output will be delayed by 32 cycles.
any high on rst will load the buffer with in.

*)
(* @END_DECLARATION := '0' *)
stop := LIMIT(0,N,32) - 1;
IF rst OR NOT init THEN
	init := TRUE;
	FOR i := 0 TO stop DO buf[i] := in; END_FOR;
	out := in;
	i := 0;
ELSIF stop < 0 THEN
	out := in;
ELSE
	out := buf[i];
	buf[i] := in;
	i := INC1(i, N);
END_IF;



(* revision history
hm 1.10.2006		rev 1.1
	corrected error in buffer management

hm 19.1.2007		rev 1.2
	changed reset to load the value of in instead of 0

hm	27. oct. 2008	rev 1.3
	improved performance

hm	23. feb.2009	rev 1.4
	corrected an index problem

hm	23. mar. 2009	rev 1.5
	corrected non standard write to input N

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    IN : REAL;
    N : INT;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : DELAY;
    semantist_out_OUT : REAL;
END_VAR
semantist_fb(IN := IN, N := N, RST := RST);
semantist_out_OUT := semantist_fb.OUT;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
