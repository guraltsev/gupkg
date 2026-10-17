"""Make HTTPS verification accept chains that end at an intermediate in the system store.

The bundled CPython 3.12 builds certificate chains only up to a self-signed root,
so a site such as github.com fails with "unable to get issuer certificate" on a
Windows machine whose store holds the issuing intermediate but not the newest
root. CPython 3.13 and later accept a chain that ends at any trusted
certificate (``VERIFY_X509_PARTIAL_CHAIN``); this module gives older
interpreters the same behavior. Verification is never disabled.

Usage and API
-------------
Call ``enable_partial_chain_verification()`` once at process start; it is
idempotent and affects the default HTTPS context used by ``urllib``.

Implementation Approach
-----------------------
The interpreter's default HTTPS context factory is wrapped so every context it
creates carries the partial-chain flag in addition to its normal settings.
"""

from __future__ import annotations

import ssl


def enable_partial_chain_verification() -> None:
    """Add the partial-chain flag to every default HTTPS context created afterwards."""
    flag = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", None)
    factory = ssl._create_default_https_context
    if flag is None or getattr(factory, "_gupkg_partial_chain", False):
        return

    def create_context(*args, **kwargs) -> ssl.SSLContext:
        """Create the usual default context with partial-chain verification enabled."""
        context = factory(*args, **kwargs)
        context.verify_flags |= flag
        return context

    create_context._gupkg_partial_chain = True  # type: ignore[attr-defined]
    ssl._create_default_https_context = create_context
