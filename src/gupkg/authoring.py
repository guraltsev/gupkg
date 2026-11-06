"""Create, adopt, export and bundle package definitions (``gupkg pkg new``, ``pkg adopt``, ``pkg export`` and ``pkg bundle``).

These are the maintainer-side commands of the ``pkg`` group. ``new`` writes a starter version directory or tracker that
passes ``gupkg pkg check``; ``adopt`` writes the ``pkg.toml`` of a program that is already unpacked in ``App`` from the Start Menu
shortcuts its installer made (moved into a ``shortcuts`` folder beside ``App``, else found in the Start Menu); ``export`` writes a distribution ZIP of one version (``<name>\\<version>\\...`` plus the package's
``_check_updates`` when it has one) and reports its SHA-256; ``bundle`` copies the verified bundle of the running gupkg into a
version directory (together with its launchers) so the program can be installed without gupkg (design section 21). Nothing is
overwritten, links are never followed, and every command checks the directory it acts on first. The launchers (``install.cmd`` and
``manage.cmd`` of a version, ``check-updates.cmd`` of a tracker) are the files of the ``templates`` folder, copied by ``gupkg.launchers``.

Usage and API
-------------
``new_package(parent, name, version=None, track=None)`` returns the files it wrote. ``adopt_version(version_dir, read_shortcut,
env)`` returns an ``AdoptResult`` (the file written, the shortcuts kept, the ones skipped with a reason, and the launchers it added).
``add_launchers(version_dir, replace=True)`` copies the launchers into a valid version directory and returns the names written.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .launchers import TRACKER_LAUNCHER, VERSION_LAUNCHERS, launcher_text, write_launchers
from .model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name
from .pkgcheck import check_directory
from .services.safepath import is_reparse_point
from .system.port import ShortcutSpec
from .tomlio import dumps_tables

# The time stored for every ZIP entry, so that an export is reproducible byte for byte.
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
# The characters and length a package name may use.
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The shape of the ``owner/repo`` part of a ``github:owner/repo`` tracker.
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class AuthoringError(ValueError):
    """Report an authoring command that was refused, and why."""


@dataclass(frozen=True)
class ExportResult:
    """The ZIP ``export_distribution`` wrote: where, its SHA-256 and how many entries it holds.

    Attributes
    ----------
    path : Path
        The ZIP file that was written.
    sha256 : str
        The SHA-256 of the ZIP file.
    entries : int
        The number of entries in the ZIP file.
    """

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

    Returns
    -------
    list of Path
        The files written, in the order they were written.

    Raises
    ------
    AuthoringError
        If the name, version or repository is not valid, or a file to write already exists.
    """
    # Validate the name, version and tracker repository before deciding any file.
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

    # Refuse to overwrite anything, and refuse to write through a link.
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
    # The starter is one literal ``pkg.toml``; the commented lines show the user what else can be added.
    return (
        "format = 2\n"
        f'description = "{name}"\n'
        f'project_url = "https://github.com/{repository}"\n'
        "\n"
        "# `gupkg check-updates` runs this tracker and writes each new version as a pinned directory.\n"
        # The [track] table names the provider and the release asset to follow.
        "[track]\n"
        'provider = "github"\n'
        f'repo = "{repository}"\n'
        "# The release asset to download; ${version} is the version the tracker found.\n"
        f'asset = "{name}-${{version}}-win64.zip"\n'
        "\n"
        # The tracker's downloads come from the asset URL the provider found.
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
    # The starter is one literal ``pkg.toml``; the missing checksum is left for the user to fill in.
    return (
        "format = 2\n"
        f'description = "{name} {version}"\n'
        "\n"
        # A placeholder download URL, to be replaced by the real one.
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
    """The ``pkg.toml`` that ``adopt_version`` wrote, the shortcuts it kept and the shortcut files it skipped (file, reason).

    Attributes
    ----------
    path : Path
        The ``pkg.toml`` that was written.
    shortcuts : tuple[str, ...]
        The names of the shortcuts the manifest keeps.
    skipped : tuple[tuple[str, str], ...]
        ``(file, reason)`` pairs for the shortcut files that were not adopted.
    launchers : tuple[str, ...]
        The launcher files that were found next to the application.
    searched : tuple[Path, ...]
        The folders that were searched for shortcut files.
    read_from : Mapping[str, Path]
        Where each adopted piece of information was read from, by name.
    """

    path: Path
    shortcuts: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    launchers: tuple[str, ...] = ()
    searched: tuple[Path, ...] = ()
    read_from: Mapping[str, Path] = field(default_factory=dict)


def adopt_version(
    version_dir: Path,
    read_shortcut: Callable[[Path], ShortcutSpec],
    env: Mapping[str, str],
    *,
    shortcuts: Path | None = None,
    start_menus: Sequence[Path] = (),
    prefix: str = "",
    force: bool = False,
) -> AdoptResult:
    """Write the ``pkg.toml`` of a program that is already unpacked in ``<version_dir>\\App`` from the shortcuts its installer made.

    Every ``.lnk`` below the shortcuts folder that starts a program inside ``App`` becomes a ``[[shortcut]]`` of a shipped,
    format 2 package: its target, arguments, working directory (the program's folder when the link had none or one outside
    ``App``), icon (dropped when outside ``App``) and description are kept, and every path inside ``App`` is written as ``$App``.
    Uninstallers, links to files that are not programs, links outside ``App`` and links to missing files are skipped and
    reported. The package name is the name of the folder above *version_dir* and the version is the name of *version_dir*.

    When *shortcuts* is not given and ``<version_dir>\\shortcuts`` does not exist, the links below each of *start_menus* are
    used instead: only those into ``App`` count (the others belong to other programs and are not reported), and when none is
    found the file is written without shortcuts. Links that start the same program with the same arguments make one
    shortcut, the first found; the others are skipped with a reason.

    A link into ``current\\App`` or into the App of another version of the package is read as a link into this ``App``,
    and the result names the folder it was read from.

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
    start_menus : sequence of Path
        The Start Menu folders searched when there is no shortcuts folder (the user's and the machine's). They are only read.
    prefix : str
        A folder put before each shortcut name, relative to the root's Start Menu folder (default empty: none).
    force : bool
        Replace an existing ``pkg.toml`` (kept as ``pkg.toml.old``) instead of refusing.

    Returns
    -------
    AdoptResult
        The file written, the shortcut names kept, the skipped links with the reason for each, and the Start Menu folders
        searched (empty when a shortcuts folder was read).

    Raises
    ------
    AuthoringError
        If the folders are missing or not valid names, ``pkg.toml`` exists without *force*, no link of a shortcuts folder is
        usable, two different links share a name, or the file written fails ``pkg check`` (the previous file is then put back).
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
    # A missing default folder means "find them": the Start Menu links into App. A folder that was named must exist.
    searched = tuple(Path(menu) for menu in start_menus if shortcuts is None and not folder.is_dir())
    if not searched and not folder.is_dir():
        raise AuthoringError(f"{folder} is not a folder: move the program's Start Menu shortcuts there first")
    manifest = version_dir / MANIFEST_NAME
    if os.path.lexists(manifest) and not force:
        raise AuthoringError(f"{manifest} exists; pass --force to replace it (the old one is kept as {MANIFEST_NAME}.old)")

    # Turn each link into a shortcut row or a reason for skipping it. In the Start Menu most links start other programs:
    # those, and links that cannot be read, are none of this package's business and are not reported.
    bases = searched or (folder,)
    links = [(base, item) for base in bases if base.is_dir() and not is_reparse_point(base)
             for item in sorted(base.rglob("*")) if item.suffix.lower() == ".lnk" and item.is_file()]
    rows: list[dict[str, str]] = []
    skipped: list[tuple[str, str]] = []
    read_from: dict[str, Path] = {}
    started: dict[tuple[str, str], str] = {}

    # Classify every link in order, so the first link of a program wins and later duplicates are reported.
    for base, link in links:
        absorbed = _absorb_shortcut(link, read_shortcut, app, env, prefix)
        # Links that start the same program with the same arguments are one shortcut (an installer's link in both menus,
        # gupkg's own link into `current`): the first one found is kept.
        if not isinstance(absorbed, str):
            program = (absorbed[0]["target"].lower(), absorbed[0].get("arguments", ""))
            if program in started:
                absorbed = f"it starts the same program as shortcut {started[program]}"

        # A string is a reason to skip; links that cannot be read or lie outside App are silent in a Start Menu search.
        if isinstance(absorbed, str):
            if not searched or not absorbed.startswith(("cannot be read", "its target is outside App")):
                skipped.append((str(link) if searched else str(link.relative_to(base)), absorbed))
            continue

        # Keep the row, and remember which other App folder (if any) the link was read from.
        started[program] = absorbed[0]["name"]
        rows.append(absorbed[0])
        if absorbed[1] is not None:
            read_from[absorbed[0]["name"]] = absorbed[1]

    # A shortcuts folder with no usable link is an error; two links with one name cannot both become shortcuts.
    if not rows and not searched:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in skipped) or "no .lnk files"
        raise AuthoringError(f"no usable shortcut in {folder} ({reasons})")
    names = [row["name"] for row in rows]
    for name in names:
        if names.count(name) > 1:
            raise AuthoringError(f"two shortcuts are both called '{name}': rename or remove one in {folder}")

    # Games are tens of gigabytes: the App fingerprint (a hash of every file) is left out of the receipt.
    data: dict[str, object] = {"format": 2, "description": package, "record_manifest": False, "source": {"shipped": True}}
    if rows:
        data["shortcut"] = rows
    origin = "the Start Menu links into App" if searched else f"the shortcuts in {folder.name}\\"
    text = f"# Written by `gupkg pkg adopt` from {origin}.\n" + dumps_tables(data)

    _write_checked_manifest(version_dir, text)

    # The adopted program can be installed and managed from a double-click like any other version: add the launchers it lacks.
    return AdoptResult(manifest, tuple(names), tuple(skipped), tuple(add_launchers(version_dir, replace=False)), searched, read_from)


def add_launchers(version_dir: Path, *, replace: bool = True) -> list[str]:
    """Copy ``install.cmd`` and ``manage.cmd`` into a version directory and return the names written.

    Parameters
    ----------
    version_dir : Path
        A version directory that passes ``gupkg pkg check``.
    replace : bool
        Replace launchers that are already there (default); otherwise leave an existing launcher as it is.

    Returns
    -------
    list of str
        The launcher file names written, in the order of ``VERSION_LAUNCHERS``.

    Raises
    ------
    AuthoringError
        If the directory is not a valid version directory, or a launcher in it is a link.
    """
    # Refuse a directory that is not a valid version before writing into it.
    version_dir = Path(os.path.abspath(version_dir))
    report = check_directory(version_dir)
    if not report.ok:
        raise AuthoringError(f"{version_dir} is not a valid version directory: " + "; ".join(report.errors))

    # Write each launcher that is missing (or all of them when replacing), never through a link.
    names = []
    for name in VERSION_LAUNCHERS:
        target = version_dir / name
        if is_reparse_point(target):
            raise AuthoringError(f"{target} is a link; gupkg never writes through a link")
        if os.path.lexists(target) and not replace:
            continue
        write_launchers(version_dir, (name,))
        names.append(name)
    return names


def _absorb_shortcut(
    link: Path, read_shortcut: Callable[[Path], ShortcutSpec], app: Path, env: Mapping[str, str], prefix: str,
) -> tuple[dict[str, str], Path | None] | str:
    """Decide what one installer link becomes: its ``[[shortcut]]`` row, or the reason (a string) it is left out.

    A link is kept only when it starts a program that exists inside *app*; unreadable links, links outside *app*, links to
    files that are not programs and uninstallers are reasons. A kept link comes with the other App folder of the package
    (``current`` or another version) its target was read from, or ``None``.

    Parameters
    ----------
    link : Path
        The ``.lnk`` file.
    read_shortcut : callable
        Returns the ``ShortcutSpec`` of a ``.lnk`` path.
    app : Path
        The ``App`` folder of the version being adopted.
    env : mapping
        The environment used to expand ``%VAR%`` in the link's paths.
    prefix : str
        A folder put before the shortcut name.
    """
    # A link that cannot be read is reported, not fatal: the other links of the folder may still be good.
    try:
        spec = read_shortcut(link)
    except (OSError, ValueError) as error:
        return f"cannot be read: {error}"

    # Only a program that lives in App can be started through the package.
    located = _locate_in_app(spec.target_path, app, env)
    if located is None:
        return f"its target is outside App ({spec.target_path})"
    target, other_app = located

    # Keep only programs that exist and are not uninstallers.
    program = os.path.basename(target).lower()
    if not program.endswith((".exe", ".bat", ".cmd")):
        return f"its target is not a program ({program})"
    if re.search(r"uninstall|(?<![a-z])unins\d*\.exe", f"{link.stem} {program}".lower()):
        return "it is an uninstaller"
    if not (app / target).is_file():
        return f"its target does not exist in App ({target})"
    return _shortcut_row(link.stem, prefix, target, spec, app, env), other_app


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
    located = _locate_in_app(text, app, env)
    return located[0] if located is not None else None


def _locate_in_app(text: str, app: Path, env: Mapping[str, str]) -> tuple[str, Path | None] | None:
    """Return the path *text* relative to *app*, and the other App folder it was read from (``None`` when it is *app* itself).

    A path into ``current\\App`` or into the App of another version of the same package is read as the same path in
    *app*: links made for an earlier install of the package still name the right program. ``None`` when it is neither.
    """
    lowered = {key.lower(): value for key, value in env.items()}
    expanded = re.sub(r"%([^%]+)%", lambda found: lowered.get(found.group(1).lower(), found.group(0)), text)
    if not os.path.isabs(expanded):
        return None

    # Compare resolved spellings: an installer may store the short (8.3) or the long name of the same folder.
    try:
        relative = os.path.relpath(os.path.realpath(expanded), os.path.realpath(app))
    except ValueError:  # another drive
        return None
    if relative != ".." and not relative.startswith(".." + os.sep):
        return relative, None

    # Then <package>\<other version or current>\App\...: the written spelling first, so a link to `current` is reported as such.
    for spelling in (os.path.abspath(expanded), os.path.realpath(expanded)):
        for package in (os.path.abspath(app.parent.parent), os.path.realpath(app.parent.parent)):
            parts = Path(os.path.relpath(spelling, package)).parts
            if len(parts) >= 2 and parts[0] != ".." and parts[1].lower() == "app":
                return os.path.join(*parts[2:]) if len(parts) > 2 else ".", Path(package, *parts[:2])
    return None


def _shortcut_row(stem: str, prefix: str, target: str, spec: ShortcutSpec, app: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Return the ``[[shortcut]]`` row for one link: *target* is already relative to *app*; the other fields come from *spec*."""
    row = {"name": f"{prefix}\\{stem}" if prefix else stem, "target": "$App\\" + target}
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
    directory must pass ``gupkg pkg check`` and contain no link. A launcher the folder lacks (``install.cmd`` and ``manage.cmd``,
    and ``check-updates.cmd`` in the tracker) is added to the ZIP from the templates, so every distribution carries them.

    Parameters
    ----------
    version_dir : Path
        The version directory ``<root>\\<name>\\<version>`` to export.
    output : Path
        The ZIP file to create; it must not exist.

    Returns
    -------
    ExportResult
        The ZIP's path, its SHA-256 and the number of entries.

    Raises
    ------
    AuthoringError
        If the directory is not a valid version directory, holds a link, or *output* exists.
    """
    # Refuse an invalid version directory or an existing output before reading the tree.
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
            # A link anywhere in the tree refuses the export; bytecode folders are pruned from the walk.
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a distribution never holds links")
            folders[:] = sorted(item for item in folders if item != "__pycache__")

            # Record each file with its archive name, in sorted order.
            for item in sorted(files):
                if item.endswith(".pyc"):
                    continue
                relative = (here / item).relative_to(folder).as_posix()
                entries.append((here / item, f"{prefix}/{relative}"))

    # Add the launchers that the folders lack, copied from the templates.
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
        # A fixed timestamp and mode on every entry make the same tree give the same bytes.
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

    # The digest is computed from the file as written, not from the entries.
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

    Parameters
    ----------
    version_dir : Path
        A valid version directory that receives the ``gupkg`` folder and its launchers.
    source : Path
        The verified gupkg tree to copy.

    Returns
    -------
    int
        The number of files copied.

    Raises
    ------
    AuthoringError
        If *version_dir* is not a valid version directory, already has a ``gupkg`` folder, or *source* holds a link.
    """
    # Refuse an invalid version directory or an existing bundle before copying anything.
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
            # A link anywhere in the source refuses the bundle; bytecode folders are pruned from the walk.
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a bundle never holds links")
            folders[:] = [item for item in folders if item != "__pycache__"]

            # Mirror the folder, then copy its files except bytecode and per-install state.
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
