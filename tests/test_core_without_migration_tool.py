"""Cover that gupkg core loads and validates format 2 data without the migration tool present.

Behavior covered: with only the `gupkg` package and `tomli_w` on the import path (no
`gupkg_migrate`), a format 2 version directory validates, a tracker renders a pinned version,
settings layers load, and `gupkg_migrate` cannot be imported; a 0.17 package is refused with
the generic unsupported-format error that does not mention any migration tool.

Real: a fresh Python process over a copy of the `gupkg` source folder. Mocked: nothing.
Out of scope: the migration tool itself (see the `test_migrate_*` modules).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
SHIPPED_TOMLI_W = SOURCE / "gupkg" / "python" / "Lib" / "site-packages" / "tomli_w"

PROGRAM = r"""
import json, sys
sys.path[:0] = [sys.argv[1]]
from pathlib import Path
from gupkg.pkgcheck import check_directory
from gupkg.settingsload import load_settings
from gupkg.model.manifest import ManifestKind, TrackResult, parse_manifest, render_pinned

root = Path(sys.argv[2])
version = check_directory(root / "tool" / "1.0")
old = check_directory(root / "old" / "1.0")
tracker = parse_manifest((root / "tool" / "_check_updates" / "pkg.toml").read_text(), ManifestKind.TRACKER)
pinned = render_pinned(tracker, TrackResult("2.0"))
settings = load_settings(
    env={"USERPROFILE": str(root / "home")}, elevated=False, system_mode=False, app_dir=None,
    installed_system_root=root / "sys",
).settings
try:
    import gupkg_migrate
    importable = True
except ImportError:
    importable = False
print(json.dumps({
    "version_ok": version.ok, "old_errors": list(old.errors), "pinned_url": pinned.source.download_url,
    "color": settings["ui.color"], "migrate_importable": importable,
    "loaded": sorted(name for name in sys.modules if name.startswith("gupkg_migrate")),
}))
"""


def test_core_validates_and_loads_format_2_without_the_migration_tool(tmp_path: Path) -> None:
    """Format 2 packages and settings load and validate in a process that has no `gupkg_migrate`."""
    site = tmp_path / "site"
    shutil.copytree(SOURCE / "gupkg", site / "gupkg", ignore=shutil.ignore_patterns("python", "__pycache__", "7zip", "shim"))
    shutil.copytree(SHIPPED_TOMLI_W, site / "tomli_w", ignore=shutil.ignore_patterns("__pycache__"))
    root = tmp_path / "data"
    (root / "tool" / "1.0" / "App").mkdir(parents=True)
    (root / "tool" / "1.0" / "App" / "tool.exe").write_text("x", encoding="utf-8")
    (root / "tool" / "1.0" / "pkg.toml").write_text('format = 2\n[source]\nshipped = true\n', encoding="utf-8")
    (root / "tool" / "_check_updates").mkdir()
    (root / "tool" / "_check_updates" / "pkg.toml").write_text(
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://example.org/dl"\nmatch = "x"\n'
        '[source]\ndownload_url = "https://example.org/${version}.zip"\n', encoding="utf-8",
    )
    (root / "old" / "1.0").mkdir(parents=True)
    (root / "old" / "1.0" / "pkg.toml").write_text('description = "x"\n[origin]\nurl = "https://example.org/a.zip"\n', encoding="utf-8")
    settings = root / "sys" / ".gupkg"
    settings.mkdir(parents=True)
    (settings / "settings.toml").write_text('schema = 3\n[ui]\ncolor = "never"\n', encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-S", "-c", PROGRAM, str(site), str(root)], capture_output=True, text=True, check=False,
    )

    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["version_ok"] is True
    assert result["pinned_url"] == "https://example.org/2.0.zip"
    assert result["color"] == "never"
    assert result["migrate_importable"] is False and result["loaded"] == []
    assert len(result["old_errors"]) == 1 and result["old_errors"][0].startswith("Unsupported pkg.toml format")
    assert "migrat" not in result["old_errors"][0].lower()
