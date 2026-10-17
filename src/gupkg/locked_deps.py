"""Install and update gupkg's runtime dependencies from a hash-pinned lockfile.

The lockfile ``gupkg-deps.lock`` beside the ``gupkg`` package pins every
distribution (including transitive ones) that the bundled Python needs, with
SHA-256 hashes. ``repair_deps`` reinstalls exactly that set into the bundled
``python\\Lib\\site-packages`` directory; ``update_deps`` resolves newer stable
releases, shows a plan, asks for confirmation, and then installs and rewrites
the lockfile. Both refuse to run unless the process is elevated and every
directory it trusts is admin-only. A failed run leaves the live directory and
the lockfile untouched.

Usage and API
-------------
Call ``repair_deps(...)`` or ``update_deps(...)``; the command line exposes them
as ``gupkg manager self repair-deps`` and ``gupkg manager self update-deps``.
``read_lock``, ``format_lock`` and ``plan_changes`` are available for tools that
inspect or generate lockfiles.

Implementation Approach
-----------------------
Wheels are installed into a staging directory next to the live one with
``--require-hashes --only-binary=:all: --no-deps`` so no build script runs.
Probe imports run in an isolated child process against the staging directory.
Only after they pass is the live directory renamed to a backup and the staging
directory renamed into place, so each step either completes or leaves the live
install as it was. New versions are resolved with ``pip install --dry-run
--report`` in the bundled interpreter, which also yields the wheel hashes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .dependencies import _IMPORT_PROBES, _RUNTIME_DEPENDENCIES
from .elevation import elevated_mode, enforce_elevated_runtime, require_trusted_directory

LOCK_NAME = "gupkg-deps.lock"

# Distributions the lockfile must always contain besides gupkg's own features:
# pip is needed to repair or update, and the swap replaces the whole directory.
_EXTRA_ROOTS = ("pip",)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


class NotElevatedError(RuntimeError):
    """Report that a dependency command was run without administrator rights."""


@dataclass(frozen=True)
class LockedRequirement:
    """Describe one pinned distribution and the hashes its wheel may have."""

    name: str
    version: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class PlanEntry:
    """Describe how one distribution changes between two lockfiles."""

    name: str
    old: str | None
    new: str | None

    @property
    def description(self) -> str:
        """Return a one-line human description of the change."""
        if self.old is None:
            return f"  add     {self.name} {self.new}"
        if self.new is None:
            return f"  remove  {self.name} {self.old}"
        return f"  update  {self.name} {self.old} -> {self.new}"


# ---------------------------------------------------------------------------
# Public commands
# ---------------------------------------------------------------------------


def repair_deps(package_directory: Path | None = None, progress: Callable[[str], None] = print) -> list[LockedRequirement]:
    """Reinstall exactly the distributions pinned by the lockfile.

    Parameters
    ----------
    package_directory : Path, optional
        Directory of the ``gupkg`` package; defaults to the running package.
    progress : callable, default=print
        Receives one status line at a time.

    Returns
    -------
    list of LockedRequirement
        The distributions that are now installed.

    Raises
    ------
    RuntimeError
        If the run is not elevated and trusted, or any install step fails.
        The live directory and the lockfile are unchanged in that case.
    """
    package = _checked_package_directory(package_directory)
    requirements = read_lock(package / LOCK_NAME)
    if not requirements:
        raise RuntimeError(f"{package / LOCK_NAME} lists no distributions; nothing to install")
    progress(f"[gupkg] Installing {len(requirements)} locked distributions with hash verification")
    _install_and_swap(package, package / LOCK_NAME)
    progress("[gupkg] Dependencies repaired")
    return requirements


def update_deps(
    package_directory: Path | None = None,
    *,
    confirm: Callable[[list[str]], bool],
    progress: Callable[[str], None] = print,
) -> list[PlanEntry]:
    """Resolve newer stable releases, confirm the plan, then install and relock.

    Parameters
    ----------
    package_directory : Path, optional
        Directory of the ``gupkg`` package; defaults to the running package.
    confirm : callable
        Receives the plan lines and returns whether to proceed.
    progress : callable, default=print
        Receives one status line at a time.

    Returns
    -------
    list of PlanEntry
        The applied changes; empty when everything is current or the plan was
        declined.

    Raises
    ------
    RuntimeError
        If the run is not elevated and trusted, versions cannot be resolved
        (for example, PyPI is unreachable), or an install step fails.
        The live directory and the lockfile are unchanged in that case.
    """
    package = _checked_package_directory(package_directory)
    current = read_lock(package / LOCK_NAME)

    # Resolve with the bundled interpreter so environment markers match it.
    progress("[gupkg] Resolving the newest stable releases from PyPI")
    candidate = resolve_lock(package, _lock_roots())
    plan = plan_changes(current, candidate)
    if not plan:
        progress("[gupkg] Dependencies are already current")
        return []

    # Nothing is installed until the person has seen exactly what changes.
    lines = ["Planned dependency changes:", *(entry.description for entry in plan)]
    if not confirm(lines):
        progress("[gupkg] Update cancelled; nothing was changed")
        return []

    # Install from a temporary lock; the real one changes only after the swap.
    pending = package / (LOCK_NAME + ".new")
    pending.write_text(format_lock(candidate), encoding="utf-8", newline="\n")
    try:
        _install_and_swap(package, pending)
        os.replace(pending, package / LOCK_NAME)
    finally:
        pending.unlink(missing_ok=True)
    progress("[gupkg] Dependencies updated and lockfile rewritten")
    return plan


# ---------------------------------------------------------------------------
# Lockfile format
# ---------------------------------------------------------------------------


def read_lock(path: Path) -> list[LockedRequirement]:
    """Read a lockfile of ``name==version --hash=sha256:...`` lines.

    Parameters
    ----------
    path : Path
        Lockfile to read.

    Returns
    -------
    list of LockedRequirement
        Entries in file order.

    Raises
    ------
    RuntimeError
        If the file is missing or a line is malformed or lacks a hash.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read the dependency lockfile {path}: {exc}") from exc

    # Join backslash-continued lines, then parse one requirement per line.
    requirements = []
    for line in text.replace("\\\r\n", " ").replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        match = re.fullmatch(r"([^=\s]+)==([^=\s]+)", parts[0])
        hashes = tuple(part[len("--hash=sha256:") :] for part in parts[1:] if part.startswith("--hash=sha256:"))
        valid = (
            match is not None
            and _NAME.match(match.group(1))
            and _VERSION.match(match.group(2))
            and hashes
            and len(hashes) == len(parts) - 1
            and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
        )
        if not valid:
            raise RuntimeError(f"Malformed or unhashed line in {path}: {line!r}")
        requirements.append(LockedRequirement(match.group(1), match.group(2), hashes))
    return requirements


def format_lock(requirements: list[LockedRequirement]) -> str:
    """Render requirements in the canonical lockfile format, sorted by name."""
    lines = ["# Generated by gupkg; edit with 'gupkg manager self update-deps'."]
    for item in sorted(requirements, key=lambda entry: _canonical(entry.name)):
        hashes = " ".join(f"--hash=sha256:{digest}" for digest in item.hashes)
        lines.append(f"{item.name}=={item.version} {hashes}")
    return "\n".join(lines) + "\n"


def plan_changes(old: list[LockedRequirement], new: list[LockedRequirement]) -> list[PlanEntry]:
    """Return added, removed, and re-versioned distributions between two locks."""
    before = {_canonical(item.name): item for item in old}
    after = {_canonical(item.name): item for item in new}
    plan = []
    for key in sorted(before.keys() | after.keys()):
        old_item, new_item = before.get(key), after.get(key)
        old_version = old_item.version if old_item else None
        new_version = new_item.version if new_item else None
        if old_version != new_version:
            plan.append(PlanEntry((new_item or old_item).name, old_version, new_version))
    return plan


def _printable(text: str) -> str:
    """Drop control characters from subprocess text before it reaches the terminal."""
    return "".join(char for char in text if char == "\n" or char.isprintable())


def _canonical(name: str) -> str:
    """Normalize a distribution name the way package indexes compare them."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------------------
# Preconditions and subprocess helpers
# ---------------------------------------------------------------------------


def _lock_roots() -> tuple[str, ...]:
    """Return the distributions whose closure the lockfile must contain."""
    roots = [name for names in _RUNTIME_DEPENDENCIES.values() for name in names]
    return tuple(dict.fromkeys([*roots, *_EXTRA_ROOTS]))


def _checked_package_directory(package_directory: Path | None) -> Path:
    """Return the package directory after proving the run may modify dependencies."""
    package = Path(package_directory or Path(__file__).resolve().parent).resolve()

    # Dependency management is an administrator-only action on trusted paths.
    if not elevated_mode():
        raise NotElevatedError("This command must be run from an elevated (Administrator) prompt")
    if package_directory is None:
        enforce_elevated_runtime(package)
    runtime = package / "python"
    if not (runtime / "python.exe").is_file():
        raise RuntimeError(
            f"The bundled Python {runtime / 'python.exe'} is missing; "
            "reinstall gupkg from an elevated prompt"
        )
    for directory in (package, runtime):
        require_trusted_directory(directory)
    return package


def _python(package: Path) -> str:
    """Return the bundled interpreter path."""
    return str(package / "python" / "python.exe")


def _clean_environment() -> dict[str, str]:
    """Return a child environment without Python or pip variables the user controls."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYTHON", "PIP_"))
    }


def _run(command: list[str], package: Path) -> subprocess.CompletedProcess[str]:
    """Run one isolated bundled-interpreter command with captured text output."""
    return subprocess.run(
        command,
        cwd=str(package / "python"),
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_lock(package: Path, roots: tuple[str, ...]) -> list[LockedRequirement]:
    """Resolve the newest stable wheels for *roots* and their dependencies.

    Parameters
    ----------
    package : Path
        Directory of the ``gupkg`` package whose bundled Python resolves.
    roots : tuple of str
        Distribution names to resolve.

    Returns
    -------
    list of LockedRequirement
        The full closure with the SHA-256 PyPI served for each wheel.

    Raises
    ------
    RuntimeError
        If pip cannot resolve (for example, offline) or reports no hash.
    """
    with tempfile.TemporaryDirectory(prefix="gupkg-resolve-", dir=package / "python") as scratch:
        report_path = Path(scratch) / "report.json"
        command = [
            _python(package), "-I", "-B", "-m", "pip", "install", "--isolated",
            "--dry-run", "--ignore-installed", "--only-binary=:all:", "--disable-pip-version-check",
            "--no-input", "--quiet", "--report", str(report_path), *roots,
        ]
        result = _run(command, package)
        if result.returncode != 0 or not report_path.is_file():
            detail = [_printable(line) for line in (result.stderr or result.stdout).strip().splitlines()[-1:]] or ["no output"]
            raise RuntimeError(
                f"Could not resolve dependency versions (is PyPI reachable?): {detail[0]}. "
                "The existing install and lockfile are unchanged."
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return requirements_from_report(report)


def requirements_from_report(report: dict) -> list[LockedRequirement]:
    """Convert a pip installation report into locked requirements.

    Raises
    ------
    RuntimeError
        If an entry has an unexpected name or version, is yanked, or lacks a
        SHA-256 hash.
    """
    requirements = []
    for item in report.get("install", []):
        metadata = item.get("metadata", {})
        name, version = str(metadata.get("name", "")), str(metadata.get("version", ""))
        digest = item.get("download_info", {}).get("archive_info", {}).get("hashes", {}).get("sha256")
        if not (_NAME.match(name) and _VERSION.match(version)):
            raise RuntimeError(f"Refusing unexpected release {name!r} {version!r} (stable releases only)")
        if item.get("is_yanked"):
            raise RuntimeError(f"Refusing yanked release {name} {version}")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise RuntimeError(f"pip reported no SHA-256 for {name} {version}")
        requirements.append(LockedRequirement(name, version, (digest,)))
    return requirements


# ---------------------------------------------------------------------------
# Staged install and swap
# ---------------------------------------------------------------------------


def _install_and_swap(package: Path, lock_path: Path) -> None:
    """Install *lock_path* into staging, probe it, then swap it into place."""
    live = package / "python" / "Lib" / "site-packages"
    staging = live.with_name("site-packages.staging")
    backup = live.with_name("site-packages.old")

    # Start from empty staging; a leftover from an interrupted run is discarded.
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        # Wheels only, hashes required, no dependency resolution: nothing but
        # the pinned files can land, and no build script can run.
        command = [
            _python(package), "-I", "-B", "-m", "pip", "install", "--isolated",
            "--target", str(staging), "--require-hashes", "--only-binary=:all:", "--no-deps",
            "--disable-pip-version-check", "--no-input", "--upgrade", "-r", str(lock_path),
        ]
        result = _run(command, package)
        if result.returncode != 0:
            tail = _printable("\n".join((result.stderr or result.stdout).strip().splitlines()[-4:]))
            raise RuntimeError(
                "Installing the locked dependencies failed (hash mismatch, or no wheel for a "
                f"pinned version); the live install is unchanged.\n{tail}"
            )

        # Prove the staged copy imports before it replaces anything.
        _probe_staging(package, staging)

        # Swap: if the second rename fails, put the original back.
        shutil.rmtree(backup, ignore_errors=True)
        if live.exists():
            os.replace(live, backup)
        try:
            os.replace(staging, live)
        except OSError:
            if backup.exists():
                os.replace(backup, live)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _probe_staging(package: Path, staging: Path) -> None:
    """Import each root distribution's probe module from *staging* in an isolated child."""
    # Roots pull in their transitive closure, so importing them exercises it.
    names = [_IMPORT_PROBES.get(root.replace("-", "_"), root.replace("-", "_")) for root in _lock_roots()]
    code = (
        "import importlib, sys\n"
        "sys.path.append(sys.argv[1])\n"
        "failed = []\n"
        "for name in sys.argv[2:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        failed.append(f'{name}: {exc}')\n"
        "print('; '.join(failed))\n"
        "raise SystemExit(1 if failed else 0)\n"
    )
    result = _run([_python(package), "-I", "-B", "-c", code, str(staging), *names], package)
    if result.returncode != 0:
        raise RuntimeError(
            f"The staged dependencies failed their import check ({result.stdout.strip()}); "
            "the live install is unchanged."
        )
