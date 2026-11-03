FUNCTION main : DINT
VAR
    days : ARRAY[1..4] OF INT := 31, 28, 31, 30;
    zeros : ARRAY[0..3] OF INT := 2(7), 1, 2;
END_VAR
printf('%d %d %d %d$N', days[2], days[4], zeros[1], zeros[3]);
main := 0;
END_FUNCTION
