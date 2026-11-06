"""Build a pretend machine with published packages for the engine tests.

``World`` wraps the temporary machine of ``tests.support.mode`` (two roots, an app settings file, a ``FakeSystem``) and an
``Engine`` over the one scope of a run on it (user mode, or system mode when the fake machine is elevated). ``World.publish`` writes a pinned version directory into a root and serves its zip artifact from the
fake's URL map, with the digest in ``pkg.toml`` when asked, so install, update, check-updates and repair run end to end on
real folders, real ``current`` junctions and the fake registry, shortcuts and ``PATH``.

Usage and API
-------------
Use the ``world`` and ``elevated_world`` fixtures, or construct ``World``; ``publish``, ``publish_shipped``,
``publish_script``, ``tracker`` and ``registry`` write packages, and ``run`` and ``one`` call the lifecycle operations.

Implementation Approach
-----------------------
Packages are written as real format 2 version directories and their artifacts are served from the fake machine's URL
map, so the engine runs every phase on real folders while the network, the registry and the Start Menu stay pretend.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

from gupkg.catalog.registry import registry_cache_dir
from gupkg.engine import lifecycle
from gupkg.engine.context import Engine
from gupkg.model.scope import Scope
from gupkg.model.plan import BatchResult, PackageResult
from gupkg.services.fingerprint import fingerprint_directory
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg.tomlio import write_toml
from tests.support.mode import Machine

# Address prefix under which every artifact of the pretend machine is served.
BASE_URL = "https://example.org/files"


def zip_bytes(members: dict[str, str]) -> bytes:
    """Return a zip archive of text *members*.

    Parameters
    ----------
    members : dict[str, str]
        Archive members as ``{name: text}``.

    Returns
    -------
    bytes
        Bytes of the archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def pkg_text(
    url: str | None, *, sha256: str | None = None, top: str = "", source: str = "", tables: str = "", shipped: bool | None = None,
    script: str | None = None,
) -> str:
    """Return the text of a pinned ``pkg.toml``: top-level keys, the ``[source]`` table, then further tables.

    Parameters
    ----------
    url : str | None
        Download address; the key is omitted when ``None``.
    sha256 : str | None, optional
        Declared digest; omitted when ``None``.
    top : str, optional
        Extra top-level keys.
    source : str, optional
        Extra keys of the ``[source]`` table.
    tables : str, optional
        Further tables, appended as given.
    shipped : bool | None, optional
        Value of ``shipped`` in ``[source]``; omitted when ``None``.
    script : str | None, optional
        Populate script path for ``[source]``; omitted when ``None``.

    Returns
    -------
    str
        Text of the manifest.
    """
    # Assemble the manifest line by line; the optional keys appear only when asked for.
    lines = ["format = 2", 'description = "a test package"', top.rstrip(), "[source]"]
    if url is not None:
        lines.append(f'download_url = "{url}"')
    if script is not None:
        lines.append(f'script = "{script}"')
    if sha256 is not None:
        lines.append(f'sha256 = "{sha256}"')
    # The ``shipped`` flag and the trailing free-form text close the manifest.
    if shipped is not None:
        lines.append(f"shipped = {'true' if shipped else 'false'}")
    lines.append(source.rstrip())
    lines.append(tables.rstrip())
    return "\n".join(line for line in lines if line != "") + "\n"


class ScriptDouble:
    """A populate script that is a process double: it records its calls and writes *files* into the ``App`` it is given.

    The double reads the JSON context from standard input, exactly as a ``.ps1``, ``.cmd`` or executable populate script
    would, and fills ``paths.app``. With a non-zero *returncode* it writes nothing and fails.

    Parameters
    ----------
    files : dict[str, str]
        Files written into ``App``, as ``{path: text}``.
    returncode : int, optional
        Exit code; non-zero makes the script write nothing and fail.
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

    Methods
    -------
    root
        Returns the root of a mode.
    write_app_settings
        Rewrites the app settings and rebuilds the scope.
    rescope
        Rebuilds the engine's scope.
    mode_context
        Returns the scope of the run.
    serve
        Serves a zip artifact.
    publish
        Publishes a pinned version.
    publish_shipped
        Publishes a version whose ``App`` ships with it.
    publish_script
        Publishes a version filled by a populate script.
    tracker
        Writes a ``json`` tracker.
    announce
        Makes a tracker find a version.
    registry
        Publishes a registry cache.
    run
        Runs an engine operation on selectors.
    one
        Runs an engine operation on one target.
    """

    def __init__(self, base: Path, *, elevated: bool = False, untrusted: set[Path] | None = None) -> None:
        self.machine = Machine(base, elevated=elevated, untrusted=untrusted)
        self.fake: FakeSystem = self.machine.fake
        self.base = base
        self.engine = Engine(
            system=self.fake, env=self.machine.env, scope=self.machine.scope(), sleep=lambda seconds: None, retries=0,
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
        """Return the root of *mode*.

        Parameters
        ----------
        mode : str
            ``user`` or ``system``.

        Returns
        -------
        Path
            The root folder of that mode.
        """
        return {"user": self.user, "system": self.system_root}[mode]

    def write_app_settings(self, top: str = "", tables: str = "") -> Scope:
        """Rewrite ``$App\\settings.toml`` and build the scope again (a run reads its settings once, when it starts).

        Parameters
        ----------
        top : str, optional
            Extra top-level keys, such as ``schema``.
        tables : str, optional
            Extra tables written after ``[roots]``.

        Returns
        -------
        Scope
            The rebuilt scope.
        """
        self.machine.write_app_settings(top, tables)
        return self.rescope()

    def rescope(self, **options) -> Scope:
        """Build the scope again (after the test changed a settings file) and give it to the engine; return it.

        Returns
        -------
        Scope
            The rebuilt scope.
        """
        self.engine.scope = self.machine.scope(**options)
        return self.engine.scope

    @property
    def scope(self) -> Scope:
        """The scope of the run (for tests that call the services directly)."""
        return self.engine.scope

    def mode_context(self, mode: str | None = None) -> Scope:
        """Return the scope of the run; *mode*, when given, must be the mode of this world's run.

        Parameters
        ----------
        mode : str | None, optional
            Mode the caller expects the run to have.

        Returns
        -------
        Scope
            The scope of the run.
        """
        assert mode is None or mode == self.scope.mode, f"this world runs in {self.scope.mode} mode, not {mode}"
        return self.scope

    # -- publishing ---------------------------------------------------------------

    def serve(self, name: str, version: str, members: dict[str, str] | None = None, *, suffix: str = "zip") -> tuple[str, str]:
        """Serve a zip artifact for ``name`` ``version`` and return its URL and SHA-256.

        Parameters
        ----------
        name : str
            Package name.
        version : str
            Version label.
        members : dict[str, str] | None, optional
            Archive members as ``{name: text}``; a default one-file archive when ``None``.
        suffix : str, optional
            File-name extension of the artifact.

        Returns
        -------
        tuple[str, str]
            The artifact address and its SHA-256.
        """
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
        """Write ``<root>/<name>/<version>`` with a pinned ``pkg.toml`` and serve its artifact; return the version folder.

        Parameters
        ----------
        name : str
            Package name.
        version : str
            Version label.
        mode : str, optional
            Mode whose root receives the version.
        members : dict[str, str] | None, optional
            Archive members, as for ``serve``.
        hashed : bool, optional
            Whether ``pkg.toml`` declares the digest.
        top : str, optional
            Extra top-level keys.
        source : str, optional
            Extra keys of the ``[source]`` table.
        tables : str, optional
            Further tables.
        shipped : bool | None, optional
            Value of ``shipped`` in ``[source]``; omitted when ``None``.
        root : Path | None, optional
            Root folder to use instead of the mode's root.

        Returns
        -------
        Path
            The version folder.
        """
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
        """Write a version whose ``App`` ships inside the version directory (with a ``download_url`` when *download* is true).

        Parameters
        ----------
        name : str
            Package name.
        version : str
            Version label.
        mode : str, optional
            Mode whose root receives the version.
        files : dict[str, str] | None, optional
            Files placed in ``App``, as ``{path: text}``.
        download : bool, optional
            Whether the version also declares a download address.
        hashed : bool, optional
            Whether ``pkg.toml`` declares the digest of that download.
        tables : str, optional
            Further tables.
        top : str, optional
            Extra top-level keys.
        root : Path | None, optional
            Root folder to use instead of the mode's root.

        Returns
        -------
        Path
            The version folder.
        """
        # Serve the artifact only when the version also declares a download address.
        url, digest = self.serve(name, version) if download else (None, None)
        folder = (root or self.root(mode)) / name / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pkg.toml").write_text(
            pkg_text(url, sha256=digest if hashed else None, top=top, tables=tables, shipped=True), encoding="utf-8"
        )
        # Ship the version's ``App`` files inside the version directory.
        for relative, content in (files if files is not None else {"app.exe": f"{name} {version} shipped\n"}).items():
            target = folder / "App" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return folder

    def publish_script(
        self, name: str, version: str, *, script: str = "populate.cmd", mode: str = "user", tables: str = "", top: str = "",
        root: Path | None = None, files: dict[str, str] | None = None, returncode: int = 0,
    ) -> "ScriptDouble":
        """Write a version whose ``App`` is filled by the populate script *script*, and register the process double that plays it.

        Parameters
        ----------
        name : str
            Package name.
        version : str
            Version label.
        script : str, optional
            File name of the populate script in ``pkg.local``.
        mode : str, optional
            Mode whose root receives the version.
        tables : str, optional
            Further tables.
        top : str, optional
            Extra top-level keys.
        root : Path | None, optional
            Root folder to use instead of the mode's root.
        files : dict[str, str] | None, optional
            Files the script writes into ``App``, as ``{path: text}``.
        returncode : int, optional
            Exit code of the script; non-zero makes it fail without writing.

        Returns
        -------
        'ScriptDouble'
            The process double registered for the script.
        """
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

        Parameters
        ----------
        name : str
            Package name.
        mode : str, optional
            Mode whose root receives the tracker.
        tables : str, optional
            Further tables.
        top : str, optional
            Extra top-level keys.
        root : Path | None, optional
            Root folder to use instead of the mode's root.
        checksum : bool, optional
            Whether the tracker reads the digest from a sidecar file.
        suffix : str, optional
            File-name extension of the artifacts.

        Returns
        -------
        Path
            The tracker folder.
        """
        # Create the tracker folder that the registry would publish beside the versions.
        folder = (root or self.root(mode)) / name / "_check_updates"
        folder.mkdir(parents=True, exist_ok=True)
        # Write a ``json`` tracker whose artifacts are found by version substitution.
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
        """Make the tracker of *name* find *version*: serve the version document, the artifact and (optionally) its sidecar.

        Parameters
        ----------
        name : str
            Package name.
        version : str
            Version the tracker should find.
        sidecar : bool, optional
            Whether to serve the ``.sha256`` sidecar of the artifact.
        members : dict[str, str] | None, optional
            Archive members, as for ``serve``.
        suffix : str, optional
            File-name extension of the artifact.

        Returns
        -------
        str
            SHA-256 of the served artifact.
        """
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

        Parameters
        ----------
        packages : dict[str, dict[str, object]]
            Maps a name to ``{"versions": {version: members}, "tracker": text or None}``.
        mode : str, optional
            Mode whose root holds the cache.
        fingerprints : list[tuple[str, str]], optional
            ``(name, version)`` pairs whose fingerprints the registry file publishes.
        revision : str, optional
            Revision recorded in the cache.
        published_raw : dict[str, str] | None, optional
            Extra ``{"name/version": fingerprint}`` entries published as given.

        Returns
        -------
        Path
            The cache folder.
        """
        # Start the registry cache at the revision tree a sync would publish.
        cache = registry_cache_dir(self.root(mode), "main")
        tree = cache / "trees" / revision
        # Write each package's versions, pinned and served, then its tracker.
        for name, spec in packages.items():
            folder = tree / "pkgs" / name
            folder.mkdir(parents=True, exist_ok=True)
            # Copy a ready-made version folder as it is; otherwise serve and pin a new version.
            for version, members in dict(spec.get("versions", {})).items():
                if isinstance(members, Path):
                    shutil.copytree(members, folder / version)
                    continue
                url, digest = self.serve(name, version, members)
                (folder / version).mkdir(exist_ok=True)
                (folder / version / "pkg.toml").write_text(pkg_text(url, sha256=digest, tables=str(spec.get("tables", ""))), encoding="utf-8")
            # Add the package's tracker text when it has one.
            if spec.get("tracker"):
                (folder / "_check_updates").mkdir(exist_ok=True)
                (folder / "_check_updates" / "pkg.toml").write_text(str(spec["tracker"]), encoding="utf-8")
        # Compute the published fingerprints of the requested versions and merge the raw ones.
        published = {f"{name}/{version}": fingerprint_directory(tree / "pkgs" / name / version) for name, version in fingerprints}
        published.update(published_raw or {})
        data: dict[str, object] = {"format": 2, "name": "main"}
        if published:
            data["fingerprints"] = published
        # Write the registry file and the state file that mark the cache as synchronized.
        write_toml(tree / "registry.toml", data)
        write_toml(cache / "state.toml", {"revision": revision, "source": "test", "synchronized_at": "", "last_failure": ""})
        return cache

    # -- running ------------------------------------------------------------------

    def run(self, operation: str, *selectors: str, **options) -> BatchResult:
        """Run one operation of the engine on *selectors* and return the batch result.

        Parameters
        ----------
        operation : str
            Name of the lifecycle operation, such as ``update`` or ``switch``.
        *selectors : str
            Package selectors the operation acts on.
        **options : object
            Keyword options passed to the operation.

        Returns
        -------
        BatchResult
            The batch result.
        """
        function = getattr(lifecycle, operation.replace("-", "_"))
        if operation == "switch":
            return function(self.engine, selectors[0], **options)
        return function(self.engine, list(selectors), **options)

    def one(self, operation: str, selector: str, **options) -> PackageResult:
        """Run one operation on one target and return its result.

        Parameters
        ----------
        operation : str
            Name of the lifecycle operation.
        selector : str
            The one package selector.
        **options : object
            Keyword options passed to the operation.

        Returns
        -------
        PackageResult
            The result for that target.
        """
        batch = self.run(operation, selector, **options)
        assert len(batch.results) == 1
        return batch.results[0]


def current(package: Path) -> str:
    """Return the version that ``<package>/current`` points at.

    Parameters
    ----------
    package : Path
        Package folder.

    Returns
    -------
    str
        Name of the version folder that ``current`` resolves to.
    """
    return Path(os.path.realpath(package / "current")).name


def replace_declared_sha256(version_dir: Path, replacement: str | None = None, *, length: int = 64) -> None:
    """Overwrite the first *length* characters of the ``sha256`` that ``<version_dir>/pkg.toml`` declares with *replacement*.

    Without *replacement* the digest becomes a different, well-formed one (64 zeros, or 64 ones when it already is 64 zeros), so
    the download no longer matches it.

    Parameters
    ----------
    version_dir : Path
        Version folder whose ``pkg.toml`` is changed.
    replacement : str | None, optional
        Text to write; a different well-formed digest when ``None``.
    length : int, optional
        Number of characters of the digest to overwrite.
    """
    manifest = version_dir / "pkg.toml"
    text = manifest.read_text(encoding="utf-8")
    start = text.index('sha256 = "') + len('sha256 = "')
    if replacement is None:
        replacement = "0" * 64 if text[start:start + 64] != "0" * 64 else "1" * 64
    manifest.write_text(text[:start] + replacement + text[start + length:], encoding="utf-8")
