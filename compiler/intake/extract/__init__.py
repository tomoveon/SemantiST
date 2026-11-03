"""Container-format to ST extraction (layer 1 of the compatibility layer).

The ``whole_*`` modules are the extractors that produced the collected
``rusty_compatibility/*/.st文件`` sources, moved here from
``rusty_compatibility/提取脚本`` with host paths turned into driver
configuration. :mod:`.cli` runs them, applies the recorded curation, adds the
fixed inputs that cannot be regenerated on Linux, writes POU-level provenance
and compares the result with the collected sources.
"""
