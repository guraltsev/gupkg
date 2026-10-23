"""Turn a downloaded artifact into a prepared ``App``: unpack, then run ``[[prepare]]``.

This is the part of the built-in download script that issue 026 owns. Downloading and
verifying the artifact (issue 027) and activating the result (issue 028) happen around it:
the caller passes the verified file and a destination that does not exist yet, and gets
back either a complete ``App`` or nothing. A custom populate script (decision D27) does its
own work but can call ``prepare_app`` to run the package's declarative steps on its result.

Usage and API
-------------
``populate_app(system, manifest, artifact, destination, context=...)`` unpacks the artifact
of a pinned version's ``[source]`` and runs its ``[[prepare]]`` steps. ``populate_git`` does
the same for a ``git`` source. ``prepare_app(manifest, app, context)`` runs only the steps.
``run_custom_populate(context, script)`` runs a version's own populate script (decision D27): a
Python hook gets the context object, any other script gets it as JSON on standard input.

Implementation Approach
-----------------------
Unpacking is atomic (``gupkg.services.unpack``). The prepare steps then run on the
finished folder, and if one fails the folder is deleted again, so the destination exists
only when every step succeeded.
"""

from __future__ import annotations

from pathlib import Path

from ..model.manifest import Manifest
from ..report.events import subprocess_lines
from ..system.port import System
from .hooks import HookContext, HookError, call_populate, load_hook
from .prepare import run_prepare
from .safepath import UnsafePath, confine, remove_tree
from .unpack import Limits, UnpackReport, plan_from_source, unpack_artifact, unpack_git


def populate_app(
    system: System,
    manifest: Manifest,
    artifact: Path,
    destination: Path,
    *,
    context: HookContext | None = None,
    filename: str | None = None,
    seven_zip: Path | None = None,
    limits: Limits = Limits(),
) -> UnpackReport:
    """Unpack *artifact* as ``manifest.source`` describes and run ``manifest.prepare``.

    Parameters
    ----------
    system : System
        The boundary that runs 7-Zip.
    manifest : Manifest
        A pinned version's manifest (no templates left).
    artifact : Path
        The verified download.
    destination : Path
        The ``App`` folder to create; it must not exist.
    context : HookContext, optional
        Needed only when a ``[[prepare]]`` step is a ``module``.
    filename : str, optional
        The published file name of the artifact.
    seven_zip : Path, optional
        The 7-Zip executable.
    limits : Limits
        Bounds on the unpacked size.

    Returns
    -------
    UnpackReport
        What the unpack produced.

    Raises
    ------
    UnpackError
        If the unpack fails.
    PrepareError
        If a prepare step fails; the destination is removed.
    """
    report = unpack_artifact(
        system, artifact, destination, plan_from_source(manifest.source),
        filename=filename, seven_zip=seven_zip, limits=limits,
    )
    prepare_app(manifest, destination, context)
    return report


def populate_git(
    system: System, manifest: Manifest, destination: Path, *,
    context: HookContext | None = None, limits: Limits = Limits(),
) -> UnpackReport:
    """Check out a ``git`` source into *destination* and run ``manifest.prepare``.

    Raises
    ------
    UnpackError
        If ``git`` fails or the pinned commit is not what the repository serves.
    PrepareError
        If a prepare step fails; the destination is removed.
    """
    source = manifest.source
    git = source.git
    report = unpack_git(
        system, source.download_url or "", git.ref if git else None, git.commit if git else None,
        destination, plan_from_source(source), limits=limits,
    )
    prepare_app(manifest, destination, context)
    return report


def run_custom_populate(context: HookContext, script: str, timeout: float | None = 3600) -> None:
    """Run the populate script *script* of a version directory with *context*.

    A ``.py`` script is a hook module whose ``populate(ctx)`` receives the context object. Any
    other script (``.ps1``, ``.cmd``, ``.bat`` or an executable) is started through
    ``System.run`` in the version directory and receives ``context.to_json()`` on standard
    input; its output lines are reported as events. Either way the script fills
    ``context.paths.app`` itself, and may use the shared HTTP client (decision D28).

    Parameters
    ----------
    context : HookContext
        The context whose ``paths.version_root`` holds the script.
    script : str
        The manifest's ``[source] script``, a path below ``pkg.local``.
    timeout : float, optional
        Seconds a non-Python script may run.

    Raises
    ------
    HookError
        If the script is missing, outside the version directory or behind a link, or exits
        with a non-zero status.
    """
    root = context.paths.version_root
    if script.lower().endswith(".py"):
        call_populate(load_hook(root, script), context)
        return
    try:
        path = confine(root, script)
    except UnsafePath as error:
        raise HookError(f"{script}: {error}") from error
    if not path.is_file():
        raise HookError(f"{script} does not exist")
    command = [str(path)]
    result = context.system.run(
        command, cwd=root, input_text=context.to_json(), on_line=subprocess_lines(command), timeout=timeout
    )
    if result.returncode != 0:
        raise HookError(f"{script} exited with status {result.returncode}")


def prepare_app(manifest: Manifest, app: Path, context: HookContext | None = None) -> None:
    """Run the ``[[prepare]]`` steps of *manifest* on *app*, deleting *app* if a step fails."""
    try:
        run_prepare(manifest.prepare, app, context)
    except Exception:
        remove_tree(app)
        raise
