FUNCTION_BLOCK D10_RECIPE_SEQUENCER
VAR_INPUT
    Start : BOOL;
    Level : INT;
    RecipeCode : INT;
    Abort : BOOL;
END_VAR
VAR_OUTPUT
    Step : INT;
    Valve : BOOL;
    Heater : BOOL;
    Setpoint : INT;
END_VAR
VAR
    holdTimer : TON;
    recipe : INT;
    setpoints : ARRAY[0..9] OF INT := [20, 25, 30, 35, 40, 45, 50, 55, 60, 65];
    lastStart : BOOL;
END_VAR

IF Abort THEN
    Step := 0;
    Valve := FALSE;
    Heater := FALSE;
END_IF;
holdTimer(IN := Step = 3, PT := T#1s);
CASE Step OF
    0:
        IF Start AND NOT lastStart THEN
            Step := 1;
        END_IF;
    1:
        Valve := TRUE;
        IF Level >= 700 THEN
            Valve := FALSE;
            Step := 2;
        END_IF;
    2:
        IF RecipeCode >= 4700 AND RecipeCode <= 4719 AND (RecipeCode MOD 7) = 0 THEN
            recipe := RecipeCode - 4700;
            Step := 3;
        END_IF;
    3:
        Heater := TRUE;
        IF holdTimer.Q THEN
            Heater := FALSE;
            Step := 4;
        END_IF;
    4:
        (* defect: codes 4700..4719 admit recipe 0..19, the table has 10 entries *)
        Setpoint := setpoints[recipe];
        Step := 0;
END_CASE;
lastStart := Start;
END_FUNCTION_BLOCK
