"""Run the installed gupkg command dispatcher."""

import sys
from pathlib import Path

from .elevation import UntrustedLocationError, elevated_mode, enforce_bundled_runtime

# `python -m gupkg` runs only on the isolated bundled interpreter, like the
# gupkg.cmd launcher; developers use scripts\run.cmd instead.
try:
    enforce_bundled_runtime(Path(__file__).resolve().parent, elevated=elevated_mode())
except UntrustedLocationError as error:
    print(f"[gupkg] {error}", file=sys.stderr)
    raise SystemExit(1)

from .cli import main  # noqa: E402

raise SystemExit(main())
