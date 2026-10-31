"""Create, adopt, export and bundle package definitions (``gupkg pkg new``, ``pkg adopt``, ``pkg export`` and ``pkg bundle``).

These are the maintainer-side commands of the ``pkg`` group. ``new`` writes a starter version directory or tracker that
passes ``gupkg pkg check``; ``adopt`` writes the ``pkg.toml`` of a program that is already unpacked in ``App`` from the Start Menu
shortcuts its installer made (moved into a ``shortcuts`` folder beside ``App``); ``export`` writes a distribution ZIP of one version (``<name>\\<version>\\...`` plus the package's
``_check_updates`` when it has one) and reports its SHA-256; ``bundle`` copies the verified bundle of the running gupkg into a
version directory (together with its launchers) so the program can be installed without gupkg (design section 21). Nothing is
overwritten, links are never followed, and every command checks the directory it acts on first. The launchers (``install.cmd`` and
``gupkg-tui.cmd`` of a version, ``check-updates.cmd`` of a tracker) come from the one template of ``gupkg.launchers``.

Usage and API
-------------
``new_package(parent, name, version=None, track=None)`` returns the files it wrote. ``adopt_version(version_dir, read_shortcut,
env)`` returns an ``AdoptResult`` (the file written, the shortcuts kept and the ones skipped with a reason).
``export_distribution(version_dir, output)``
returns an ``ExportResult`` (path, SHA-256 and entry count). ``bundle_gupkg(version_dir, source)`` copies the bundle tree and
returns the number of files copied and writes the version's launchers. ``AuthoringError`` carries the message of a refusal.

Implementation Approach
-----------------------
Every output goes to a new file or a new folder that must not exist yet (``adopt`` replaces a ``pkg.toml`` only when asked, keeping the
old one as ``pkg.toml.old``, and puts it back if the new one fails ``pkg check``). The ZIP is written in sorted order with a fixed
timestamp so the same version directory always gives the same bytes, and the digest is computed from the written file. The
bundle copy refuses a source or a destination that contains a link or a junction, and leaves out bytecode and per-install state.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .launchers import TRACKER_LAUNCHER, VERSION_LAUNCHERS, launcher_text, write_launchers
from .model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name
from .pkgcheck import check_directory
from .services.safepath import is_reparse_point
from .system.port import ShortcutSpec
from .tomlio import dumps_tables

# The time stored for every ZIP entry, so that an export is reproducible byte for byte.
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class AuthoringError(ValueError):
    """Report an authoring command that was refused, and why."""


@dataclass(frozen=True)
class ExportResult:
    """The ZIP ``export_distribution`` wrote: where, its SHA-256 and how many entries it holds."""

    path: Path
    sha256: str
    entries: int


# ---------------------------------------------------------------------------
# pkg new
# ---------------------------------------------------------------------------


def new_package(parent: Path, name: str, version: str | None = None, track: str | None = None) -> list[Path]:
    """Write a starter for package *name* below *parent* and return the files written.

    Parameters
    ----------
    parent : Path
        The folder (a root, or any working folder) that receives ``<name>\\``.
    name : str
        The package name.
    version : str, optional
        Write the pinned version directory ``<name>\\<version>\\pkg.toml``.
    track : str, optional
        ``github:owner/repo``: write a tracker that follows the releases of that repository. Without *version* and
        *track* a tracker with placeholders is written.

    Raises
    ------
    AuthoringError
        If the name, version or repository is not valid, or a file to write already exists.
    """
    if not _PACKAGE_NAME.fullmatch(name) or not is_version_name(name):
        raise AuthoringError(f"'{name}' cannot be a package name: use letters, digits, '.', '_' and '-'")
    if version is not None and not is_version_name(version):
        raise AuthoringError(f"'{version}' cannot be a version name")
    repository = "owner/repo"
    if track is not None:
        kind, _, spec = track.partition(":")
        if kind != "github" or not _GITHUB_REPO.fullmatch(spec):
            raise AuthoringError("--track takes github:owner/repo (the other providers are written by hand, see `pkg check`)")
        repository = spec

    # Decide every file first, so nothing is written when one of them exists.
    package = Path(parent) / name
    files: dict[Path, str | bytes] = {}
    if version is not None:
        files[package / version / MANIFEST_NAME] = _version_starter(name, version)
        for launcher, command in VERSION_LAUNCHERS.items():
            files[package / version / launcher] = launcher_text(command).encode("ascii")
    if track is not None or version is None:
        files[package / TRACKER_DIR_NAME / MANIFEST_NAME] = _tracker_starter(name, repository)
        files[package / TRACKER_DIR_NAME / TRACKER_LAUNCHER] = launcher_text("check-updates").encode("ascii")
    for path in files:
        if os.path.lexists(path):
            raise AuthoringError(f"{path} exists; gupkg never overwrites a package definition")
    if is_reparse_point(package) or is_reparse_point(Path(parent)):
        raise AuthoringError(f"{package} or its folder is a link; refusing to write through it")

    # Write each starter; the folders are created only now that every check has passed.
    written: list[Path] = []
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _tracker_starter(name: str, repository: str) -> str:
    """Return the text of a starter tracker that follows the GitHub releases of *repository*."""
    return (
        "format = 2\n"
        f'description = "{name}"\n'
        f'project_url = "https://github.com/{repository}"\n'
        "\n"
        "# `gupkg check-updates` runs this tracker and writes each new version as a pinned directory.\n"
        "[track]\n"
        'provider = "github"\n'
        f'repo = "{repository}"\n'
        "# The release asset to download; ${version} is the version the tracker found.\n"
        f'asset = "{name}-${{version}}-win64.zip"\n'
        "\n"
        "[source]\n"
        'download_url = "${asset_url}"\n'
        "\n"
        "# Add shortcuts, environment variables, PATH entries and command shims here, for example:\n"
        "# [[shim]]\n"
        f'# name = "{name}"\n'
        f'# target = "$App\\\\{name}.exe"\n'
    )


def _version_starter(name: str, version: str) -> str:
    """Return the text of a starter pinned version directory for *name* *version*."""
    return (
        "format = 2\n"
        f'description = "{name} {version}"\n'
        "\n"
        "[source]\n"
        f'download_url = "https://example.org/{name}-{version}.zip"\n'
        "# gupkg refuses a download without a checksum: add the SHA-256 of the file once you have checked it.\n"
        '# sha256 = "..."\n'
        "\n"
        "# Add shortcuts, environment variables, PATH entries and command shims here, for example:\n"
        "# [[shim]]\n"
        f'# name = "{name}"\n'
        f'# target = "$App\\\\{name}.exe"\n'
    )


# ---------------------------------------------------------------------------
# pkg adopt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptResult:
    """The ``pkg.toml`` that ``adopt_version`` wrote, the shortcuts it kept and the shortcut files it skipped (file, reason)."""

    path: Path
    shortcuts: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def adopt_version(
    version_dir: Path,
    read_shortcut: Callable[[Path], ShortcutSpec],
    env: Mapping[str, str],
    *,
    shortcuts: Path | None = None,
    group: str = "Games",
    force: bool = False,
) -> AdoptResult:
    """Write the ``pkg.toml`` of a program that is already unpacked in ``<version_dir>\\App`` from the shortcuts its installer made.

    Every ``.lnk`` below the shortcuts folder that starts a program inside ``App`` becomes a ``[[shortcut]]`` of a shipped,
    format 2 package: its target, arguments, working directory (the program's folder when the link had none or one outside
    ``App``), icon (dropped when outside ``App``) and description are kept, and every path inside ``App`` is written as ``$App``.
    Uninstallers, links to files that are not programs, links outside ``App`` and links to missing files are skipped and
    reported. The package name is the name of the folder above *version_dir* and the version is the name of *version_dir*.

    Parameters
    ----------
    version_dir : Path
        The version directory ``<root>\\<name>\\<version>``; it must hold ``App``.
    read_shortcut : callable
        Returns the ``ShortcutSpec`` of a ``.lnk`` path (the machine's ``read_shortcut``).
    env : mapping
        The environment used to expand ``%VAR%`` in the targets of the links.
    shortcuts : Path, optional
        The folder that holds the links (default ``<version_dir>\\shortcuts``). It is only read.
    group : str
        The Start Menu folder of the new shortcuts (default ``Games``); empty puts them at the top.
    force : bool
        Replace an existing ``pkg.toml`` (kept as ``pkg.toml.old``) instead of refusing.

    Returns
    -------
    AdoptResult
        The file written, the shortcut names kept and the skipped links with the reason for each.

    Raises
    ------
    AuthoringError
        If the folders are missing or not valid names, ``pkg.toml`` exists without *force*, no link is usable, two links share a
        name, or the file written fails ``pkg check`` (the previous file is then put back).
    """
    # Validate the places first: nothing is written unless the version directory has the shape of a package.
    version_dir = Path(os.path.abspath(version_dir))
    app = version_dir / "App"
    package = version_dir.parent.name
    folder = Path(shortcuts) if shortcuts is not None else version_dir / "shortcuts"
    if not is_version_name(version_dir.name) or not is_version_name(package):
        raise AuthoringError(f"{version_dir} is not <root>\\<name>\\<version>: '{package}' and '{version_dir.name}' must be valid folder names")
    if not app.is_dir() or is_reparse_point(app):
        raise AuthoringError(f"{app} is not a folder: install the program into the App folder of the version first")
    if not folder.is_dir():
        raise AuthoringError(f"{folder} is not a folder: move the program's Start Menu shortcuts there first")
    manifest = version_dir / MANIFEST_NAME
    if os.path.lexists(manifest) and not force:
        raise AuthoringError(f"{manifest} exists; pass --force to replace it (the old one is kept as {MANIFEST_NAME}.old)")

    # Turn each link into a shortcut row or a reason for skipping it.
    links = sorted(item for item in folder.rglob("*") if item.suffix.lower() == ".lnk" and item.is_file())
    rows: list[dict[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for link in links:
        absorbed = _absorb_shortcut(link, read_shortcut, app, env, group)
        if isinstance(absorbed, str):
            skipped.append((str(link.relative_to(folder)), absorbed))
        else:
            rows.append(absorbed)
    if not rows:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in skipped) or "no .lnk files"
        raise AuthoringError(f"no usable shortcut in {folder} ({reasons})")
    names = [row["name"] for row in rows]
    for name in names:
        if names.count(name) > 1:
            raise AuthoringError(f"two shortcuts are both called '{name}': rename or remove one in {folder}")

    # Games are tens of gigabytes: the App fingerprint (a hash of every file) is left out of the receipt.
    data = {"format": 2, "description": package, "record_manifest": False, "source": {"shipped": True}, "shortcut": rows}
    text = f"# Written by `gupkg pkg adopt` from the shortcuts in {folder.name}\\.\n" + dumps_tables(data)

    _write_checked_manifest(version_dir, text)
    return AdoptResult(manifest, tuple(names), tuple(skipped))


def _absorb_shortcut(link: Path, read_shortcut: Callable[[Path], ShortcutSpec], app: Path, env: Mapping[str, str], group: str) -> dict[str, str] | str:
    """Decide what one installer link becomes: its ``[[shortcut]]`` row, or the reason (a string) it is left out.

    A link is kept only when it starts a program that exists inside *app*; unreadable links, links outside *app*, links to
    files that are not programs and uninstallers are reasons.
    """
    # A link that cannot be read is reported, not fatal: the other links of the folder may still be good.
    try:
        spec = read_shortcut(link)
    except (OSError, ValueError) as error:
        return f"cannot be read: {error}"

    # Only a program that lives in App can be started through the package.
    target = _inside_app(spec.target_path, app, env)
    if target is None:
        return f"its target is outside App ({spec.target_path})"
    program = os.path.basename(target).lower()
    if not program.endswith((".exe", ".bat", ".cmd")):
        return f"its target is not a program ({program})"
    if re.search(r"uninstall|(?<![a-z])unins\d*\.exe", f"{link.stem} {program}".lower()):
        return "it is an uninstaller"
    if not (app / target).is_file():
        return f"its target does not exist in App ({target})"
    return _shortcut_row(link.stem, group, target, spec, app, env)


def _write_checked_manifest(version_dir: Path, text: str) -> None:
    """Write *text* as the version's ``pkg.toml``; the old file is kept as ``pkg.toml.old`` and put back if the new one fails ``pkg check``."""
    manifest = version_dir / MANIFEST_NAME
    old = manifest.with_name(MANIFEST_NAME + ".old")

    # Move the old file aside rather than overwrite it, so every failure path below can restore it.
    replaced = os.path.lexists(manifest)
    if replaced:
        os.replace(manifest, old)
    manifest.write_text(text, encoding="utf-8", newline="\n")

    # A file that does not validate is never left behind: remove it and bring the old one back.
    report = check_directory(version_dir)
    if not report.ok:
        manifest.unlink()
        if replaced:
            os.replace(old, manifest)
        raise AuthoringError(f"the {MANIFEST_NAME} made from the shortcuts is not valid: " + "; ".join(report.errors))


def _inside_app(text: str, app: Path, env: Mapping[str, str]) -> str | None:
    """Return the path *text* relative to *app* (``%VAR%`` expanded from *env*; ``.`` for *app* itself), or ``None`` when it is not inside *app*."""
    lowered = {key.lower(): value for key, value in env.items()}
    expanded = re.sub(r"%([^%]+)%", lambda found: lowered.get(found.group(1).lower(), found.group(0)), text)
    if not os.path.isabs(expanded):
        return None

    # Compare resolved spellings: an installer may store the short (8.3) or the long name of the same folder.
    try:
        relative = os.path.relpath(os.path.realpath(expanded), os.path.realpath(app))
    except ValueError:  # another drive
        return None
    if relative == ".." or relative.startswith(".." + os.sep):
        return None
    return relative


def _shortcut_row(stem: str, group: str, target: str, spec: ShortcutSpec, app: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Return the ``[[shortcut]]`` row for one link: *target* is already relative to *app*; the other fields come from *spec*."""
    row = {"name": f"{group}\\{stem}" if group else stem, "target": "$App\\" + target}
    if spec.arguments:
        row["arguments"] = spec.arguments

    # A program usually has to start in its own folder: use the link's folder when it is inside App, else the program's.
    folder = _inside_app(spec.working_directory, app, env) if spec.working_directory else None
    if folder is None:
        folder = os.path.dirname(target)
    row["working_dir"] = "$App" if folder in ("", ".") else "$App\\" + folder

    # An icon is kept only when it is a file of App; the index after the last comma is kept with it.
    icon, _, index = spec.icon_location.rpartition(",") if "," in spec.icon_location else (spec.icon_location, "", "0")
    inside = _inside_app(icon, app, env) if icon else None
    if inside is not None:
        row["icon_location"] = f"$App\\{inside},{index}"
    if spec.description:
        row["description"] = spec.description
    return row


# ---------------------------------------------------------------------------
# pkg export
# ---------------------------------------------------------------------------


def export_distribution(version_dir: Path, output: Path) -> ExportResult:
    """Write a distribution ZIP of one version directory and return its path, SHA-256 and entry count.

    The ZIP holds ``<name>\\<version>\\...`` (the whole version directory, bundled gupkg and shipped ``App`` included) and the
    package's ``_check_updates`` folder when it has one, so it unpacks into a root as it stands (design 21.2). The version
    directory must pass ``gupkg pkg check`` and contain no link. A launcher the folder lacks (``install.cmd`` and ``gupkg-tui.cmd``,
    and ``check-updates.cmd`` in the tracker) is added to the ZIP from the template, so every distribution carries them.

    Raises
    ------
    AuthoringError
        If the directory is not a valid version directory, holds a link, or *output* exists.
    """
    version_dir = Path(version_dir).resolve()
    report = check_directory(version_dir)
    if not report.ok:
        raise AuthoringError(f"{version_dir} is not a valid version directory: " + "; ".join(report.errors))
    if os.path.lexists(output):
        raise AuthoringError(f"{output} exists; gupkg never overwrites a file")
    name, version = version_dir.parent.name, version_dir.name
    tracker = version_dir.parent / TRACKER_DIR_NAME

    # Collect every entry first (sorted, no link followed) so a refusal leaves no half-written ZIP.
    entries: list[tuple[Path | bytes, str]] = []
    for folder, prefix in ((version_dir, f"{name}/{version}"), *(((tracker, f"{name}/{TRACKER_DIR_NAME}"),) if tracker.is_dir() else ())):
        for current, folders, files in os.walk(folder):
            here = Path(current)
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a distribution never holds links")
            folders[:] = sorted(item for item in folders if item != "__pycache__")
            for item in sorted(files):
                if item.endswith(".pyc"):
                    continue
                relative = (here / item).relative_to(folder).as_posix()
                entries.append((here / item, f"{prefix}/{relative}"))

    # Add the launchers that the folders lack, generated from the template.
    present = {arcname for _source, arcname in entries}
    wanted = [(f"{name}/{version}/{launcher}", command) for launcher, command in VERSION_LAUNCHERS.items()]
    if tracker.is_dir():
        wanted.append((f"{name}/{TRACKER_DIR_NAME}/{TRACKER_LAUNCHER}", "check-updates"))
    for arcname, command in wanted:
        if arcname not in present:
            entries.append((launcher_text(command).encode("ascii"), arcname))
    entries.sort(key=lambda entry: entry[1])

    # Write the ZIP through a partial file and rename it, so the output appears whole or not at all.
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, arcname in entries:
            info = zipfile.ZipInfo(arcname, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            if isinstance(source, bytes):
                archive.writestr(info, source)
                continue
            with open(source, "rb") as handle, archive.open(info, "w") as sink:
                shutil.copyfileobj(handle, sink)
    os.replace(partial, output)
    digest = hashlib.sha256()
    with open(output, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return ExportResult(output, digest.hexdigest(), len(entries))


# ---------------------------------------------------------------------------
# pkg bundle
# ---------------------------------------------------------------------------


def bundle_gupkg(version_dir: Path, source: Path) -> int:
    """Copy the gupkg tree *source* into ``<version_dir>\\gupkg`` and return the number of files copied.

    The caller has verified *source* against its ``MANIFEST.sha256``. Links are refused in both places, bytecode and the
    per-install ``gupkg-config.toml`` are left out, and an existing ``gupkg`` folder is never replaced.

    Raises
    ------
    AuthoringError
        If *version_dir* is not a valid version directory, already has a ``gupkg`` folder, or *source* holds a link.
    """
    version_dir, source = Path(version_dir).resolve(), Path(source).resolve()
    report = check_directory(version_dir)
    if not report.ok:
        raise AuthoringError(f"{version_dir} is not a valid version directory: " + "; ".join(report.errors))
    destination = version_dir / "gupkg"
    if os.path.lexists(destination):
        raise AuthoringError(f"{destination} exists; gupkg never replaces a bundled copy")

    # Copy into a work folder beside the destination and rename it, so a failure leaves no partial bundle.
    work = version_dir / f".gupkg-bundle-{os.getpid()}"
    count = 0
    try:
        for current, folders, files in os.walk(source):
            here = Path(current)
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a bundle never holds links")
            folders[:] = [item for item in folders if item != "__pycache__"]
            relative = here.relative_to(source)
            (work / relative).mkdir(parents=True, exist_ok=True)
            for item in files:
                if item.endswith(".pyc") or item == "gupkg-config.toml":
                    continue
                shutil.copy2(here / item, work / relative / item)
                count += 1
        os.replace(work, destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # The program can now be installed from a double-click: give the version directory its launchers.
    write_launchers(version_dir)
    return count
