FUNCTION_BLOCK STORE_8
VAR_INPUT
	Set0 : BOOL;
	D0,D1, D2, D3, D4, D5, D6, D7 : BOOL;
	Clr : BOOL;
	Rst : BOOL;
END_VAR
VAR_OUTPUT
	Q0, Q1, Q2, Q3, Q4, Q5, Q6, Q7 : BOOL;
END_VAR
VAR
	edge: BOOL;
END_VAR

(*
version 1.2	14. mar. 2009
programmer 	hugo
tested by		oscat

Store_4 stores up to 4 boolean inputs until a reset is clearing the outputs.
the respective output is set with a true at the respective input and stays true until a reset is generated.
a set input is used to set all outputs true at the same time, independent of their inputs.
a clear input is used to reset only the lowest priority of the outputs one at a time.
the lowest priority is q3.

*)
(* @END_DECLARATION := '0' *)
IF rst OR set0 THEN
	q0 := NOT rst;
	q1 := q0;
	q2 := q0;
	q3 := q0;
	q4 := q0;
	q5 := q0;
	q6 := q0;
	q7 := q0;
ELSE
	IF D0 THEN Q0 := TRUE; END_IF;
	IF D1 THEN Q1 := TRUE; END_IF;
	IF D2 THEN Q2 := TRUE; END_IF;
	IF D3 THEN Q3 := TRUE; END_IF;
	IF D4 THEN Q4 := TRUE; END_IF;
	IF D5 THEN Q5 := TRUE; END_IF;
	IF D6 THEN Q6 := TRUE; END_IF;
	IF D7 THEN Q7 := TRUE; END_IF;

	IF clr AND NOT edge THEN
		IF q0 THEN q0 := FALSE;
		ELSIF q1 THEN q1 := FALSE;
		ELSIF q2 THEN q2 := FALSE;
		ELSIF q3 THEN q3 := FALSE;
		ELSIF q4 THEN q4 := FALSE;
		ELSIF q5 THEN q5 := FALSE;
		ELSIF q6 THEN q6 := FALSE;
		ELSE q7 := FALSE;
		END_IF;
	END_IF;
	edge := clr;
END_IF;


(* revision history
hm	25.12.2007	rev 1.0
	original version

hm	30. oct. 2008	rev 1.1
	optimized performance

hm	14. mar. 2009	rev 1.2
	removed double assignments

*)


END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    Set0 : BOOL;
    D0 : BOOL;
    D1 : BOOL;
    D2 : BOOL;
    D3 : BOOL;
    D4 : BOOL;
    D5 : BOOL;
    D6 : BOOL;
    D7 : BOOL;
    Clr : BOOL;
    Rst : BOOL;
END_VAR
VAR
    semantist_fb : STORE_8;
    semantist_out_Q0 : BOOL;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
    semantist_out_Q3 : BOOL;
    semantist_out_Q4 : BOOL;
    semantist_out_Q5 : BOOL;
    semantist_out_Q6 : BOOL;
    semantist_out_Q7 : BOOL;
END_VAR
semantist_fb(Set0 := Set0, D0 := D0, D1 := D1, D2 := D2, D3 := D3, D4 := D4, D5 := D5, D6 := D6, D7 := D7, Clr := Clr, Rst := Rst);
semantist_out_Q0 := semantist_fb.Q0;
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
semantist_out_Q3 := semantist_fb.Q3;
semantist_out_Q4 := semantist_fb.Q4;
semantist_out_Q5 := semantist_fb.Q5;
semantist_out_Q6 := semantist_fb.Q6;
semantist_out_Q7 := semantist_fb.Q7;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
