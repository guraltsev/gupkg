"""Cover how a downloaded archive becomes a version's ``App`` folder.

``populate_app`` unpacks the verified download the way the pinned version's ``[source]`` describes (the archive kind,
removing a wrapper folder with ``strip``, a nested archive) and then runs the version's ``[[prepare]]`` steps on the result.
It builds the ``App`` folder in the place it is given and leaves nothing else there, so the install can move the finished
folder into place. The unpacking rules themselves are in ``test_unpack`` and the prepare steps in ``test_prepare``; this
file shows that the two are chained in that order from one manifest.

``FakeSystem`` stands in for the machine; the archive is a real zip built for the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, parse_manifest
from gupkg.services.populate import populate_app
from gupkg.system.fake import FakeSystem
from tests.support.archive import make_zip, tree

pytestmark = pytest.mark.unit


def test_the_app_is_unpacked_as_the_source_describes_and_then_prepared(system: FakeSystem, tmp_path: Path) -> None:
    """A zip with one wrapper folder and a marker folder gives an `App` without the wrapper and without what `[[prepare]]` removes."""
    # Arrange: an archive with a wrapper folder and a marker folder, the manifest that describes both, and an empty stage.
    archive = make_zip(tmp_path / "tool-1.0.zip", {
        "tool-1.0/tool.exe": "exe", "tool-1.0/bin/helper.dll": "dll", "tool-1.0/portable/.portable": "marker",
    })
    manifest = parse_manifest(
        'format = 2\n[source]\ndownload_url = "https://vendor.example/tool-1.0.zip"\nstrip = 1\n'
        '[[prepare]]\nremove = ["portable"]\n',
        ManifestKind.VERSION,
    )
    stage = tmp_path / "stage"
    stage.mkdir()

    # Act: populate the app from the archive.
    populate_app(system, manifest, archive, stage / "App", filename="tool-1.0.zip")

    # Assert: the wrapper and the removed folder are gone, and nothing else was left in the stage.
    assert tree(stage / "App") == {"tool.exe": "exe", "bin/helper.dll": "dll"}
    assert list(stage.iterdir()) == [stage / "App"]
