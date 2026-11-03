FUNCTION D01_MAGIC_INDEX : INT
VAR_INPUT
    X : DINT;
    Y : DINT;
    IDX : INT;
END_VAR
VAR
    table : ARRAY[0..3] OF INT := [10, 20, 30, 40];
END_VAR

D01_MAGIC_INDEX := 0;
IF X = 12345 AND Y = 3 * X - 7 THEN
    IF IDX >= 0 THEN
        (* defect: the upper bound IDX <= 3 is missing *)
        D01_MAGIC_INDEX := table[IDX];
    END_IF;
END_IF;
END_FUNCTION
