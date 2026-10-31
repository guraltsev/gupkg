# Conditional tests: the old registry layout

until: No registry that gupkg still supports carries the former package launcher `gupkg-tui.cmd` (now `manage.cmd`). That is
true when `RETIRED_LAUNCHER_NAMES` in `src/gupkg_registry/layout.py` is removed because the published registry has been
converted and no older checkout is supported.

expires: 2027-04-30

purpose: Registries made before the rename carry `gupkg-tui.cmd` in every version directory. The registry tool must convert
such a registry (remove the old file, write `manage.cmd`, refresh the fingerprint), and the validator must refuse a registry
that still has the old name. The behaviour exists only for old-layout registries, so the tests end with them. Until then they
are held to every rule of [`_docs/tests.md`](../../../_docs/tests.md).
