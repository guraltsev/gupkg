"""The registry tool: keep a gupkg registry's pinned versions current (decision D36, design 17).

This is a maintainer tool, separate from gupkg core and not shipped in the release bundle: it
runs on the machine of the registry's maintainer (or in its scheduled job) and may import core
(decision D44). It processes untrusted network data, so it fails closed everywhere.

Usage and API
-------------
``python -m gupkg_registry update [PACKAGE ...] --registry PATH`` runs the trackers and writes new
``<name>/<version>/`` directories with their ``sha256`` and fingerprint. ``pin NAME/VERSION``
gives an existing version its first hash. ``validate`` checks a whole registry (what CI runs).
See ``gupkg_registry.cli`` for the options and ``gupkg_registry.update`` for the steps.
"""
