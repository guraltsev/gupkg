"""Describe the result of one gupkg command and format it for people or scripts.

Every front end (command line, package TUI, manager TUI) receives the same
:class:`Outcome` from the operations in :mod:`gupkg.commands` and shows it with
the same formatters, so the wording, status names, and machine-readable layout
cannot differ between them.

Usage and API
-------------
``failure(...)`` builds a failed outcome. ``format_human(...)`` returns the text
a person should see (the result records; problems are events), and
``format_toml(...)`` returns one parseable ``output_schema = 1`` document.

Implementation Approach
-----------------------
An outcome carries the workflow's :class:`~gupkg.core.ActionResult` plus
optional structured ``data`` sections (``package``, ``config``, ``manager``,
``registry``, ``self``, ``summary``) and record lists (``targets``,
``registry_packages``, ``self_shims``). Formatters walk those sections in a
fixed order, which keeps output deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .core import EXIT_USER_ERROR, ActionResult
from .tomlio import dumps


@dataclass
class Outcome:
    """Hold one command result and its optional command-specific records."""

    command: str
    result: ActionResult
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Return the reported status, defaulting to changed, current, or failed."""
        result = self.result
        return result.status or (
            "failed" if not result.ok else ("changed" if result.changed else "current")
        )


def failure(command: str, message: str, code: int = EXIT_USER_ERROR) -> Outcome:
    """Create a failed outcome that has not touched package state."""
    return Outcome(command, ActionResult(ok=False, errors=[message], exit_code=code))


# Record sections rendered as TOML tables (and ``section.key`` human lines).
_TABLE_SECTIONS = ("package", "config", "manager", "registry", "self", "summary")
# Record lists rendered as TOML arrays of tables, with their human prefixes.
_ARRAY_SECTIONS = (
    ("targets", "target", "target"),
    ("registry_packages", "registry.package", "registry package"),
    ("self_shims", "self.shim", "self shim"),
)


def format_human(outcome: Outcome) -> str:
    """Format an outcome for people.

    Parameters
    ----------
    outcome : Outcome
        Result to present.

    Returns
    -------
    str
        The status line and the records. Warnings and errors are not part of
        it: they are reported once, as events, while the run happens.
    """
    lines = [f"{outcome.command}: {outcome.status}"]
    package = outcome.data.get("package")
    if isinstance(package, dict) and package.get("identity"):
        active = package.get("installed_version")
        lines.append(
            f"  {package['identity']} {package['version']}"
            + (f" (active: {active})" if active else " (not active)")
            + f" [{package['scope']} scope]"
        )
    config = outcome.data.get("config")
    if isinstance(config, dict):
        if config.get("operation") != "check":
            lines.append(f"  {config['path']}")
        if config.get("backup_path"):
            lines.append(f"  previous version kept as {config['backup_path']}")
    for section in ("manager", "registry", "self"):
        values = outcome.data.get(section)
        if isinstance(values, dict):
            lines.extend(f"{section}.{key}: {value}" for key, value in values.items() if value is not None)
    for key, _, label in _ARRAY_SECTIONS:
        for record in outcome.data.get(key, []):
            details = ", ".join(
                f"{name}={value}" for name, value in record.items() if value not in (None, [], "")
            )
            lines.append(f"{label}: {details}")
    summary = outcome.data.get("summary")
    if isinstance(summary, dict):
        lines.append("summary: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    return "\n".join(lines) + "\n"


def format_toml(outcome: Outcome) -> str:
    """Format an outcome as one deterministic, versioned TOML document."""
    result = outcome.result
    document: dict[str, Any] = {
        "output_schema": 1,
        "command": str(outcome.command),
        "ok": result.ok,
        "changed": result.changed,
        "status": str(outcome.status),
        "exit_code": result.exit_code,
        "warnings": [str(item) for item in result.warnings],
        "errors": [str(item) for item in result.errors],
    }

    def table(values: dict[str, Any]) -> dict[str, Any]:
        """Return one table's values, omitting unset ones."""
        return {key: _toml_value(value) for key, value in values.items() if value is not None}

    for section in _TABLE_SECTIONS:
        if isinstance(outcome.data.get(section), dict):
            document.setdefault(section, {}).update(table(outcome.data[section]))
    for key, header, _ in _ARRAY_SECTIONS:
        records = [table(record) for record in outcome.data.get(key, [])]
        if records:
            # A dotted header such as ``registry.package`` is an array inside the ``registry`` table.
            *parents, leaf = header.split(".")
            parent = document
            for name in parents:
                parent = parent.setdefault(name, {})
            parent.setdefault(leaf, []).extend(records)
    return dumps(document)


def _toml_value(value: Any) -> Any:
    """Return a bool, an int or a list of strings as is; any other value becomes a string."""
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value)
