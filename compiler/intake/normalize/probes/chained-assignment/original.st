FUNCTION_BLOCK CLICK
VAR_INPUT
    IN : BOOL;
    LEVEL : INT;
END_VAR
VAR_OUTPUT
    Q0, Q1, Q2 : BOOL;
    WIDE : DINT;
    NARROW : SINT;
END_VAR
IF NOT IN THEN Q0 := Q1 := Q2 := FALSE; ELSE Q0 := Q1 := Q2 := TRUE; END_IF;
WIDE := NARROW := LEVEL;
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    c : CLICK;
END_VAR
c(IN := TRUE, LEVEL := 300);
printf('%d %d %d %d %d$N', c.Q0, c.Q1, c.Q2, c.WIDE, c.NARROW);
c(IN := FALSE, LEVEL := -5);
printf('%d %d %d %d %d$N', c.Q0, c.Q1, c.Q2, c.WIDE, c.NARROW);
main := 0;
END_FUNCTION
