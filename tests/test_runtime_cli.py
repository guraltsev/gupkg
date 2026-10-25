"""Cover standalone command information outside a package directory.

The real launcher ``src/gupkg/gupkg.cmd`` runs in a temporary non-package directory
on the committed bundled Python.  No package or manager data, network activity,
or Windows integration is involved.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRECTORY = ROOT / "src"
RELEASE_VERSION = re.search(
    r'(?m)^__version__ = "([^"]+)"',
    (RUNTIME_DIRECTORY / "gupkg" / "_version.py").read_text(encoding="utf-8"),
).group(1)


class RuntimeCommandTests(unittest.TestCase):
    """Verify command information that does not require a package selection."""

    def run_command(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run the real launcher, which starts the bundled Python with -B so no bytecode is written to the source tree."""
        return subprocess.run(
            [str(RUNTIME_DIRECTORY / "gupkg" / "gupkg.cmd"), *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_version_matches_the_declared_release(self) -> None:
        """The version command reports the runtime module's version, the only declaration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.run_command("--version", cwd=Path(tmpdir))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(
            result.stdout.strip().endswith(f" {RELEASE_VERSION}"),
            msg=result.stdout,
        )

    def test_manifests_do_not_repeat_the_folder_identity(self) -> None:
        """The checked-in manifests declare no name, version, or localVersion."""
        for manifest in (RUNTIME_DIRECTORY / "pkg.toml",):
            declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
            for key in ("name", "version", "localVersion"):
                self.assertNotIn(key, declared, msg=str(manifest))

    def test_help_exits_without_selecting_a_package(self) -> None:
        """The help command succeeds from a directory that contains no package."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.run_command("--help", cwd=Path(tmpdir))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Package manager for Windows", result.stdout)

    def test_pkg_check_accepts_a_version_directory_without_a_local_revision(self) -> None:
        """A version directory without ``.lN`` passes the authoring check, run through the real launcher."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "Example"
            version_directory = package_root / "1.2.3"
            app_directory = version_directory / "App"
            app_directory.mkdir(parents=True)
            (app_directory / "payload.txt").write_text("payload", encoding="utf-8")
            (version_directory / "pkg.toml").write_text('format = 2\ndescription = "Example"\n', encoding="utf-8")
            result = self.run_command("pkg", "check", str(version_directory), cwd=package_root)

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_the_migrate_forwarder_and_its_launcher_reach_the_migration_tool(self) -> None:
        """`gupkg migrate --help` and `gupkg-migrate.cmd --help` both print the migration tool's help."""
        with tempfile.TemporaryDirectory() as tmpdir:
            forwarded = self.run_command("migrate", "--help", cwd=Path(tmpdir))
            direct = subprocess.run(
                [str(RUNTIME_DIRECTORY / "gupkg" / "gupkg-migrate.cmd"), "--help"],
                cwd=tmpdir, capture_output=True, text=True, check=False,
            )

        for result in (forwarded, direct):
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("migrate", result.stdout.lower())
