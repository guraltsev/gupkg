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


def make_zip(path: Path, members: dict[str, bytes | str]) -> Path:
    """Write a zip archive of *members* (a name ending in ``/`` is a folder) and return its path."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def make_7z(system: FakeSystem, path: Path, files: dict[str, str], *options: str) -> Path:
    """Build a 7z archive of *files* with the bundled 7-Zip and return its path."""
    source = path.parent / f"{path.stem}-src"
    for name, content in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = system.run([str(find_7z()), "a", "-bd", *options, str(path), "*"], cwd=source)
    assert result.returncode == 0, result.stdout
    return path


def make_tar(path: Path, members: list[tuple[str, bytes, str]], mode: str = "w") -> Path:
    """Write a tarball of ``(name, data, kind)`` members (kind ``file``, ``symlink`` or ``hardlink``)."""
    with tarfile.open(path, mode) as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
                archive.addfile(info)
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, "a/file.txt"
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path


def tree(root: Path) -> dict[str, str]:
    """Return ``{relative posix path: text}`` of every file below *root*."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def sums_manifest(files: dict[str, str]) -> str:
    """Return the text of a ``<sha256>  <path>`` manifest for *files*."""
    return "".join(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n" for name, text in sorted(files.items()))
