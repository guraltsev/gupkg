"""Convert a 0.17 manager configuration (``gupkg-config.toml``, schema 2) to ``settings.toml`` (schema 3).

``plan_settings(config_path)`` reads the old file and returns a ``SettingsPlan``: the new
settings text, where it goes (the system root's ``.gupkg\\settings.toml``), notes about what
was dropped and the validation errors. ``apply_settings(plan)`` writes it and refuses to
replace an existing file unless told to.

The mapping follows design section 11: ``[packages]`` becomes ``[roots]``, ``[bin]`` becomes
``[integration.*].bin``, and ``[registry]`` becomes a ``[[registry]]`` named ``main``. The
old registry cache folder and the shim linkage have no place in schema 3 and are dropped
with a note. The result is validated with the schema 3 model before anything is written.
"""

from __future__ import annotations

import ntpath
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gupkg.core import write_text_atomic
from gupkg.model.settings import Layer, SettingsError, default_layer, expand_env, merge_layers
from gupkg.settingsload import settings_file
from gupkg.tomlio import dumps


@dataclass
class SettingsPlan:
    """The outcome of converting one manager configuration, before anything is written."""

    source: Path
    output: Path | None = None
    text: str = ""
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the plan can be applied."""
        return not self.errors


def _path_text(value: Any, base: Path, field_name: str, errors: list[str]) -> str | None:
    """Return a configured path as settings text: ``%VAR%`` kept, ``~`` and relative paths made explicit."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"[{field_name}] must be a nonempty string")
        return None
    text = value.strip()
    if text.startswith("~"):
        return "%USERPROFILE%" + text[1:].replace("/", "\\")
    if text.startswith("%") or ntpath.splitdrive(text)[0]:
        return text.replace("/", "\\")
    return ntpath.normpath(ntpath.join(str(base), text))


def plan_settings(config_path: Path, *, env: Mapping[str, str] | None = None,
                  output: Path | None = None) -> SettingsPlan:
    """Plan the conversion of a schema 2 manager configuration.

    Parameters
    ----------
    config_path : Path
        The ``gupkg-config.toml`` to convert; relative paths in it resolve against its folder.
    env : mapping, optional
        The environment used to place the output under the expanded system root
        (``os.environ`` by default).
    output : Path, optional
        Where to write the result instead of ``<system root>\\.gupkg\\settings.toml``.

    Returns
    -------
    SettingsPlan
        The new text, destination, notes and errors.
    """
    config_path = Path(config_path)
    plan = SettingsPlan(config_path)
    try:
        old = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        plan.errors.append(f"cannot read {config_path}: {exc}")
        return plan
    if old.get("schema_version") != 2 or old.get("mode") != "manager":
        plan.errors.append("this is not a schema 2 manager configuration (mode = \"manager\", schema_version = 2)")
        return plan

    base = config_path.resolve().parent
    packages = old.get("packages") if isinstance(old.get("packages"), dict) else {}
    bins = old.get("bin") if isinstance(old.get("bin"), dict) else {}
    data: dict[str, Any] = {"schema": 3}
    roots = {
        name: _path_text(packages.get(name), base, f"packages.{name}", plan.errors) for name in ("system", "user")
    }
    data["roots"] = {name: value for name, value in roots.items() if value}
    integration = {}
    for name in ("system", "user"):
        text = _path_text(bins.get(name), base, f"bin.{name}", plan.errors)
        if text:
            integration[name] = {"bin": text}
    if integration:
        data["integration"] = integration

    registry = old.get("registry") if isinstance(old.get("registry"), dict) else {}
    if registry.get("source"):
        data["registry"] = [{"name": "main", "source": registry["source"]}]
    if registry.get("cache"):
        plan.notes.append("[registry].cache was dropped: the registry cache now lives in <root>\\.gupkg\\")
    shims = old.get("shims")
    if isinstance(shims, dict) and shims.get("linkage") not in (None, "dynamic"):
        plan.notes.append(f"[shims].linkage = {shims['linkage']!r} was dropped: settings schema 3 has no shim linkage")
    if "self" in old:
        plan.notes.append("[self] was dropped: it held a setting that no longer exists")

    if plan.errors:
        return plan
    try:
        merge_layers([default_layer(), Layer("system", data, str(config_path))])
    except SettingsError as exc:
        plan.errors.extend(exc.problems)
        return plan
    plan.text = dumps(data)
    if output is not None:
        plan.output = Path(output)
    else:
        try:
            system_root = Path(expand_env(data["roots"]["system"], os.environ if env is None else env))
        except SettingsError as exc:
            plan.errors.append(str(exc))
            return plan
        plan.output = settings_file(system_root)
    return plan


def apply_settings(plan: SettingsPlan, *, force: bool = False) -> Path:
    """Write the settings file of a plan.

    Parameters
    ----------
    plan : SettingsPlan
        A plan from ``plan_settings``.
    force : bool, optional
        Replace an existing settings file.

    Returns
    -------
    Path
        The file written.

    Raises
    ------
    ValueError
        If the plan has errors, or the destination exists and *force* is not given.
    """
    if not plan.ok or plan.output is None:
        raise ValueError("refusing to apply a settings plan with errors")
    if plan.output.exists() and not force:
        raise ValueError(f"{plan.output} already exists; use --force to replace it")
    write_text_atomic(plan.output, plan.text)
    return plan.output
