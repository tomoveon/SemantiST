FUNCTION SEMANTIC_HAZARDS : DINT
VAR_INPUT
    a : DINT;
    b : DINT;
    index : DINT;
END_VAR
VAR
    sum : DINT;
    values : ARRAY[0..3] OF DINT;
END_VAR

sum := a + b;
SEMANTIC_HAZARDS := (sum / b) + values[index];
END_FUNCTION
