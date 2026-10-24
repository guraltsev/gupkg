"""Run a vendor installer into a staging folder (design 8.4, decision D39).

A version whose ``[source.installer]`` table names ``run`` arguments is populated by running the downloaded installer
(GIMP's Inno Setup 7 installer is the one current user) with ``$Stage`` standing for the staged ``App`` folder. This is
vendor code: it runs with the rights of the gupkg that runs it, and gupkg cannot see anything it changes outside the
staging folder (registry keys, files in other places). That is why the step is explicit and labelled, and why it is
guarded harder than anything else the populate step does.

Usage and API
-------------
``expand_arguments(run, stage)`` expands ``$Stage`` and refuses arguments that point anywhere but into the stage.
``run_installer(engine, fetched, run, stage, work, require, name, version)`` runs the installer; it returns when the installer
exited with status 0 and the staged ``App`` passed the checks, and raises ``InstallerError`` (or ``ProcessNotAllowed``
under a sandbox that has no double for it) otherwise.

Implementation Approach
-----------------------
Only a file that was downloaded, verified under the checksum rules and hashed again is run (``reverify``). It is
copied to the work folder under its published name, hashed once more there, and judged admin-only in an elevated run
(the cache and the work folder must both be admin-only). The child gets a minimal environment (machine-level variables
only, ``TEMP`` inside the work folder), no standard input, a hard timeout and its output as events. A
destination argument such as ``/DIR=`` must lie inside the stage: an absolute path elsewhere, a ``..`` segment, a UNC
path or a relative path with separators is refused before anything starts. After the run, ``require`` is judged,
and the staged folder must hold no link or junction (an installer could point one outside). A failure removes the whole
staging folder (the caller does), so nothing is left and nothing is activated.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..catalog.local import canonical_path, is_inside, is_reparse_point
from ..report.events import Step, Warning, emit, subprocess_lines
from ..services.artifact import FetchedArtifact, reverify
from ..services.unpack.transform import require_paths
from ..services.values import file_name_from_url
from ..system.port import safe_process_environment
from .context import Engine, require_trusted_tools

_VARIABLE = re.compile(r"\$(?:\$|\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
_OPTION = re.compile(r"^(?P<flag>[/-]{1,2}[A-Za-z][\w-]*)(?P<sep>[=:])(?P<value>.*)$", re.DOTALL)
_DRIVE = re.compile(r"^[A-Za-z]:")


class InstallerError(RuntimeError):
    """Report that a vendor installer was refused, failed, timed out or left an unacceptable ``App``."""


def expand_arguments(run: Sequence[str], stage: Path) -> list[str]:
    """Return *run* with ``$Stage`` expanded, after refusing every argument that points outside *stage*.

    Parameters
    ----------
    run : sequence of str
        The ``[source.installer] run`` arguments.
    stage : Path
        The staging folder the installer must install into.

    Returns
    -------
    list of str
        The arguments to start the installer with.

    Raises
    ------
    InstallerError
        If an argument uses an unknown variable, holds a control character, or names a path that is not inside *stage*
        (an absolute path elsewhere, a UNC path, a ``..`` segment, or a relative path with a separator).
    """
    expanded: list[str] = []
    for text in run:
        def replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("plain")
            if name is None:
                return "$"
            if name != "Stage":
                raise InstallerError(f"installer argument {text!r} uses ${name}; only $Stage exists there")
            return str(stage)

        value = _VARIABLE.sub(replace, text)
        _require_inside(text, value, stage)
        expanded.append(value)
    return expanded


def _require_inside(original: str, value: str, stage: Path) -> None:
    """Refuse an expanded argument whose path part leaves *stage* (or has a control character)."""
    if any(ord(char) < 32 for char in value):
        raise InstallerError(f"installer argument {original!r} holds a control character")
    option = _OPTION.match(value)
    target = (option.group("value") if option else value).strip().strip('"')
    if not target:
        return
    segments = [part for part in re.split(r"[\\/]+", target) if part]
    if ".." in segments:
        raise InstallerError(f"installer argument {original!r} has a '..' segment")
    if _DRIVE.match(target) or target.startswith(("\\\\", "//")):
        # An absolute path: only a drive path inside the stage is accepted (a UNC path never is).
        inside = canonical_path(target) == canonical_path(stage) or is_inside(Path(target), stage)
        if target.startswith(("\\\\", "//")) or not inside:
            raise InstallerError(f"installer argument {original!r} names {target!r}, which is not inside the stage {stage}")
    elif len(segments) > 1:
        raise InstallerError(f"installer argument {original!r} names the path {target!r}; only the stage may be named")


def run_installer(
    engine: Engine,
    fetched: FetchedArtifact,
    run: Sequence[str],
    stage: Path,
    work: Path,
    require: Sequence[str],
    name: str,
    version: str,
) -> list[str]:
    """Run the verified installer *fetched* into *stage* and validate the result; return the arguments it ran with.

    Raises
    ------
    InstallerError
        If the file is not an ``.exe``, changed after verification, an argument leaves the stage, the installer fails
        or times out, or the staged ``App`` holds a link.
    ChecksumMismatch
        If the cached file changed after it was verified.
    UnpackError
        If a ``require`` path is missing after the installer ran.
    gupkg.selfmgmt.elevation.UntrustedLocationError
        In an elevated run, if the cache or the work folder is not admin-only.
    gupkg.system.port.ProcessNotAllowed
        If a sandbox has no double for the installer (the package needs a real run).
    """
    arguments = expand_arguments(run, stage)
    filename = fetched.download.filename or file_name_from_url(fetched.download.final_url) or fetched.download.path.name
    if not filename.lower().endswith(".exe"):
        raise InstallerError(f"{name} {version}: the artifact {filename!r} is not an .exe, which is all a vendor installer step runs")

    # Only the verified bytes run, from admin-only places in an elevated run.
    reverify(fetched)
    folder = work / "installer"
    folder.mkdir()
    installer = folder / ntpath.basename(filename)
    require_trusted_tools(engine, installer, fetched.download.path)
    shutil.copyfile(fetched.download.path, installer)
    if _sha256(installer) != (fetched.record.observed_sha256 or ""):
        raise InstallerError(f"{filename} changed between the verification and the start of the installer")
    temp = work / "temp"
    temp.mkdir()
    stage.mkdir(exist_ok=True)

    command = [str(installer), *arguments]
    emit(Step(f"runs vendor installer: {filename} {' '.join(arguments)}", "info"))
    emit(Warning(
        f"{name} {version} runs a vendor installer. Anything it changes outside {stage} is outside gupkg's control "
        "and is not undone by uninstall."
    ))
    try:
        result = engine.system.run(
            command, cwd=folder, env=safe_process_environment({"TEMP": str(temp), "TMP": str(temp)}),
            on_line=subprocess_lines(command), timeout=engine.installer_timeout,
        )
    except TimeoutError as error:
        raise InstallerError(f"The vendor installer of {name} {version} did not finish within {engine.installer_timeout:g} seconds") from error
    if result.returncode != 0:
        raise InstallerError(f"The vendor installer of {name} {version} exited with status {result.returncode}")

    # The result is judged after the installer ran: it must hold what the package requires and no link.
    if not stage.is_dir():
        raise InstallerError(f"The vendor installer of {name} {version} did not create {stage}")
    _refuse_links(stage)
    if require:
        require_paths(stage, tuple(require))
    return arguments


def _sha256(path: Path) -> str:
    """Return the SHA-256 of a file as lower-case hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _refuse_links(stage: Path) -> None:
    """Refuse a staged ``App`` that holds a link or junction (the installer could have pointed one anywhere)."""
    for folder, directories, files in os.walk(stage):
        for entry in (*directories, *files):
            path = Path(folder) / entry
            if is_reparse_point(path):
                raise InstallerError(f"The vendor installer left a link at {path}; an App with links is not accepted")
