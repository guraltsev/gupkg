"""Discover packages and registries and derive package state (reads disk, never mutates).

``gupkg.catalog.catalog`` is the one place where state is derived, ``gupkg.catalog.local``
scans roots and package roots, ``gupkg.catalog.registry`` synchronises and reads registry
caches, and ``gupkg.catalog.record`` reads and writes the install record.
"""
