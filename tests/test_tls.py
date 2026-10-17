"""Cover the HTTPS verification policy applied to gupkg's network requests.

Behavior covered: after start-up the default HTTPS context accepts chains that
end at a trusted intermediate while still verifying certificates and host names.
Real: the standard library's SSL contexts. Out of scope: contacting any server.
"""

from __future__ import annotations

import ssl

from gupkg.tls import enable_partial_chain_verification


def test_default_https_context_verifies_and_accepts_partial_chains() -> None:
    """Requests keep full verification but accept chains ending at a trusted intermediate."""
    enable_partial_chain_verification()
    enable_partial_chain_verification()

    context = ssl._create_default_https_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
