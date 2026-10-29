"""Build a pretend machine with published packages for the engine tests.

``World`` wraps the temporary machine of ``tests.mode_support`` (two roots, a settings loader, a ``FakeSystem``) and an
``Engine`` over it. ``World.publish`` writes a pinned version directory into a root and serves its zip artifact from the
fake's URL map, with the digest in ``pkg.toml`` when asked, so install, update, check-updates and repair run end to end on
real folders, real ``current`` junctions and the fake registry, shortcuts and ``PATH``.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

from gupkg.catalog.registry import registry_cache_dir
from gupkg.engine import lifecycle
from gupkg.engine.context import Engine
from gupkg.model.mode import ModeChoice, ModeContext, build_mode_context
from gupkg.model.plan import BatchResult, PackageResult
from gupkg.modes import default_folders
from gupkg.services.fingerprint import fingerprint_directory
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg.tomlio import write_toml
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


class ScriptDouble:
    """A populate script that is a process double: it records its calls and writes *files* into the ``App`` it is given.

    The double reads the JSON context from standard input, exactly as a ``.ps1``, ``.cmd`` or executable populate script
    would, and fills ``paths.app``. With a non-zero *returncode* it writes nothing and fails.
    """

    def __init__(self, files: dict[str, str], returncode: int = 0) -> None:
        self.files, self.returncode = files, returncode
        self.calls: list[dict] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Play the script: record the context and write the files."""
        context = json.loads(call.input_text or "{}")
        self.calls.append(context)
        if self.returncode:
            return ProcessResult(self.returncode, "", "the script failed")
        for relative, content in self.files.items():
            target = Path(context["paths"]["app"]) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ProcessResult(0, "populated\n")


class World:
    """A pretend machine with an engine, and helpers that publish packages into it.

    Parameters
    ----------
    base : Path
        The temporary folder everything lives under.
    elevated : bool
        Whether the fake machine reports an elevated process.
    """

    def __init__(self, base: Path, *, elevated: bool = False, untrusted: set[Path] | None = None) -> None:
        self.machine = Machine(base, elevated=elevated, untrusted=untrusted)
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

    def root(self, mode: str) -> Path:
        """Return the root of *mode*."""
        return {"user": self.user, "system": self.system_root}[mode]

    def mode_context(self, mode: str = "user") -> ModeContext:
        """Return the resolved install-mode context of *mode* on this machine (for tests that call the services directly)."""
        loaded = self.engine.load(system_mode=mode == "system")
        return build_mode_context(
            ModeChoice(mode, "test"), loaded.settings, self.machine.env, defaults=default_folders(self.fake),
        )

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

    def publish_shipped(
        self, name: str, version: str, *, mode: str = "user", files: dict[str, str] | None = None, download: bool = False,
        hashed: bool = True, tables: str = "", top: str = "", root: Path | None = None,
    ) -> Path:
        """Write a version whose ``App`` ships inside the version directory (with a ``download_url`` when *download* is true)."""
        url, digest = self.serve(name, version) if download else (None, None)
        folder = (root or self.root(mode)) / name / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pkg.toml").write_text(
            pkg_text(url, sha256=digest if hashed else None, top=top, tables=tables, shipped=True), encoding="utf-8"
        )
        for relative, content in (files if files is not None else {"app.exe": f"{name} {version} shipped\n"}).items():
            target = folder / "App" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return folder

    def publish_script(
        self, name: str, version: str, *, script: str = "populate.cmd", mode: str = "user", tables: str = "", top: str = "",
        root: Path | None = None, files: dict[str, str] | None = None, returncode: int = 0,
    ) -> "ScriptDouble":
        """Write a version whose ``App`` is filled by the populate script *script*, and register the process double that plays it."""
        folder = (root or self.root(mode)) / name / version
        (folder / "pkg.local").mkdir(parents=True, exist_ok=True)
        (folder / "pkg.local" / script).write_text("rem the double plays this script\n", encoding="utf-8")
        (folder / "pkg.toml").write_text(pkg_text(None, top=top, tables=tables, script=f"pkg.local/{script}"), encoding="utf-8")
        double = ScriptDouble(files if files is not None else {"app.exe": f"{name} {version} scripted\n"}, returncode)
        self.fake.processes[script.lower()] = double
        return double

    def tracker(
        self, name: str, *, mode: str = "user", tables: str = "", top: str = "", root: Path | None = None, checksum: bool = True,
        suffix: str = "zip",
    ) -> Path:
        """Write ``<root>/<name>/_check_updates`` with a ``json`` tracker that reads ``{"version": ...}`` from a URL.

        The version's download address is ``<BASE_URL>/<name>-${version}.zip`` and its checksum is found in the
        ``.sha256`` sidecar next to it (served by ``announce``).
        """
        folder = (root or self.root(mode)) / name / "_check_updates"
        folder.mkdir(parents=True, exist_ok=True)
        text = "\n".join(line for line in (
            "format = 2", 'description = "a tracked package"', top.rstrip(),
            "[track]", 'provider = "json"', f'check_url = "{BASE_URL}/{name}-latest.json"', 'version = "$.version"',
            "[source]", f'download_url = "{BASE_URL}/{name}-${{version}}.{suffix}"',
            *(("[source.checksum]", 'sidecar = "${download_url}.sha256"') if checksum else ()),
            tables.rstrip(),
        ) if line != "") + "\n"
        (folder / "pkg.toml").write_text(text, encoding="utf-8")
        return folder

    def announce(
        self, name: str, version: str, *, sidecar: bool = True, members: dict[str, str] | None = None, suffix: str = "zip"
    ) -> str:
        """Make the tracker of *name* find *version*: serve the version document, the artifact and (optionally) its sidecar."""
        self.fake.add_url(f"{BASE_URL}/{name}-latest.json", f'{{"version": "{version}"}}')
        url, digest = self.serve(name, version, members, suffix=suffix)
        if sidecar:
            self.fake.add_url(f"{url}.sha256", f"{digest}  {name}-{version}.{suffix}\n")
        return digest

    def registry(
        self, packages: dict[str, dict[str, object]], *, mode: str = "user", fingerprints: list[tuple[str, str]] = (),
        revision: str = "a" * 40, published_raw: dict[str, str] | None = None,
    ) -> Path:
        """Publish a registry cache named ``main`` for the packages of *mode* and return its cache folder.

        *packages* maps a name to ``{"versions": {version: members}, "tracker": text or None}``; each version is
        written pinned with its digest and served (a ``Path`` as *members* is copied as it is). *fingerprints* lists
        ``(name, version)`` pairs whose fingerprint ``_registry.toml`` publishes (computed from the files as written by
        ``gupkg.services.fingerprint``); *published_raw* adds ``{"name/version": fingerprint}`` entries as given.
        """
        cache = registry_cache_dir(self.root(mode), "main")
        tree = cache / "trees" / revision
        for name, spec in packages.items():
            folder = tree / "pkgs" / name
            folder.mkdir(parents=True, exist_ok=True)
            for version, members in dict(spec.get("versions", {})).items():
                if isinstance(members, Path):
                    shutil.copytree(members, folder / version)
                    continue
                url, digest = self.serve(name, version, members)
                (folder / version).mkdir(exist_ok=True)
                (folder / version / "pkg.toml").write_text(pkg_text(url, sha256=digest, tables=str(spec.get("tables", ""))), encoding="utf-8")
            if spec.get("tracker"):
                (folder / "_check_updates").mkdir(exist_ok=True)
                (folder / "_check_updates" / "pkg.toml").write_text(str(spec["tracker"]), encoding="utf-8")
        published = {f"{name}/{version}": fingerprint_directory(tree / "pkgs" / name / version) for name, version in fingerprints}
        published.update(published_raw or {})
        data: dict[str, object] = {"format": 2, "name": "main"}
        if published:
            data["fingerprints"] = published
        write_toml(tree / "registry.toml", data)
        write_toml(cache / "state.toml", {"revision": revision, "source": "test", "synchronized_at": "", "last_failure": ""})
        return cache

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
