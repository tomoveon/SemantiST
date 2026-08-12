# Compatibility layer

The library intake stage selects reusable profiles from `profiles/` and writes
one `compatibility.json` into the derived library workspace. RuSTy compilation,
native-link validation, and the production fuzz build all consume that same
manifest. Library-specific declarations and semantic adapters remain beside the
normalized library; the Harness never supplies vendor or standard functions.

Profiles are opt-in because vendor library names and ABIs can overlap. A profile
must provide typed ST interfaces and deterministic native implementations. It
must not turn an unsupported call into a synthetic crash.
