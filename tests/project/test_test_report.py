"""The test-suite report tool (``tools/test_report.py``): the indicators, the time report and the specification view.

A maintainer runs the tool to measure the suite before and after a change (``_docs/tests.md`` section 2.3). It reads
files, changes none, and needs neither gupkg nor the network. These tests run it on a small fake repository below
``tmp_path`` and check the figures it prints.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

import test_report


def _write(path: Path, text: str) -> None:
    """Write *text* (dedented) to *path*, creating its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A repository with one flat test file, one area file, a temporary folder, a baseline and a source tree."""
    _write(tmp_path / "tests" / "test_flat.py", '''\
        """Flat module."""

        import pytest


        def test_one():
            """Summary of one."""


        @pytest.mark.parametrize("n", [1, 2], ids=["a", "b"])
        def test_two_cases(n):
            pass
        ''')
    _write(tmp_path / "tests" / "engine" / "test_install.py", '''\
        """Install feature."""


        class TestInstall:
            """Group."""

            def test_a_method_counts(self):
                """Method summary."""
        ''')
    _write(tmp_path / "tests" / "support" / "helpers.py", "def helper():\n    return 1\n")
    _write(tmp_path / "tests" / "_temp" / "job77" / "README.md", "ref: .agents/jobs/77-x-controller.md\nkind: guard\n")
    _write(tmp_path / "tests" / "_temp" / "issue5" / "README.md", "ref: _issues/issue5-gone.md\n")
    _write(tmp_path / ".agents" / "jobs" / "77-x-progress.md", "Status: Open, not started.\n")
    _write(tmp_path / "tests" / "hygiene_baseline.txt", "rule a file::t\n\nrule b file::u\n")
    _write(tmp_path / "src" / "pkg" / "mod.py", "x = 1\n")
    _write(tmp_path / "src" / "gupkg" / "python" / "bundled.py", "y = 2\n" * 100)
    return tmp_path


def test_the_report_counts_files_tests_layout_and_baseline(fake_repo: Path) -> None:
    """Size, shape, layout and hygiene figures match the fake tree; the bundled runtime is not counted as source."""
    data = test_report.report(fake_repo)

    assert data["size"]["source"]["files"] == 1
    assert data["size"]["source"]["lines"] == 1
    assert data["size"]["tests"]["files"] == 3
    assert data["shape"]["test_functions"] == 3
    assert data["shape"]["collected_cases"] == 4  # test_one, two cases of test_two_cases, the method
    assert data["shape"]["name_length_max"] == len("test_a_method_counts")
    assert data["shape"]["name_length_median"] == len("test_two_cases")
    assert 0 < data["shape"]["docstring_share"] < 1
    assert data["layout"]["flat_files"] == 1
    assert data["layout"]["area_files"] == 1
    assert data["hygiene"]["baseline_lines"] == 2
    folders = {f["folder"]: f for f in data["layout"]["temp_folders"]}
    assert folders["job77"]["open"] is True
    assert folders["job77"]["ref"] == ".agents/jobs/77-x-controller.md"
    assert folders["issue5"]["open"] is False


def test_the_command_line_prints_text_and_json_without_changing_files(fake_repo: Path, capsys) -> None:
    """The default output names every group of indicators, ``--json`` is parseable, and no file is written."""
    before = sorted(p.relative_to(fake_repo).as_posix() for p in fake_repo.rglob("*") if "__pycache__" not in p.parts)

    assert test_report.main(["--root", str(fake_repo), "--no-collect"]) == 0
    text = capsys.readouterr().out
    for heading in ("Size", "Shape", "Layout", "Hygiene"):
        assert heading in text
    assert test_report.main(["--root", str(fake_repo), "--no-collect", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["layout"]["flat_files"] == 1

    after = sorted(p.relative_to(fake_repo).as_posix() for p in fake_repo.rglob("*") if "__pycache__" not in p.parts)
    assert after == before


def test_the_time_report_lists_the_slowest_tests_and_the_share_of_the_slowest_200(tmp_path: Path) -> None:
    """From a JUnit file: total time, the 10 slowest tests in order, the share of the slowest 200, no tiers yet."""
    cases = "".join(f'<testcase classname="t.m" name="test_{i}" time="{i}"/>' for i in range(1, 301))
    junit = tmp_path / "junit.xml"
    junit.write_text(f'<testsuites><testsuite>{cases}</testsuite></testsuites>', encoding="utf-8")

    data = test_report.timings(junit)

    assert data["tests"] == 300
    assert data["total_seconds"] == sum(range(1, 301))
    assert [s["test"] for s in data["slowest"]][:2] == ["t.m::test_300", "t.m::test_299"]
    assert len(data["slowest"]) == 10
    assert data["slowest_200_seconds"] == sum(range(101, 301))
    assert data["slowest_200_share"] == round(sum(range(101, 301)) / sum(range(1, 301)), 3)
    assert data["tiers"] is None


def test_the_specification_view_prints_docstrings_without_code(fake_repo: Path) -> None:
    """``--spec`` gives one heading per feature file and one entry per test, with docstrings and no code."""
    text = test_report.spec(fake_repo, "engine")

    assert "## tests/engine/test_install.py" in text
    assert "Install feature." in text
    assert "### test_a_method_counts" in text
    assert "Method summary." in text
    assert "def " not in text
    assert "test_flat" not in text
    assert "### test_one" in test_report.spec(fake_repo)
