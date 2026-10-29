"""Build the ``Scope`` that service-level tests pass where the code asks for the run's scope.

``scope_of(system)`` returns the scope of a process on *system*: its mode is the mode of the token (``gupkg.scope.process_mode``),
its root is a folder name that nothing reads, and its settings are the built-in defaults with ``integrate = "none"`` (no Start Menu or ``PATH`` folder to expand). The scope carries *system*, so
``Scope.require_trusted`` judges folders through the same ``FakeSystem(untrusted_dirs=...)`` the test configured.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.model.scope import Scope, make_scope
from gupkg.model.settings import LAYER_APP, Layer, default_layer, merge_layers
from gupkg.scope import process_mode
from gupkg.system.port import Mode


def scope_of(system, root: Path | str = "scope-root", *, mode: Mode | None = None) -> Scope:
    """Return the scope of a process on *system*; *mode* overrides the mode of the token (rarely needed)."""
    chosen = mode if mode is not None else process_mode(system)
    layer = Layer(LAYER_APP, {"schema": 4, "integration": {"integrate": "none"}})
    return make_scope(chosen, Path(root), "test", merge_layers([default_layer(chosen.value), layer]), {}, system=system)
