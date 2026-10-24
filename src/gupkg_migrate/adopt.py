"""Adopt a package that 0.17 installed: write its install record from ``current`` and its declared components (design 18).

0.17 left ``current``, the shortcuts, the command wrappers, the environment values and the ``PATH`` entries that the
package declares, and no record of any of them. Adoption gives the 1.0 engine that record without installing anything:
it reads the active version's (already converted) ``pkg.toml``, works out the effects the engine would apply for the
package's install mode, and writes ``<package>\\.gupkg\\package.toml`` with every one of them marked *adopted*. It then
reads 0.17's old receipts and ``.gupkg\\state\\update.toml`` once, reports what they said and removes them (the engine
reads receipts of its own schema). ``gupkg list`` is unchanged afterwards, nothing is reinstalled, and ``uninstall``
removes the shortcuts that 0.17 created.

Usage and API
-------------
``plan_adoption(package_root, system=..., env=..., mode=..., settings_path=...)`` returns an ``AdoptionPlan`` (the record,
the files to remove, notes and errors) and changes nothing; ``apply_adoption(plan)`` writes the record first and then
removes the old files. A package that already has an install record is reported as adopted and left alone.

Implementation Approach
-----------------------
The effects come from the engine's own ``desired_effects``, so an adopted package and a freshly installed one carry the
same record. Two 0.17 habits are matched so the record names what is really on the machine: a shortcut whose name contains a
dot was saved with its last dot-part replaced by ``.lnk``, and ``PATH`` entries that the package declares are recorded as
added by the install (so uninstall removes them), while the shared ``bin`` folder never is. A package that 0.17 never
installed (no ``current``) is skipped, and one whose active version is not format 2 yet is reported with the command that
converts it.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from gupkg.catalog.local import DATA_DIR_NAME, read_current
from gupkg.catalog.record import read_install_record, write_install_record
from gupkg.engine.effects import EffectError, desired_effects, effect_context
from gupkg.catalog.catalog import CatalogError
from gupkg.model.manifest import APP_MISSING_ERROR, ManifestError
from gupkg.model.mode import InputNeeded, ModeRefused
from gupkg.model.settings import SettingsError
from gupkg.model.state import Effect, InstallRecord
from gupkg.modes import ModeRequest, resolve_mode, settings_loader
from gupkg.pkgcheck import check_directory
from gupkg.system.port import System, is_elevated_run


@dataclass
class AdoptionPlan:
    """What adopting one package would write and remove.

    Attributes
    ----------
    package : Path
        The package root.
    version : str, optional
        The active version (what ``current`` names).
    mode : str, optional
        The install mode the record will carry.
    record : InstallRecord, optional
        The install record to write.
    remove : list of Path
        Old 0.17 files to delete once the record is written.
    notes, errors : list of str
        What the old files said, and what stops the adoption.
    already_adopted : bool
        The package has an install record; nothing is done.
    skipped : str
        Why there is nothing to adopt (no ``current``), empty otherwise.
    """

    package: Path
    version: str | None = None
    mode: str | None = None
    record: InstallRecord | None = None
    remove: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    already_adopted: bool = False
    skipped: str = ""

    @property
    def ok(self) -> bool:
        """Return whether the plan can be applied."""
        return not self.errors

    @property
    def changes_anything(self) -> bool:
        """Return whether applying the plan writes or removes something."""
        return self.ok and not self.already_adopted and not self.skipped and self.record is not None


def plan_adoption(
    package_root: Path,
    *,
    system: System,
    env: Mapping[str, str],
    mode: str | None = None,
    settings_path: Path | None = None,
) -> AdoptionPlan:
    """Plan the adoption of the package at *package_root* without changing anything.

    Parameters
    ----------
    package_root : Path
        A package root that 0.17 installed (it has a ``current`` junction).
    system : System
        The machine boundary (reads ``current``, answers admin-only questions).
    env : mapping
        The environment, to expand the configured roots.
    mode : str, optional
        ``system``, ``user`` or ``portable`` when the root does not decide it.
    settings_path : Path, optional
        A settings file naming the roots (``--settings``).

    Returns
    -------
    AdoptionPlan
        The plan; ``errors`` says what stops it.
    """
    package_root = Path(package_root)
    plan = AdoptionPlan(package_root)
    current = read_current(package_root, system)
    if current.status == "absent":
        plan.skipped = "no current: 0.17 did not install it, so there is nothing to adopt"
        return plan
    if current.status != "valid":
        plan.skipped = f"{current.problem or 'current is ' + current.status}; not adopted"
        return plan
    plan.version = current.version
    plan.remove = _old_files(package_root, plan)

    # An install record means the package is already managed by 1.0.
    existing = read_install_record(package_root)
    if existing.problem:
        plan.errors.append(existing.problem)
        return plan
    if existing.record is not None:
        plan.already_adopted = True
        return plan

    # The active version must already be format 2: adoption follows the conversion.
    report = check_directory(current.target)
    if report.manifest is None or any(error != APP_MISSING_ERROR for error in report.errors):
        plan.errors.append(
            f"the active version {current.version} is not a valid format 2 version directory ({'; '.join(report.errors)}); "
            f"convert it first with `gupkg-migrate package {current.target}`"
        )
        return plan

    # The install mode comes from the root that holds the package, or from --mode.
    elevated = is_elevated_run(system)
    loader = settings_loader(system=system, env=env, elevated=elevated, settings_path=settings_path)
    try:
        resolved = resolve_mode(ModeRequest(str(package_root), mode), system=system, env=env, load=loader)
    except (ModeRefused, InputNeeded, CatalogError, SettingsError) as error:
        plan.errors.append(f"cannot tell the install mode ({error}); pass --mode system|user|portable")
        return plan
    context = resolved.context
    plan.mode = context.mode

    # The effects the engine would apply, marked as adopted, and matched to what 0.17 wrote.
    try:
        desired, notes = desired_effects(report.manifest, effect_context(context, package_root, env))
    except (EffectError, ManifestError, ValueError) as error:
        plan.errors.append(f"the declared components cannot be expanded: {error}")
        return plan
    plan.notes.extend(notes)
    adopted = [_adopt(effect, context.start_menu, system) for effect in desired]
    plan.record = InstallRecord(context.mode, None, tuple(adopted))
    return plan


def apply_adoption(plan: AdoptionPlan) -> list[str]:
    """Write the install record and then remove the old 0.17 files.

    The record is written first and atomically, so an interruption leaves either nothing or a complete adoption; the
    old files are only deleted after that.

    Returns
    -------
    list of str
        What was written and removed.

    Raises
    ------
    ValueError
        If the plan has errors.
    OSError
        If the record cannot be written (nothing was removed then).
    """
    if not plan.ok:
        raise ValueError("refusing to apply an adoption plan with errors")
    done: list[str] = []
    if plan.changes_anything:
        written = write_install_record(plan.package, plan.record)
        done.append(f"wrote {written} ({len(plan.record.effects)} effects, adopted)")
    for path in plan.remove:
        try:
            if path.is_file():
                os.chmod(path, 0o666)
                path.unlink()
                done.append(f"removed {path}")
        except OSError as error:
            done.append(f"could not remove {path}: {error}")
    for folder in (plan.package / DATA_DIR_NAME / "state", plan.package / DATA_DIR_NAME / "receipts"):
        try:
            folder.rmdir()
        except OSError:
            pass
    return done


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adopt(effect: Effect, start_menu: Path | None, system: System) -> Effect:
    """Mark *effect* adopted and match the facts that 0.17 recorded nowhere.

    A shortcut name with a dot was saved by 0.17 with ``with_suffix``: when that file is the one on the machine, the
    record names it. A ``PATH`` entry that is not the shared ``bin`` folder is recorded as added by the install.
    """
    fields = dict(effect.fields)
    if effect.kind == "shortcut" and start_menu is not None:
        wanted = Path(str(fields["path"]))
        legacy = wanted.with_name(wanted.name[: -len(".lnk")]).with_suffix(".lnk")
        if legacy != wanted and _exists(system, legacy) and not _exists(system, wanted):
            fields["path"] = str(legacy)
    if effect.kind == "path" and not fields.get("shared"):
        fields["added"] = True
    return Effect(effect.kind, fields, adopted=True)


def _exists(system: System, shortcut: Path) -> bool:
    """Return whether the system can read a shortcut at *shortcut*."""
    try:
        system.read_shortcut(shortcut)
    except OSError:
        return False
    return True


def _old_files(package_root: Path, plan: AdoptionPlan) -> list[Path]:
    """Read 0.17's receipts and ``update.toml`` once, note what they said, and return the files to remove.

    Only files that have 0.17's shape are listed: a receipt of the 1.0 schema is never touched.
    """
    base = package_root / DATA_DIR_NAME
    found: list[Path] = []
    receipts = base / "receipts"
    if receipts.is_dir() and not os.path.isjunction(receipts):
        for path in sorted(receipts.glob("*.toml")):
            data = _read(path)
            if data is not None and "schemaVersion" in data and "schema" not in data:
                found.append(path)
                plan.notes.append(
                    f"0.17 receipt {path.name}: version {data.get('version', '?')} was staged and not activated; "
                    "it is an ordinary version directory now"
                )
    state = base / "state"
    if state.is_dir() and not os.path.isjunction(state):
        for path in sorted([state / "update.toml", *state.glob("update.corrupt-*.toml")]):
            if not path.is_file():
                continue
            data = _read(path)
            found.append(path)
            if data is not None and path.name == "update.toml":
                checked = data.get("lastSuccessfulCheck") or data.get("lastAttemptedCheck")
                plan.notes.append(
                    "0.17 update state read once: "
                    + (f"last check {checked}, status {data.get('lastStatus', '?')}" if checked else "no check recorded")
                    + "; 1.0 keeps no such file"
                )
    return found


def _read(path: Path) -> dict | None:
    """Return the parsed TOML of *path*, or ``None`` when it cannot be read (the file is then still removed)."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
