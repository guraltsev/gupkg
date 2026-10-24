"""Build a pretend machine with published packages for the engine tests.

``World`` wraps the temporary machine of ``tests.mode_support`` (three roots, a settings loader, a ``FakeSystem``) and an
``Engine`` over it. ``World.publish`` writes a pinned version directory into a root and serves its zip artifact from the
fake's URL map, with the digest in ``pkg.toml`` when asked, so install, update, check-updates and repair run end to end on
real folders, real ``current`` junctions and the fake registry, shortcuts and ``PATH``.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable
from pathlib import Path

from gupkg.engine import lifecycle
from gupkg.engine.context import Engine
from gupkg.model.plan import BatchResult, PackageResult, Request
from gupkg.system.fake import FakeSystem
from tests.mode_support import Machine, pin_no_ambient_elevation  # noqa: F401  (autouse fixture of every engine test)

BASE_URL = "https://example.org/files"


def zip_bytes(members: dict[str, str]) -> bytes:
    """Return a zip archive of text *members*."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def pkg_text(
    url: str | None, *, sha256: str | None = None, top: str = "", source: str = "", tables: str = "", shipped: bool | None = None,
    script: str | None = None,
) -> str:
    """Return the text of a pinned ``pkg.toml``: top-level keys, the ``[source]`` table, then further tables."""
    lines = ["format = 2", 'description = "a test package"', top.rstrip(), "[source]"]
    if url is not None:
        lines.append(f'download_url = "{url}"')
    if script is not None:
        lines.append(f'script = "{script}"')
    if sha256 is not None:
        lines.append(f'sha256 = "{sha256}"')
    if shipped is not None:
        lines.append(f"shipped = {'true' if shipped else 'false'}")
    lines.append(source.rstrip())
    lines.append(tables.rstrip())
    return "\n".join(line for line in lines if line != "") + "\n"


class World:
    """A pretend machine with an engine, and helpers that publish packages into it.

    Parameters
    ----------
    base : Path
        The temporary folder everything lives under.
    elevated : bool
        Whether the fake machine reports an elevated process.
    portable_configured : bool
        Whether the settings configure a portable root.
    """

    def __init__(self, base: Path, *, elevated: bool = False, portable_configured: bool = True, untrusted: set[Path] | None = None) -> None:
        self.machine = Machine(base, elevated=elevated, portable_configured=portable_configured, untrusted=untrusted)
        self.fake: FakeSystem = self.machine.fake
        self.base = base
        self.engine = Engine(
            system=self.fake, env=self.machine.env, load=self.machine.loader(), sleep=lambda seconds: None, retries=0,
        )

    # -- roots -------------------------------------------------------------------

    @property
    def user(self) -> Path:
        """The user root."""
        return self.machine.user_root

    @property
    def system_root(self) -> Path:
        """The system root."""
        return self.machine.system_root

    @property
    def portable(self) -> Path:
        """The portable root."""
        return self.machine.portable_root

    def root(self, mode: str) -> Path:
        """Return the root of *mode*."""
        return {"user": self.user, "system": self.system_root, "portable": self.portable}[mode]

    # -- publishing ---------------------------------------------------------------

    def serve(self, name: str, version: str, members: dict[str, str] | None = None, *, suffix: str = "zip") -> tuple[str, str]:
        """Serve a zip artifact for ``name`` ``version`` and return its URL and SHA-256."""
        url = f"{BASE_URL}/{name}-{version}.{suffix}"
        data = zip_bytes(members if members is not None else {"app.exe": f"{name} {version}\n"})
        self.fake.add_url(url, data)
        return url, hashlib.sha256(data).hexdigest()

    def publish(
        self,
        name: str,
        version: str,
        *,
        mode: str = "user",
        members: dict[str, str] | None = None,
        hashed: bool = True,
        top: str = "",
        source: str = "",
        tables: str = "",
        shipped: bool | None = None,
        root: Path | None = None,
    ) -> Path:
        """Write ``<root>/<name>/<version>`` with a pinned ``pkg.toml`` and serve its artifact; return the version folder."""
        url, digest = self.serve(name, version, members)
        folder = (root or self.root(mode)) / name / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pkg.toml").write_text(
            pkg_text(url, sha256=digest if hashed else None, top=top, source=source, tables=tables, shipped=shipped),
            encoding="utf-8",
        )
        return folder

    # -- running ------------------------------------------------------------------

    def run(self, operation: str, *selectors: str, **options) -> BatchResult:
        """Run one operation of the engine on *selectors* and return the batch result."""
        function = getattr(lifecycle, operation.replace("-", "_"))
        if operation == "switch":
            return function(self.engine, selectors[0], **options)
        return function(self.engine, list(selectors), **options)

    def one(self, operation: str, selector: str, **options) -> PackageResult:
        """Run one operation on one target and return its result."""
        batch = self.run(operation, selector, **options)
        assert len(batch.results) == 1
        return batch.results[0]


def shortcut_names(world: World) -> list[str]:
    """Return the names of the shortcuts the fake machine holds, sorted."""
    return sorted(path.stem for path in world.fake.shortcuts())


def requests(operation: str, selectors: list[str], **options) -> list[Request]:
    """Build requests for a batch."""
    return [Request(operation, item, **options) for item in selectors]


Collector = Callable[[object], None]
