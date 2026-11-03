FUNCTION Array_size: DINT
	VAR_INPUT en: BOOL (*{order 1}*); END_VAR
	VAR_OUTPUT eno: BOOL (*{order 2}*); END_VAR
	VAR_IN_OUT self: ARRAY[*] OF LREAL (*{order 3}*); END_VAR
	VAR_INPUT scale: DINT (*{order 4}*); END_VAR
	Array_size := (UPPER_BOUND(self, 1) - LOWER_BOUND(self, 1) + 1) * scale;
	eno := TRUE;
END_FUNCTION

FUNCTION Twice: DINT
	VAR_INPUT en: BOOL (*{order 1}*); END_VAR
	VAR_OUTPUT eno: BOOL (*{order 2}*); END_VAR
	VAR_INPUT x: DINT (*{order 3}*); END_VAR
	IF en THEN Twice := 2 * x; ELSE Twice := -1; END_IF;
END_FUNCTION

FUNCTION main : DINT
VAR ENO__SYSMAC : BOOL; END_VAR
VAR
    a : ARRAY[0..4] OF LREAL;
END_VAR
printf('%d %d$N', Array_size(en := TRUE, eno => ENO__SYSMAC, self := a, scale := Twice(en := TRUE, eno => ENO__SYSMAC, x := 3)), Twice(en := TRUE, eno => ENO__SYSMAC, x := Twice(en := TRUE, eno => ENO__SYSMAC, x := 5)));
main := 0;
END_FUNCTION
