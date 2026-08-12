FUNCTION CONTROL_FLOW : DINT
VAR_INPUT
    x : DINT;
END_VAR
VAR
    i : DINT;
    y : DINT;
    values : ARRAY[0..3] OF DINT;
END_VAR

y := 0;
IF x < 0 THEN
    y := -x;
ELSIF x = 0 THEN
    IF x <= 0 THEN
        y := 1;
    ELSE
        y := 2;
    END_IF;
ELSE
    y := x;
END_IF;

CASE x OF
    0:
        y := y + 1;
    1, 2:
        y := y + 2;
    3..5:
        y := y + 3;
ELSE
    y := y + 4;
END_CASE;

WHILE y < 8 DO
    y := y + 1;
END_WHILE;

REPEAT
    y := y - 1;
UNTIL y <= 4
END_REPEAT;

FOR i := 0 TO 3 DO
    y := y + values[i];
END_FOR;

CONTROL_FLOW := y / x;
END_FUNCTION

FUNCTION EMPTY_BRANCH : DINT
VAR_INPUT
    x : DINT;
END_VAR

EMPTY_BRANCH := 0;
IF x > 0 THEN
ELSE
    EMPTY_BRANCH := 1;
END_IF;
END_FUNCTION

FUNCTION EARLY_RETURN : DINT
VAR_INPUT
    x : DINT;
END_VAR

IF x < 0 THEN
    EARLY_RETURN := -1;
    RETURN;
END_IF;

EARLY_RETURN := x + 1;
END_FUNCTION

FUNCTION TRANSITIVE_DEPENDENCY : BOOL
VAR_INPUT
    input_value : DINT;
    enabled : BOOL;
END_VAR
VAR
    normalized : DINT;
END_VAR

normalized := input_value + 1;
IF enabled AND normalized > 10 THEN
    TRANSITIVE_DEPENDENCY := TRUE;
ELSE
    TRANSITIVE_DEPENDENCY := FALSE;
END_IF;
END_FUNCTION

FUNCTION_BLOCK STATEFUL_COUNTER
VAR_INPUT
    enable : BOOL;
    reset : BOOL;
END_VAR
VAR_INPUT (* CONSTANT *)
    limit : DINT := 100;
END_VAR
VAR_OUTPUT
    out_value : DINT;
END_VAR
VAR
    state : DINT;
END_VAR

IF reset THEN
    state := 0;
ELSIF enable THEN
    state := state + 1;
END_IF;
IF state >= limit THEN
    RETURN;
END_IF;
out_value := state;
END_FUNCTION_BLOCK
