"""Build the command line and environment under which gupkg runs ``git``.

A repository named in a manifest is untrusted input, and an elevated gupkg must not be steered
by the user's Git configuration or environment (decision D19). Every Git command therefore runs
with an environment that holds only machine-level variables (``safe_process_environment``) plus
fixed settings, with the user and system configuration files pointed at the null device, hooks
and credential helpers off, prompts off, and only network transports allowed.

Usage and API
-------------
``git_command(*arguments)`` returns the argument list to pass to ``System.run``;
``git_environment()`` returns the environment mapping.

Implementation Approach
-----------------------
Nothing is inherited by accident: the environment is built from an allow-list, so any
``GIT_*`` variable the user set (``GIT_SSH_COMMAND``, ``GIT_EXEC_PATH``, ``GIT_DIR``,
``GIT_TEMPLATE_DIR``...) never reaches Git. Settings that the command line can pin are pinned
with ``-c`` as well, so a configuration file found in a repository cannot enable a hook or
helper.
"""

from __future__ import annotations

from ..system.port import safe_process_environment

_NULL_DEVICE = "NUL"
_SETTINGS = (
    "core.hooksPath=" + _NULL_DEVICE,
    "core.fsmonitor=false",
    "core.sshCommand=ssh",
    "credential.helper=",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
)


def git_command(*arguments: str) -> list[str]:
    """Return ``git`` with the safety settings, then *arguments*."""
    command = ["git"]
    for setting in _SETTINGS:
        command += ["-c", setting]
    return [*command, *arguments]


def git_environment() -> dict[str, str]:
    """Return the scrubbed environment for a Git process."""
    return safe_process_environment({
        "GIT_CONFIG_GLOBAL": _NULL_DEVICE,
        "GIT_CONFIG_SYSTEM": _NULL_DEVICE,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:http:ssh:git",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    })
