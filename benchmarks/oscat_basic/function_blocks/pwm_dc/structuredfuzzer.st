FUNCTION_BLOCK PWM_DC
VAR_INPUT
	F : REAL;
	DC : REAL;
END_VAR
VAR_OUTPUT
	Q : BOOL;
END_VAR
VAR
	clk: CLK_PRG;
	pulse: TP_X;
	tmp: REAL;
END_VAR

(*
version 1.4	11. mar. 2009
programmer 	oscat
tested BY		oscat

this signal generator generates a square wave signal which is specified by the frequency and the duty cycle
*)
(* @END_DECLARATION := '0' *)
IF F > 0.0 THEN
	tmp := 1000.0 / F;
	CLK(PT := REAL_TO_TIME(tmp));
	Pulse(in := clk.Q, pt := REAL_TO_TIME(tmp * DC));
	Q := pulse.Q;
END_IF;

(* revision history

hm	25. feb 2007	rev 1.1
	recoded in st for better performance and better portability

hm	27. nov 2007	rev 1.2
	avoid divide by 0 when F = 0

hm	19. oct. 2008	rev 1.3
	changed type TP_R to TP_X because of name change
	improved performance

hm	11. mar. 2009	rev 1.4
	changed real constants to use dot syntax

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    F : REAL;
    DC : REAL;
END_VAR
VAR
    semantist_fb : PWM_DC;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(F := F, DC := DC);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
