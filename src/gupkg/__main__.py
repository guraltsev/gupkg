"""Run the installed gupkg command dispatcher."""

# `python -m gupkg` takes the same path as gupkg.cmd: the bootstrap checks that the
# run is on the isolated bundled interpreter (and, when elevated, admin-only
# folders) before it imports any other gupkg code. Developers use scripts\run.cmd.
from .bootstrap import main

raise SystemExit(main())
