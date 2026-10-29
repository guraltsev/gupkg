"""Build the command line and environment under which gupkg runs ``git``.

A repository named in a manifest is untrusted input, and an elevated gupkg must not be steered
by the user's Git configuration or environment (decision D19). Every Git command therefore runs
with an environment that holds only machine-level variables (``safe_process_environment``) plus
fixed settings, with the user and system configuration files pointed at the null device, hooks
and credential helpers off, prompts off, and only network transports allowed. In an elevated run
``git`` is not looked up through the inherited ``PATH`` either: it is the ``git.exe`` of the first
folder on ``PATH`` that only administrators can change, started by its full path, and the child
sees only such folders on its own ``PATH``.

Usage and API
-------------
``git_command(*arguments, system=..., mode=...)`` returns the argument list to pass to ``System.run`` and
``git_environment(system, mode)`` the environment mapping; both take the machine boundary and the run's ``Mode`` so a
system-mode run can judge the folders (the download rule's modules import no settings, so they take the ``Mode``, not the ``Scope``). ``GitUnavailable`` reports an elevated run that finds no trusted ``git``.

Implementation Approach
-----------------------
Nothing is inherited by accident: the environment is built from an allow-list, so any
``GIT_*`` variable the user set (``GIT_SSH_COMMAND``, ``GIT_EXEC_PATH``, ``GIT_DIR``,
``GIT_TEMPLATE_DIR``...) never reaches Git. Settings that the command line can pin are pinned
with ``-c`` as well, so a configuration file found in a repository cannot enable a hook or
helper.
"""

from __future__ import annotations

from pathlib import Path

from ..system.port import Mode, System, safe_process_environment, trusted_path_entries

_NULL_DEVICE = "NUL"
_SETTINGS = (
    "core.hooksPath=" + _NULL_DEVICE,
    "core.fsmonitor=false",
    "core.sshCommand=ssh",
    "credential.helper=",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
)


class GitUnavailable(RuntimeError):
    """Report that an elevated run found no ``git.exe`` in a folder that only administrators can change."""


def git_executable(system: System, mode: Mode) -> str:
    """Return what to run as ``git``: the bare name in user mode, a trusted full path in system mode.

    Raises
    ------
    GitUnavailable
        If the run is in system mode and no admin-only folder of ``PATH`` holds ``git.exe``.
    """
    if mode is not Mode.SYSTEM:
        return "git"
    for folder in trusted_path_entries(system):
        candidate = Path(folder) / "git.exe"
        if candidate.is_file():
            return str(candidate)
    raise GitUnavailable(
        "An elevated run starts git only from a folder that only administrators can change, and no such folder on PATH holds git.exe."
    )


def git_command(*arguments: str, system: System, mode: Mode) -> list[str]:
    """Return ``git`` (resolved for a system-mode run) with the safety settings, then *arguments*."""
    command = [git_executable(system, mode)]
    for setting in _SETTINGS:
        command += ["-c", setting]
    return [*command, *arguments]


def git_environment(system: System, mode: Mode, temp: Path | None = None) -> dict[str, str]:
    """Return the scrubbed environment for a Git process.

    An elevated run sees only admin-only folders on ``PATH``, values for the Windows folder, ``COMSPEC`` and ``PATHEXT`` that
    do not come from the process environment, and the temporary folder *temp* (the work folder, which is admin-only
    there) as ``TEMP`` and ``TMP`` when it is given.
    """
    environment = safe_process_environment({
        "GIT_CONFIG_GLOBAL": _NULL_DEVICE,
        "GIT_CONFIG_SYSTEM": _NULL_DEVICE,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:http:ssh:git",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        **({"TEMP": str(temp), "TMP": str(temp)} if temp is not None else {}),
    }, system=system, mode=mode)
    return environment
