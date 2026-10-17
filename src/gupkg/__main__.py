"""Run the installed gupkg command dispatcher."""

import sys
from pathlib import Path

from .elevation import UntrustedLocationError, elevated_mode, enforce_elevated_runtime

# An elevated `python -m gupkg` must prove it is the isolated bundled runtime
# before running anything; the gupkg.cmd launcher guarantees that for itself.
if elevated_mode():
    try:
        enforce_elevated_runtime(Path(__file__).resolve().parent)
    except UntrustedLocationError as error:
        print(f"[gupkg] {error}", file=sys.stderr)
        raise SystemExit(1)

from .cli import main  # noqa: E402

raise SystemExit(main())
