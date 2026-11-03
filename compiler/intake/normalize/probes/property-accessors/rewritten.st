FUNCTION_BLOCK FB_DRIVE
VAR
    _target : LREAL;
END_VAR

(* PROPERTY PUBLIC Target : LREAL *)

PROPERTY_GET Target : LREAL
VAR
END_VAR

Target := _target;
END_PROPERTY

PROPERTY_SET Target : LREAL
VAR
END_VAR

_target := Target * 2.0;
END_PROPERTY

(* END_PROPERTY *)

_target := _target;
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    d : FB_DRIVE;
END_VAR
d.Target := 21.0;
printf('%d$N', LREAL_TO_DINT(d.Target));
main := 0;
END_FUNCTION
