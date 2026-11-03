TYPE POINT2 :
  STRUCT
    X : INT;
    Y : INT;
  END_STRUCT
END_TYPE

FUNCTION main : DINT
VAR
    p : POINT2;
END_VAR
p.X := 3; p.Y := 4;
printf('%d$N', p.X + p.Y);
main := 0;
END_FUNCTION
