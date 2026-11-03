# OSCAT CODESYS compatibility evidence

Use this reference only for OSCAT/CODESYS intake and adapter review.

## Authoritative representation rules

- CODESYS `TIME` is an unsigned 32-bit millisecond value. Its maximum is
  `T#49D17H2M47S295MS` (`DWORD#16#FFFFFFFF`).
- CODESYS `DATE` and `DT` are unsigned 32-bit seconds since 1970-01-01;
  `TOD` is unsigned 32-bit milliseconds since midnight.
- CODESYS conversions to narrower integers discard high-order bytes. REAL to
  TIME converts through `UDINT` and uses milliseconds.
- Pinned RuSTy temporal values use signed 64-bit nanoseconds. Its IEC ST
  library exposes wide conversions such as `TIME_TO_LINT`, `DATE_TO_LINT`,
  `ULINT_TO_TIME`, `ULINT_TO_DATE`, and `ULINT_TO_DT`.

Primary CODESYS documentation:

- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_datatype_time.html>
- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_datatype_date_and_time_of_day.html>
- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_operator_time_to.html>
- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_operator_date_to.html>
- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_operator_convert_integer.html>
- <https://content.helpme-codesys.com/en/CODESYS%20Development%20System/_cds_operator_real_to.html>

## Reviewed mapping

The OSCAT adapters in `benchmarks/oscat_basic/source/stubs.st` divide RuSTy
nanoseconds by `1_000_000` for TIME/TOD-to-CODESYS values and by
`1_000_000_000` for DATE/DT-to-CODESYS values. Reverse conversions multiply by
the same factors before calling RuSTy wide temporal primitives. Narrowing is
performed only after changing units, preserving CODESYS low-bit truncation.

Treat `benchmarks/oscat_basic/source/oscat.st` as the already-compatible OSCAT
library. Prepare its audit workspace with `prepare_library.py --already-compatible
--reviewed-stubs benchmarks/oscat_basic/source/stubs.st`;
do not run it through conversion again during fuzzing. Reuse the same
`stubs.st` for every selected OSCAT POU. `STRING_TO_INT`, `INT_TO_STRING`,
numeric-to-REAL helpers, and temporal conversions are semantic adapters, not
external declarations or Harness functions.
