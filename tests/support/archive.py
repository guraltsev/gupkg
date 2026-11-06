"""Build archives at test time for the unpack, populate and registry tests.

Zip archives are written with ``zipfile``, tarballs with ``tarfile`` and 7z archives with
the bundled 7-Zip run through the ``System`` the caller gives (a ``FakeSystem`` that lets the
real 7-Zip run). Nothing here is committed binary data.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from gupkg.extractors import find_7z
from gupkg.system.fake import FakeSystem
from gupkg.tomlio import dumps


def make_zip(path: Path, members: dict[str, bytes | str]) -> Path:
    """Write a zip archive of *members* (a name ending in ``/`` is a folder) and return its path.

    Parameters
    ----------
    path : Path
        Path of the archive to write.
    members : dict[str, bytes | str]
        Archive members as ``{name: content}``; a name ending in ``/`` is a folder.

    Returns
    -------
    Path
        *path*, the archive written.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def make_7z(system: FakeSystem, path: Path, files: dict[str, str], *options: str) -> Path:
    """Build a 7z archive of *files* with the bundled 7-Zip and return its path.

    Parameters
    ----------
    system : FakeSystem
        Fake machine that runs the bundled 7-Zip.
    path : Path
        Path of the archive to write.
    files : dict[str, str]
        File contents to archive, as ``{relative name: text}``.
    *options : str
        Additional 7-Zip command-line switches, such as encryption or solid-archive options.

    Returns
    -------
    Path
        *path*, the archive written.
    """
    source = path.parent / f"{path.stem}-src"
    for name, content in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = system.run([str(find_7z()), "a", "-bd", *options, str(path), "*"], cwd=source)
    assert result.returncode == 0, result.stdout
    return path


def make_tar(path: Path, members: list[tuple[str, bytes, str]], mode: str = "w") -> Path:
    """Write a tarball of ``(name, data, kind)`` members (kind ``file``, ``symlink`` or ``hardlink``).

    Parameters
    ----------
    path : Path
        Path of the tarball to write.
    members : list[tuple[str, bytes, str]]
        ``(name, data, kind)`` triples, where *kind* is ``file``, ``symlink`` or ``hardlink``.
    mode : str, optional
        Mode passed to ``tarfile.open``, which selects the compression.

    Returns
    -------
    Path
        *path*, the tarball written.
    """
    # Add each member as a regular file, a symlink or a hardlink, according to its kind.
    with tarfile.open(path, mode) as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
                archive.addfile(info)
            # Links carry no data; a regular file stores its bytes.
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, "a/file.txt"
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path


def tree(root: Path) -> dict[str, str]:
    """Return ``{relative posix path: text}`` of every file below *root*.

    Parameters
    ----------
    root : Path
        Folder to read.

    Returns
    -------
    dict[str, str]
        Text of every file below *root*, keyed by relative POSIX path.
    """
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def sums_manifest(files: dict[str, str]) -> str:
    """Return the text of a ``<sha256>  <path>`` manifest for *files*.

    Parameters
    ----------
    files : dict[str, str]
        File contents as ``{relative name: text}``.

    Returns
    -------
    str
        Manifest text with one ``<sha256>  <name>`` line per file, in name order.
    """
    return "".join(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n" for name, text in sorted(files.items()))


def registry_zip(files: dict[str, str]) -> bytes:
    """Return the ZIP of a registry repository with format 2 and *files*.

    Parameters
    ----------
    files : dict[str, str]
        Registry files as ``{path inside the repository: text}``.

    Returns
    -------
    bytes
        ZIP bytes of a repository folder named ``repo-main`` holding *files* and a format 2 registry file.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("repo-main/_registry.toml", dumps({"format": 2, "name": "main"}))
        for name, text in files.items():
            archive.writestr(f"repo-main/{name}", text)
    return buffer.getvalue()
