FUNCTION D03_SPAN_SCALE : DINT
VAR_INPUT
    RAW : INT;
    LO : INT;
    HI : INT;
END_VAR
VAR
    span : DINT;
END_VAR

D03_SPAN_SCALE := 0;
span := INT_TO_DINT(HI) - INT_TO_DINT(LO);
(* defect: the guard should be span > 7; a span of exactly 7 divides by zero *)
IF span > 0 AND RAW > LO THEN
    D03_SPAN_SCALE := (INT_TO_DINT(RAW) - INT_TO_DINT(LO)) * 1000 / (span - 7);
END_IF;
END_FUNCTION
