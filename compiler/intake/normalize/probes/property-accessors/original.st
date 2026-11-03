FUNCTION_BLOCK FB_DRIVE
VAR
    _target : LREAL;
END_VAR

PROPERTY PUBLIC Target : LREAL

GET
VAR
END_VAR

Target := _target;
END_GET

SET
VAR
END_VAR

_target := Target * 2.0;
END_SET

END_PROPERTY

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
