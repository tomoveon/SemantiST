(* Timer boundary probe, compiled both by CODESYS (as the body of PLC_PRG)
   and by RuSTy (as this function block).
   One active scan increments n; each scenario is a range of n. *)
FUNCTION_BLOCK TIMER_PROBE
VAR_INPUT
    bRun : BOOL;
END_VAR
VAR_OUTPUT
    n : UDINT;
    scenario : INT;
    inX : BOOL;
    q0 : BOOL;
    et0 : TIME;
    q50 : BOOL;
    et50 : TIME;
    raw50 : TIME;
    qOf0 : BOOL;
    qOf50 : BOOL;
    etOf50 : TIME;
    qTp : BOOL;
    etTp : TIME;
    qTrig : BOOL;
END_VAR
VAR
    ton0 : TON;
    ton50 : TON;
    tof0 : TOF;
    tof50 : TOF;
    tp50 : TP;
    trig : R_TRIG;
    k : UDINT;
    tNow : TIME;
    tStart : TIME;
    lastX : BOOL;
END_VAR

IF NOT bRun OR n >= 1500 THEN
    RETURN;
END_IF;

tNow := TIME();
inX := FALSE;
scenario := 0;
IF n < 20 THEN
    (* S1: TON with PT = 0, IN rises at n = 5 *)
    scenario := 1;
    inX := n >= 5;
ELSIF n >= 100 AND n < 1100 THEN
    (* S2: TON with PT = 50 ms, 50 repetitions of 10 low and 10 high scans *)
    scenario := 2;
    k := (n - 100) MOD 20;
    inX := k >= 10;
ELSIF n >= 1200 AND n < 1260 THEN
    (* S3: TON with PT = 50 ms, IN held TRUE long after expiry *)
    scenario := 3;
    inX := n >= 1210 AND n < 1250;
ELSIF n >= 1300 AND n < 1340 THEN
    (* S4: TOF with PT = 0 and PT = 50 ms, IN falls at n = 1310 *)
    scenario := 4;
    inX := n < 1310;
ELSIF n >= 1400 AND n < 1440 THEN
    (* S5: TP with PT = 50 ms, retriggered during the pulse at n = 1403 *)
    scenario := 5;
    inX := (n >= 1400 AND n < 1402) OR n = 1403;
END_IF;

(* elapsed time since the last rising edge of inX, measured by this program *)
IF inX AND NOT lastX THEN
    tStart := tNow;
END_IF;
IF inX THEN
    raw50 := tNow - tStart;
ELSE
    raw50 := T#0s;
END_IF;
lastX := inX;

ton0(IN := scenario = 1 AND inX, PT := T#0s);
ton50(IN := (scenario = 2 OR scenario = 3) AND inX, PT := T#50ms);
tof0(IN := scenario = 4 AND inX, PT := T#0s);
tof50(IN := scenario = 4 AND inX, PT := T#50ms);
tp50(IN := scenario = 5 AND inX, PT := T#50ms);
(* S0: R_TRIG whose CLK is TRUE on the very first active scan *)
trig(CLK := n = 0 OR (scenario = 1 AND inX));

q0 := ton0.Q;
et0 := ton0.ET;
q50 := ton50.Q;
et50 := ton50.ET;
qOf0 := tof0.Q;
qOf50 := tof50.Q;
etOf50 := tof50.ET;
qTp := tp50.Q;
etTp := tp50.ET;
qTrig := trig.Q;
n := n + 1;
END_FUNCTION_BLOCK
