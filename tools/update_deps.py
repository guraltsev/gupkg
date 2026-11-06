"""Update or refresh the locked runtime dependencies of a gupkg checkout.

This is a maintainer tool for the development tree; it is never shipped and
gupkg never runs it. ``tools/gupkg-deps.lock`` pins every distribution
(including transitive ones) that the bundled Python needs, with SHA-256 hashes.
``update_deps`` resolves newer stable releases, shows the plan, asks for
confirmation, then installs them into the committed runtime's
``Lib/site-packages`` and rewrites the lock. ``repair_deps`` reinstalls exactly
the pinned set. It only touches the checkout, so it needs no elevation. The
runtime contains no pip: run the tool with the development ``.venv``, whose pip
does the resolving and the installing.

Usage and API
-------------
``python tools/update_deps.py`` shows the plan and asks; ``--yes`` applies it
without asking; ``--repair`` reinstalls the current lock. ``read_lock``,
``format_lock`` and ``plan_changes`` are available for tools that inspect or
generate lock files.

Implementation Approach
-----------------------
Wheels are installed into a staging directory next to the live one with the
build machine's pip (``fetch_runtime.wheel_install_command``: cross-targeted at
the bundled CPython, ``--require-hashes --only-binary=:all: --no-deps``) so no
build script runs. A staging directory that contains pip, setuptools or ensurepip
is refused. Probe imports run in an isolated child process (the bundled
interpreter) against the staging directory.
Only after they pass is the live directory renamed to a backup and the staging
directory renamed into place, so each step either completes or leaves the live
install as it was. New versions are resolved with ``pip install --dry-run
--report`` (targeted at the bundled CPython so markers match), which also yields
the wheel hashes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_runtime  # noqa: E402  (a sibling script, not an installed package)

# Repository root, anchor of the default runtime location.
ROOT = Path(__file__).resolve().parents[1]
# The committed runtime whose ``Lib/site-packages`` is updated.
DEFAULT_RUNTIME = ROOT / "src" / "gupkg" / "python"
# The hash-pinned lock that this tool reads and rewrites.
DEFAULT_LOCK = Path(__file__).resolve().parent / "gupkg-deps.lock"

# Distributions whose closure the lock must contain: gupkg's text interface and the
# TOML writer. pip is not one of them: the bundle has no installer.
LOCK_ROOTS = ("textual", "tomli_w")

# A bare top-level import can succeed for an empty or damaged package directory
# (it becomes a namespace package), so these imports prove the install is usable.
IMPORT_PROBES = {"textual": "textual.app"}

# A distribution name that is safe to pass to pip and to print.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A stable release version: plain dotted numbers, no pre-release or local part.
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


@dataclass(frozen=True)
class LockedRequirement:
    """Describe one pinned distribution and the hashes its wheel may have.

    Attributes
    ----------
    name : str
        Distribution name as the index spells it.
    version : str
        Pinned stable version.
    hashes : tuple of str
        Allowed SHA-256 digests of the wheel, in hex.
    """

    name: str
    version: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class PlanEntry:
    """Describe how one distribution changes between two lock files.

    Attributes
    ----------
    name : str
        Distribution name.
    old : str or None
        Version in the old lock; ``None`` when the distribution is added.
    new : str or None
        Version in the new lock; ``None`` when the distribution is removed.
    description : str
        One-line human description of the change.
    """

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


def main(argv: list[str] | None = None) -> int:
    """Run the update (or ``--repair``) against the committed runtime and lock.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; ``sys.argv[1:]`` when omitted.

    Returns
    -------
    int
        ``0`` on success (including a declined plan), ``1`` when a step fails.

    Raises
    ------
    SystemExit
        If the plan needs confirmation, ``--yes`` is absent and there is no terminal.
    """
    # Describe the command line from the module summary.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repair", action="store_true", help="reinstall exactly the pinned set")
    parser.add_argument("--yes", action="store_true", help="apply the plan without asking")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--python", help="interpreter whose pip does the work (default: the one running this tool)")
    args = parser.parse_args(argv)

    # The plan is always shown; applying it needs ``--yes`` or an interactive answer.
    def _confirm(lines: list[str]) -> bool:
        """Show the plan and decide whether to apply it; raise ``SystemExit`` when no terminal can answer."""
        for line in lines:
            print(line)
        if args.yes:
            return True
        if not sys.stdin.isatty():
            raise SystemExit("Refusing to apply a plan without --yes when there is no terminal")
        return input("Apply these changes? [y/N] ").strip().casefold() in {"y", "yes"}

    # Failures of either workflow are reported on stderr with status 1.
    try:
        if args.repair:
            repair_deps(args.runtime, args.lock, python=args.python)
        else:
            update_deps(args.runtime, args.lock, confirm=_confirm, python=args.python)
    except RuntimeError as error:
        print(f"[update-deps] {error}", file=sys.stderr)
        return 1
    return 0


def repair_deps(
    runtime: Path = DEFAULT_RUNTIME,
    lock: Path = DEFAULT_LOCK,
    progress: Callable[[str], None] = print,
    python: str | None = None,
) -> list[LockedRequirement]:
    """Reinstall exactly the distributions pinned by the lock.

    Parameters
    ----------
    runtime : Path
        Committed runtime whose ``Lib/site-packages`` is replaced.
    lock : Path
        Lock file of the pinned distributions.
    progress : callable, default=print
        Receives one status line per step.
    python : str, optional
        Interpreter whose pip does the install; defaults to the running one.

    Returns
    -------
    list of LockedRequirement
        The distributions that are now installed.

    Raises
    ------
    RuntimeError
        If the runtime is missing or any install step fails; the live
        directory and the lock are unchanged in that case.
    """
    # Refuse an empty or unreadable lock before touching the live install.
    _require_runtime(runtime)
    requirements = read_lock(lock)
    if not requirements:
        raise RuntimeError(f"{lock} lists no distributions; nothing to install")
    # Install into staging and swap, so a failure leaves the live install alone.
    progress(f"[update-deps] Installing {len(requirements)} locked distributions with hash verification")
    _install_and_swap(runtime, lock, python)
    progress("[update-deps] Dependencies repaired")
    return requirements


def update_deps(
    runtime: Path = DEFAULT_RUNTIME,
    lock: Path = DEFAULT_LOCK,
    *,
    confirm: Callable[[list[str]], bool],
    progress: Callable[[str], None] = print,
    python: str | None = None,
) -> list[PlanEntry]:
    """Resolve newer stable releases, confirm the plan, then install and relock.

    Parameters
    ----------
    runtime : Path
        Committed runtime whose ``Lib/site-packages`` is replaced.
    lock : Path
        Lock file that is rewritten after a successful update.
    confirm : callable
        Receives the plan lines and returns whether to proceed.
    progress : callable, default=print
        Receives one status line per step.
    python : str, optional
        Interpreter whose pip resolves and installs; defaults to the running one.

    Returns
    -------
    list of PlanEntry
        The applied changes; empty when everything is current or the plan was
        declined.

    Raises
    ------
    RuntimeError
        If versions cannot be resolved (for example, PyPI is unreachable) or an
        install step fails. The live directory and the lock are unchanged then.
    """
    # Read the current pins first; a missing runtime or lock fails before any network use.
    _require_runtime(runtime)
    current = read_lock(lock)

    # Resolve for the bundled CPython so environment markers match it.
    progress("[update-deps] Resolving the newest stable releases from PyPI")
    candidate = resolve_lock(runtime, LOCK_ROOTS, python)
    plan = plan_changes(current, candidate)
    if not plan:
        progress("[update-deps] Dependencies are already current")
        return []

    # Nothing is installed until the person has seen exactly what changes.
    lines = ["Planned dependency changes:", *(entry.description for entry in plan)]
    if not confirm(lines):
        progress("[update-deps] Update cancelled; nothing was changed")
        return []

    # Install from a temporary lock; the real one changes only after the swap.
    pending = lock.with_name(lock.name + ".new")
    pending.write_text(format_lock(candidate), encoding="utf-8", newline="\n")
    try:
        _install_and_swap(runtime, pending, python)
        os.replace(pending, lock)
    finally:
        pending.unlink(missing_ok=True)
    progress("[update-deps] Dependencies updated and lock rewritten")
    return plan


# ---------------------------------------------------------------------------
# Lock file format
# ---------------------------------------------------------------------------


def read_lock(path: Path) -> list[LockedRequirement]:
    """Read a lock file of ``name==version --hash=sha256:...`` lines.

    Parameters
    ----------
    path : Path
        Lock file to parse; comments and blank lines are skipped.

    Returns
    -------
    list of LockedRequirement
        One entry per pinned distribution, in file order.

    Raises
    ------
    RuntimeError
        If the file is missing or a line is malformed or lacks a hash.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read the dependency lock {path}: {exc}") from exc

    # Join backslash-continued lines, then parse one requirement per line.
    requirements = []
    for line in text.replace("\\\r\n", " ").replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split the pin from its hashes.
        parts = line.split()
        match = re.fullmatch(r"([^=\s]+)==([^=\s]+)", parts[0])
        hashes = tuple(part[len("--hash=sha256:") :] for part in parts[1:] if part.startswith("--hash=sha256:"))
        # Valid means a safe name and version, and nothing after the pin but SHA-256 hashes.
        valid = (
            match is not None
            and _NAME.match(match.group(1))
            and _VERSION.match(match.group(2))
            and hashes
            and len(hashes) == len(parts) - 1
            and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
        )
        # Fail loudly on the first bad line: an unhashed pin would defeat hash checking.
        if not valid:
            raise RuntimeError(f"Malformed or unhashed line in {path}: {line!r}")
        requirements.append(LockedRequirement(match.group(1), match.group(2), hashes))
    return requirements


def format_lock(requirements: list[LockedRequirement]) -> str:
    """Render requirements in the canonical lock format, sorted by name.

    Parameters
    ----------
    requirements : list of LockedRequirement
        Pins to render.

    Returns
    -------
    str
        Lock file text, ending in a newline.
    """
    lines = ["# Generated by tools/update_deps.py; do not edit by hand."]
    for item in sorted(requirements, key=lambda entry: _canonical(entry.name)):
        hashes = " ".join(f"--hash=sha256:{digest}" for digest in item.hashes)
        lines.append(f"{item.name}=={item.version} {hashes}")
    return "\n".join(lines) + "\n"


def plan_changes(old: list[LockedRequirement], new: list[LockedRequirement]) -> list[PlanEntry]:
    """Return added, removed, and re-versioned distributions between two locks.

    Parameters
    ----------
    old, new : list of LockedRequirement
        The current pins and the candidate pins.

    Returns
    -------
    list of PlanEntry
        One entry per changed distribution, in canonical-name order.
    """
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


def resolve_lock(runtime: Path, roots: tuple[str, ...], python: str | None = None) -> list[LockedRequirement]:
    """Resolve the newest stable wheels for *roots* and their dependencies.

    The build machine's pip resolves for the bundled CPython (platform and
    interpreter flags), so the runtime itself needs no pip.

    Parameters
    ----------
    runtime : Path
        Committed runtime that supplies the target interpreter tag.
    roots : tuple of str
        Top-level distributions whose closure is resolved.
    python : str, optional
        Interpreter whose pip resolves; defaults to the running one.

    Note that pip evaluates environment markers (``sys_platform``, ``python_version``
    and so on) against the *host* interpreter, not the cross-target, so a dependency
    whose marker differs between the host and the bundled CPython could be resolved
    differently from what the bundle needs. The staged import probe and the build
    probe catch a missing package; review the plan when a new dependency appears.

    Returns
    -------
    list of LockedRequirement
        The full closure with the SHA-256 PyPI served for each wheel.

    Raises
    ------
    RuntimeError
        If pip cannot resolve (for example, offline), reports no hash, or the
        closure contains pip or setuptools.
    """
    tag = fetch_runtime.runtime_tag(runtime)
    with tempfile.TemporaryDirectory(prefix="gupkg-resolve-") as scratch:
        # Ask pip for a dry-run report cross-targeted at the bundled CPython; nothing is installed.
        report_path = Path(scratch) / "report.json"
        command = [
            python or sys.executable, "-I", "-B", "-m", "pip", "install", "--isolated",
            "--dry-run", "--ignore-installed", "--only-binary=:all:", "--disable-pip-version-check",
            "--no-input", "--quiet", "--report", str(report_path),
            "--target", str(Path(scratch) / "target"), "--platform", "win_amd64", "--implementation", "cp",
            "--python-version", f"{tag[0]}.{tag[1:]}", "--abi", f"cp{tag}", *roots,
        ]
        # Surface only pip's last line when the resolution fails, stripped of control characters.
        result = _run(command, runtime)
        if result.returncode != 0 or not report_path.is_file():
            detail = [_printable(line) for line in (result.stderr or result.stdout).strip().splitlines()[-1:]] or ["no output"]
            raise RuntimeError(
                f"Could not resolve dependency versions (is PyPI reachable? does {python or sys.executable} have pip?): "
                f"{detail[0]}. "
                "The existing install and lock are unchanged."
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return requirements_from_report(report)


def requirements_from_report(report: dict) -> list[LockedRequirement]:
    """Convert a pip installation report into locked requirements.

    Parameters
    ----------
    report : dict
        Decoded ``pip install --report`` document.

    Returns
    -------
    list of LockedRequirement
        One pin per installed distribution, with the wheel's SHA-256.

    Raises
    ------
    RuntimeError
        If an entry has an unexpected name or version, is yanked, or lacks a
        SHA-256 hash.
    """
    requirements = []
    for item in report.get("install", []):
        # Take name, version and wheel digest from the report entry.
        metadata = item.get("metadata", {})
        name, version = str(metadata.get("name", "")), str(metadata.get("version", ""))
        digest = item.get("download_info", {}).get("archive_info", {}).get("hashes", {}).get("sha256")
        if not (_NAME.match(name) and _VERSION.match(version)):
            raise RuntimeError(f"Refusing unexpected release {name!r} {version!r} (stable releases only)")
        # Installers are never locked, yanked releases are refused, and a hash is mandatory.
        if name.casefold().replace("_", "-") in fetch_runtime.FORBIDDEN_PREFIXES:
            raise RuntimeError(f"Refusing {name}: the bundle must not contain pip or setuptools")
        if item.get("is_yanked"):
            raise RuntimeError(f"Refusing yanked release {name} {version}")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise RuntimeError(f"pip reported no SHA-256 for {name} {version}")
        requirements.append(LockedRequirement(name, version, (digest,)))
    return requirements


# --- supporting helpers ---


def _canonical(name: str) -> str:
    """Normalize a distribution name the way package indexes compare them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _printable(text: str) -> str:
    """Drop control characters from subprocess text before it reaches the terminal."""
    return "".join(char for char in text if char == "\n" or char.isprintable())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Staged install and swap
# ---------------------------------------------------------------------------


def _install_and_swap(runtime: Path, lock_path: Path, python: str | None = None) -> None:
    """Install *lock_path* into staging, probe it, then swap it into place."""
    live = runtime / "Lib" / "site-packages"
    staging = live.with_name("site-packages.staging")
    backup = live.with_name("site-packages.old")

    # Start from empty staging; a leftover from an interrupted run is discarded.
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        # Wheels only, hashes required, no dependency resolution: nothing but
        # the pinned files can land, and no build script can run. The build
        # machine's pip does the work, cross-targeted at the bundled CPython.
        command = fetch_runtime.wheel_install_command(staging, lock_path, fetch_runtime.runtime_tag(runtime), python)
        result = _run(command, runtime)
        if result.returncode != 0:
            tail = _printable("\n".join((result.stderr or result.stdout).strip().splitlines()[-4:]))
            raise RuntimeError(
                "Installing the locked dependencies failed (hash mismatch, no wheel for a "
                f"pinned version, or no pip in the installing interpreter); the live install is unchanged.\n{tail}"
            )
        fetch_runtime.tidy_site_packages(staging)

        # An installer in the tree (a lock entry, or a transitive dependency) is refused.
        offenders = [
            entry.name for entry in sorted(staging.iterdir())
            if entry.name.casefold().split("-", 1)[0].removesuffix(".py").removesuffix(".dist")
            in fetch_runtime.FORBIDDEN_PREFIXES
        ]
        if offenders:
            raise RuntimeError(
                f"The staged install contains {', '.join(offenders)}; the bundle must "
                "not contain pip or setuptools. The live install is unchanged."
            )

        # Prove the staged copy imports before it replaces anything.
        _probe_staging(runtime, staging)

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
        # The swap succeeded, so the backup is no longer needed.
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        # Staging is gone after a swap and discarded after a failure.
        shutil.rmtree(staging, ignore_errors=True)


def _probe_staging(runtime: Path, staging: Path) -> None:
    """Import each root distribution's probe module from *staging* in an isolated child."""
    # Roots pull in their transitive closure, so importing them exercises it.
    names = [IMPORT_PROBES.get(root.replace("-", "_"), root.replace("-", "_")) for root in LOCK_ROOTS]
    code = (
        "import importlib, importlib.machinery, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "failed = []\n"
        # Every import failure is collected, then reported at once with a non-zero exit.
        "for name in sys.argv[2:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        failed.append(f'{name}: {exc}')\n"
        # Finally, neither pip nor ensurepip may be importable from the bundle.
        "for banned in ('pip', 'ensurepip'):\n"
        "    if importlib.machinery.PathFinder.find_spec(banned, [sys.argv[1]]) is not None:\n"
        "        failed.append(f'{banned} is importable')\n"
        "print('; '.join(failed))\n"
        "raise SystemExit(1 if failed else 0)\n"
    )
    # Run the bundled interpreter, isolated, against the staging directory only.
    result = _run([_python(runtime), "-I", "-B", "-c", code, str(staging), *names], runtime)
    if result.returncode != 0:
        raise RuntimeError(
            f"The staged dependencies failed their import check ({result.stdout.strip()}); "
            "the live install is unchanged."
        )


def _require_runtime(runtime: Path) -> None:
    """Fail clearly when the committed runtime that does the installing is missing."""
    if not (runtime / "python.exe").is_file():
        raise RuntimeError(f"The runtime {runtime / 'python.exe'} is missing; run tools/fetch_runtime.py first")


def _python(runtime: Path) -> str:
    """Return the runtime's interpreter path."""
    return str(runtime / "python.exe")


def _run(command: list[str], runtime: Path) -> subprocess.CompletedProcess[str]:
    """Run one isolated runtime-interpreter command with captured text output."""
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith(("PYTHON", "PIP_"))
    }
    return subprocess.run(
        command, cwd=str(runtime), env=environment, capture_output=True, text=True, check=False
    )


if __name__ == "__main__":
    raise SystemExit(main())
