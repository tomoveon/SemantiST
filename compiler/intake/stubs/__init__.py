"""Environment stubs: a thin layer after the compatibility layer.

The compatibility layer never invents code: a name that neither the library,
its dependency closure, RuSTy nor a compatibility profile defines stays
``unresolved`` and blocks the POUs that use it. This layer gives such vendor
platform names a stub so that those POUs can be built and fuzzed, as an
explicitly approximate second tier that is reported apart from the core
results.

1. :mod:`.gaps` finds the missing names from the call sites (calls, types,
   global references; formal parameter names are not gaps) and infers a
   signature deterministically from the argument and result types.
2. :mod:`.docs` retrieves the passages of the collected vendor manuals that
   name the function, by exact name.
3. :mod:`.generate` asks an LLM (offline, cached) for a declaration and a
   category per name, given the call sites, the inferred signature and the
   passages; :mod:`.verify` compiles every library that needs the stubs with
   the pinned RuSTy and feeds the errors back, at most five rounds; a stub
   that still fails falls back to the inferred signature or is dropped.
4. The accepted stubs are frozen in ``library/<family>.json`` with their
   provenance; the intake reads only that file, so runs stay deterministic.
"""
