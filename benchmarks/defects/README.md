# Defect regression suite

`manifest.json` defines 16 cases: six sourced targets (one OSCAT and five
ICSQuartz-derived) and ten designed cases. The sourced targets are listed by
their ST file and compatibility manifest. Each designed case has
`buggy/target.st` and `fixed/target.st` with the same POU name; verified cases
may also include `witness.txt`. Keep the manifest's trigger descriptions and
regression tests consistent with any source changes.
