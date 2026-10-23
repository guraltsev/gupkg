"""Cover user-visible output from package migration helper scripts.

Checked-in legacy fixtures and temporary TOML files are real; the converted
package is installed against a ``FakeSystem`` and real ``.lnk`` files are
written and read through ``WindowsSystem`` below temporary directories. Windows
shortcut inspection is mocked where required. Exact helper call graphs and
internal normalization structures are out of scope.
"""

from __future__ import annotations

import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest import mock

from gupkg.core import Scope
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ShortcutSpec
from gupkg.system.windows import WindowsSystem

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
LEGACY_CONVERTER = SRC_ROOT / "gupkg" / "legacy_to_gupkg_toml.py"
SHORTCUT_IMPORTER = SRC_ROOT / "gupkg" / "shortcuts_to_gupkg_toml.py"
EXAMPLES_ROOT = ROOT / "tests" / "fixtures" / "legacy_examples"


def load_gupkg_module():
    """Return the supported workflow facade with the public CLI attached."""
    from gupkg import cli
    from gupkg import gupkg as module

    module.main = cli.main
    return module


def load_shortcut_importer_module():
    spec = importlib.util.spec_from_file_location(
        "shortcuts_to_gupkg_toml_under_test", SHORTCUT_IMPORTER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class LegacyConverterTests(unittest.TestCase):
    def run_converter(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LEGACY_CONVERTER), *args],
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_main(self, module, args: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            try:
                code = module.main(args)
            except SystemExit as exc:
                code = exc.code
        if code is None:
            code = 0
        self.assertIsInstance(code, int)
        return code, stdout.getvalue()

    def test_converter_writes_canonical_toml_that_gupkg_accepts(self) -> None:
        """Converter writes canonical toml that gupkg accepts."""
        module = load_gupkg_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "GoodApp" / "1.2.3.local.1"
            version_dir.mkdir(parents=True)
            (version_dir / "opt_pkg.json").write_text(
                json.dumps(
                    {
                        "name": "GoodApp",
                        "version": "1.2.3",
                        "local_version": "1",
                        "portable": False,
                        "description": "Good test package",
                        "homepage": "https://example.invalid/goodapp",
                        "download_url": "https://example.invalid/goodapp.zip",
                        "path": ["$AppPath\\App", "$AppPath\\Tools"],
                        "env": [
                            {"name": "GOODAPP_HOME", "value": "$AppPath\\App"},
                        ],
                        "shortcut": [
                            {
                                "name": "Good App.lnk",
                                "target_path": "$AppPath\\App\\good.exe",
                                "working_directory": "$AppPath\\App",
                            }
                        ],
                        "bin": [
                            {
                                "name": "good.cmd",
                                "content": '@echo off\r\n"$AppPath\\App\\good.exe" %*\r\n',
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            gupkg_toml = version_dir / "pkg.toml"
            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(gupkg_toml)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(gupkg_toml.exists())

            rendered = gupkg_toml.read_text(encoding="utf-8")
            parsed = tomllib.loads(rendered)
            self.assertNotIn("[[main]]", rendered)
            self.assertNotIn("localVersion", parsed)
            self.assertFalse(parsed["only_portable"])
            self.assertIn("[[environment]]", rendered)
            self.assertIn('Name = "GOODAPP_HOME"', rendered)
            self.assertIn('targetPath = "$App\\\\good.exe"', rendered)
            self.assertEqual(parsed["description"], "Good test package")
            self.assertEqual(parsed["homepage"], "https://example.invalid/goodapp")
            self.assertEqual(
                parsed["origin"]["url"], "https://example.invalid/goodapp.zip"
            )
            self.assertEqual(
                [entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"]
            )
            self.assertEqual(parsed["environment"][0]["Name"], "GOODAPP_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["name"], "Good App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\good.exe")
            self.assertEqual(parsed["shortcut"][0]["workingDirectory"], "$App")
            self.assertEqual(parsed["bin"][0]["name"], "good.cmd")
            self.assertIn("$App\\good.exe", parsed["bin"][0]["content"])

            (version_dir / "App").mkdir()
            (version_dir / "App" / "good.exe").write_text("", encoding="utf-8")
            system = FakeSystem(Path(tmpdir) / "machine")
            output = io.StringIO()
            with redirect_stdout(output):
                code = module.main(["--scope", "user", "install", str(version_dir)], system=system)

            self.assertEqual(code, module.EXIT_SUCCESS, msg=output.getvalue())
            self.assertEqual(
                system.env_values(Scope.USER)["GOODAPP_HOME"].value,
                str(version_dir.parent / "current" / "App"),
            )
            self.assertEqual(
                [path.name for path in system.shortcuts()], ["Good App.lnk"]
            )
            self.assertTrue((system.scope_folders(Scope.USER).bin_dir / "good.cmd").is_file())

    def test_converter_can_infer_metadata_from_directory_name(self) -> None:
        """Converter can infer metadata from directory name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Tool-portable" / "2.0.1.local.7"
            version_dir.mkdir(parents=True)
            (version_dir / "shortcut.json").write_text(
                json.dumps(
                    {
                        "shortcut": [
                            {
                                "name": "Tool",
                                "targetPath": "$AppPath\\App\\tool.exe",
                            }
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

            rendered = result.stdout
            parsed = tomllib.loads(rendered)
            self.assertFalse({"name", "version", "localVersion"} & parsed.keys())
            self.assertTrue(parsed["only_portable"])
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\tool.exe")

    def test_converter_moves_legacy_download_url_to_single_origin_table(self) -> None:
        """The converter emits a single origin table for legacy downloadURL metadata."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "OriginApp" / "1.0.0.local.1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.json").write_text(
                json.dumps({"downloadURL": "https://example.invalid/origin-app.zip"}),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertIn("[origin]", result.stdout)
            self.assertNotIn("[[origin]]", result.stdout)
            self.assertEqual(
                tomllib.loads(result.stdout)["origin"]["url"],
                "https://example.invalid/origin-app.zip",
            )

    def test_converter_preserves_existing_origin_configuration(self) -> None:
        """The converter retains canonical origin settings when it rewrites pkg.toml."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "OriginApp" / "1.0.0.local.1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.toml").write_text(
                """
                name = "OriginApp"
                version = "1.0.0"

                [origin]
                url = "https://example.invalid/current.zip"
                checksum = "sha256:current"
                extractSubdir = "current"

                [[origin.versions]]
                version = "0.9.0"
                script = "scripts/populate-old.cmd"
                """.lstrip(),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            origin = tomllib.loads(result.stdout)["origin"]
            self.assertEqual(origin["url"], "https://example.invalid/current.zip")
            self.assertEqual(origin["checksum"], "sha256:current")
            self.assertEqual(origin["extractSubdir"], "current")
            self.assertEqual(
                origin["versions"],
                [{"version": "0.9.0", "script": "scripts/populate-old.cmd"}],
            )

    def test_converter_preserves_existing_backups_before_replacing_output(self) -> None:
        """The converter creates a numbered backup without replacing an older backup."""

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "BackupApp" / "1.0.0.local.1"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.json").write_text(
                json.dumps({"name": "BackupApp", "version": "1.0.0"}),
                encoding="utf-8",
            )
            gupkg_toml = Path(tmpdir) / "pkg.toml"
            original = '# Retain this migration result.\nname = "Previous App"\n'
            gupkg_toml.write_text(original, encoding="utf-8")
            first_backup = Path(str(gupkg_toml) + ".bak")
            first_backup.write_text("# Older backup.\n", encoding="utf-8")

            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(gupkg_toml)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertEqual(
                first_backup.read_text(encoding="utf-8"), "# Older backup.\n"
            )
            self.assertEqual(
                Path(str(gupkg_toml) + ".bak.1").read_text(encoding="utf-8"), original
            )
            self.assertNotIn(
                "name", tomllib.loads(gupkg_toml.read_text(encoding="utf-8"))
            )
            self.assertIn(f"Backed up {gupkg_toml} to {gupkg_toml}.bak.1", result.stdout)

    def test_converter_rewrites_legacy_gupkg_toml_aliases_to_canonical_schema(
        self,
    ) -> None:
        """Converter rewrites legacy gupkg toml aliases to canonical schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "AliasApp" / "3.4.5.local.2"
            version_dir.mkdir(parents=True)
            (version_dir / "pkg.toml").write_text(
                r"""
                [[main]]
                name = "AliasApp"
                version = "3.4.5"
                local_version = 2
                portable = true
                description = "Legacy alias config"
                homepage = "https://example.invalid/alias"
                download_url = "https://example.invalid/alias.zip"

                env = [{ name = "ALIASAPP_HOME", value = '$AppPath\App' }]
                shortcuts = [
                  { name = "Alias App.lnk", path = '$AppPath\App\alias.exe', args = "--legacy", workdir = '$AppPath\App', icon_location = '$AppPath\Icons\alias.ico', desc = "Legacy shortcut" }
                ]
                path = ['$AppPath\App', '$AppPath\Tools']
                bin = [{ name = "alias.cmd", content = '''@echo off
                "$AppPath\App\alias.exe" %*
                ''' }]
                """,
                encoding="utf-8",
            )

            converted = version_dir / "converted.toml"
            result = self.run_converter(
                "--dir", str(version_dir), "--output", str(converted)
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            parsed = tomllib.loads(converted.read_text(encoding="utf-8"))
            self.assertFalse({"name", "version", "localVersion"} & parsed.keys())
            self.assertTrue(parsed["only_portable"])
            self.assertEqual(parsed["description"], "Legacy alias config")
            self.assertEqual(parsed["homepage"], "https://example.invalid/alias")
            self.assertEqual(
                parsed["origin"]["url"], "https://example.invalid/alias.zip"
            )
            self.assertEqual(
                [entry["value"] for entry in parsed["path"]], ["$App", "$App\\Tools"]
            )
            self.assertEqual(parsed["environment"][0]["Name"], "ALIASAPP_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["name"], "Alias App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\alias.exe")
            self.assertEqual(parsed["shortcut"][0]["arguments"], "--legacy")
            self.assertEqual(parsed["shortcut"][0]["workingDirectory"], "$App")
            self.assertEqual(parsed["shortcut"][0]["iconLocation"], "$Icons\\alias.ico")
            self.assertEqual(parsed["shortcut"][0]["description"], "Legacy shortcut")
            self.assertIn("$App\\alias.exe", parsed["bin"][0]["content"])

    def test_converter_accepts_list_root_legacy_sidecar_files(self) -> None:
        """Converter accepts list root legacy sidecar files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "ListRootApp" / "1.0.0.local.1"
            version_dir.mkdir(parents=True)
            (version_dir / "environment.json").write_text(
                json.dumps(
                    [
                        {"name": "LISTROOT_HOME", "value": "$AppPath\\App"},
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            (version_dir / "shortcuts.json").write_text(
                json.dumps(
                    {
                        "shortcuts": [
                            {
                                "name": "ListRoot",
                                "target_path": "$AppPath\\App\\listroot.exe",
                            },
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = self.run_converter("--dir", str(version_dir), "--dry-run")

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            parsed = tomllib.loads(result.stdout)
            self.assertEqual(parsed["environment"][0]["Name"], "LISTROOT_HOME")
            self.assertEqual(parsed["environment"][0]["Value"], "$App")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\listroot.exe")

    def test_converter_reads_all_checked_in_legacy_examples(self) -> None:
        """Converter reads all checked in legacy examples."""
        cases = [
            (
                "1",
                {
                    "name": "WUMgr",
                    "version": "1.1b",
                    "localVersion": 1,
                    "shortcut_count": 1,
                    "shortcut_name": "Tools\\WU Mgr",
                    "shortcut_target": "$App\\WUMgr.exe",
                },
            ),
            (
                "2",
                {
                    "name": "emacs",
                    "version": "30.2",
                    "localVersion": 2,
                    "only_portable": False,
                    "shortcut_count": 3,
                    "bin_count": 3,
                    "first_shortcut": "Emacs",
                },
            ),
            (
                "3",
                {
                    "name": "PortableGit",
                    "version": "2.46.2",
                    "localVersion": 4,
                    "shortcut_count": 1,
                    "path_values": ["$App\\cmd"],
                    "shortcut_target": "$App\\git-bash.exe",
                },
            ),
            (
                "4",
                {
                    "name": "Tixati",
                    "version": "3.29-1",
                    "localVersion": 6,
                    "shortcut_count": 1,
                    "shortcut_target": "$App\\tixati_Windows64bit.exe",
                },
            ),
            (
                "5",
                {
                    "name": "ISLANDERS - New Shores",
                    "version": "0",
                    "localVersion": 1,
                    "only_portable": False,
                    "shortcut_count": 1,
                    "shortcut_name": "Games\\Islanders - New Shores",
                    "shortcut_target": "$App\\Islanders New Shores.exe",
                },
            ),
        ]

        for example_name, expected in cases:
            with self.subTest(example=example_name):
                result = self.run_converter(
                    "--dir", str(EXAMPLES_ROOT / example_name), "--dry-run"
                )
                self.assertEqual(
                    result.returncode, 0, msg=result.stderr or result.stdout
                )

                parsed = tomllib.loads(result.stdout)
                self.assertFalse({"name", "version", "localVersion"} & parsed.keys())

                if "only_portable" in expected:
                    self.assertEqual(parsed["only_portable"], expected["only_portable"])
                if "shortcut_count" in expected:
                    self.assertEqual(
                        len(parsed.get("shortcut", [])), expected["shortcut_count"]
                    )
                if "bin_count" in expected:
                    self.assertEqual(len(parsed.get("bin", [])), expected["bin_count"])
                if "path_values" in expected:
                    self.assertEqual(
                        [entry["value"] for entry in parsed.get("path", [])],
                        expected["path_values"],
                    )
                if "shortcut_name" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["name"], expected["shortcut_name"]
                    )
                if "first_shortcut" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["name"], expected["first_shortcut"]
                    )
                if "shortcut_target" in expected:
                    self.assertEqual(
                        parsed["shortcut"][0]["targetPath"], expected["shortcut_target"]
                    )


class ShortcutImporterTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "reading .lnk files requires Windows")
    def test_shortcut_reader_reads_a_real_lnk_with_spaces_in_its_path(self) -> None:
        """The reader returns the stored fields of a real shortcut whose path has spaces."""

        importer = load_shortcut_importer_module()
        with tempfile.TemporaryDirectory() as tmp:
            shortcut_path = Path(tmp) / "Commander Keen" / "Commander Keen.lnk"
            shortcut_path.parent.mkdir()
            WindowsSystem().write_shortcut(
                shortcut_path,
                ShortcutSpec(
                    target_path=r"C:\games\CommanderKeen\App\keen.exe",
                    arguments="-fast",
                    description="Keen",
                ),
            )

            self.assertEqual(
                importer.read_windows_shortcut(shortcut_path),
                {
                    "TargetPath": r"C:\games\CommanderKeen\App\keen.exe",
                    "Arguments": "-fast",
                    "WorkingDirectory": "",
                    "IconLocation": "",
                    "Description": "Keen",
                },
            )

    @unittest.skipUnless(os.name == "nt", "reading .lnk files requires Windows")
    def test_shortcut_reader_names_the_file_it_cannot_read(self) -> None:
        """A missing shortcut is reported as one error line that names the file."""

        importer = load_shortcut_importer_module()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.lnk"
            with self.assertRaises(RuntimeError) as caught:
                importer.read_windows_shortcut(missing)

        self.assertIn(str(missing), str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "reading .lnk files requires Windows")
    def test_importer_turns_real_lnk_files_into_toml_with_and_without_icons(self) -> None:
        """Real shortcuts become shortcut tables; a shortcut with no icon gets an empty icon, not ",0"."""

        importer = load_shortcut_importer_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Game" / "1.0.0.local.1"
            shortcuts_dir = version_dir / "_shortcuts"
            (version_dir / "App").mkdir(parents=True)
            (version_dir / "Icons").mkdir()
            shortcuts_dir.mkdir()
            (version_dir / "pkg.toml").write_text(
                'name = "Game"\nversion = "1.0.0"\nlocalVersion = 1\n', encoding="utf-8"
            )
            WindowsSystem().write_shortcut(
                shortcuts_dir / "With Icon.lnk",
                ShortcutSpec(
                    target_path=str(version_dir / "App" / "game.exe"),
                    arguments="--go",
                    working_directory=str(version_dir / "App"),
                    icon_location=f"{version_dir / 'Icons' / 'game.ico'},2",
                    description="Has icon",
                ),
            )
            WindowsSystem().write_shortcut(
                shortcuts_dir / "No Icon.lnk",
                ShortcutSpec(target_path=str(version_dir / "App" / "tool.exe")),
            )

            rendered, shortcuts = importer.import_shortcuts(version_dir)

        parsed = {item["name"]: item for item in tomllib.loads(rendered)["shortcut"]}
        self.assertEqual(len(shortcuts), 2)
        with_icon = parsed["With Icon"]
        self.assertEqual(with_icon["targetPath"], "$App\\game.exe")
        self.assertEqual(with_icon["arguments"], "--go")
        self.assertEqual(with_icon["workingDirectory"], "$App")
        self.assertEqual(with_icon["iconLocation"], "$Icons\\game.ico,2")
        self.assertEqual(with_icon["description"], "Has icon")
        self.assertEqual(parsed["No Icon"].get("iconLocation", ""), "")

    def test_importer_overwrites_matching_shortcuts_and_preserves_other_config(
        self,
    ) -> None:
        """The importer replaces same-name shortcut tables without regenerating unrelated TOML."""

        importer = load_shortcut_importer_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Game" / "1.0.0.local.1"
            shortcuts_dir = version_dir / "_shortcuts" / "Games"
            app_dir = version_dir / "App"
            icons_dir = version_dir / "Icons"
            shortcuts_dir.mkdir(parents=True)
            app_dir.mkdir()
            icons_dir.mkdir()

            (shortcuts_dir / "Launch Game.lnk").write_text("", encoding="utf-8")
            (version_dir / "pkg.toml").write_text(
                r"""
name = "Game"
version = "1.0.0"
localVersion = 1

[[environment]]
Name = "GAME_HOME"
Value = "$App"

[[shortcut]]
name = "Games\\Launch Game"
targetPath = "$App\\old.exe"
description = "Old shortcut"

[[shortcut]]
name = "Tools\\Keep Me"
targetPath = "$App\\tools\\keep.exe"
""".lstrip(),
                encoding="utf-8",
            )

            shortcut_payload = {
                "TargetPath": str(app_dir / "game.exe"),
                "Arguments": "--fullscreen",
                "WorkingDirectory": str(app_dir),
                "IconLocation": f"{icons_dir / 'game.ico'},0",
                "Description": "Launch Game",
            }
            with mock.patch.object(
                importer, "read_windows_shortcut", return_value=shortcut_payload
            ):
                rendered, shortcuts = importer.import_shortcuts(version_dir)

            parsed = tomllib.loads(rendered)
            self.assertEqual(len(shortcuts), 1)
            self.assertEqual(parsed["environment"][0]["Name"], "GAME_HOME")
            self.assertEqual(len(parsed["shortcut"]), 2)

            imported = next(
                item
                for item in parsed["shortcut"]
                if item["name"] == "Games\\Launch Game"
            )
            self.assertEqual(imported["targetPath"], "$App\\game.exe")
            self.assertEqual(imported["arguments"], "--fullscreen")
            self.assertEqual(imported["workingDirectory"], "$App")
            self.assertEqual(imported["iconLocation"], "$Icons\\game.ico,0")
            self.assertEqual(imported["description"], "Launch Game")

            preserved = next(
                item for item in parsed["shortcut"] if item["name"] == "Tools\\Keep Me"
            )
            self.assertEqual(preserved["targetPath"], "$App\\tools\\keep.exe")
            self.assertNotIn("old.exe", rendered)

    def test_importer_appends_new_shortcuts_from_nested_shortcut_directory(
        self,
    ) -> None:
        """Nested files under ``_shortcuts`` become nested shortcut names in TOML."""

        importer = load_shortcut_importer_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            version_dir = Path(tmpdir) / "Tool" / "2.0.0.local.3"
            nested_dir = version_dir / "_shortcuts" / "Tools"
            app_dir = version_dir / "App" / "bin"
            nested_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True)

            (nested_dir / "Tool.lnk").write_text("", encoding="utf-8")
            (version_dir / "pkg.toml").write_text(
                """
name = "Tool"
version = "2.0.0"
localVersion = 3
""".lstrip(),
                encoding="utf-8",
            )

            shortcut_payload = {
                "TargetPath": str(app_dir / "tool.exe"),
                "Arguments": "",
                "WorkingDirectory": "",
                "IconLocation": "",
                "Description": "",
            }
            with mock.patch.object(
                importer, "read_windows_shortcut", return_value=shortcut_payload
            ):
                rendered, _ = importer.import_shortcuts(version_dir)

            parsed = tomllib.loads(rendered)
            self.assertEqual(parsed["shortcut"][0]["name"], "Tools\\Tool")
            self.assertEqual(parsed["shortcut"][0]["targetPath"], "$App\\bin\\tool.exe")


if __name__ == "__main__":
    unittest.main()
