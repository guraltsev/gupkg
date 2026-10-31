"""The automatic test-hygiene checks (``tests/support/hygiene.py``): every rule fails a new violation and spares a baselined one.

The checks run with every pytest session (``_docs/tests_hygiene.md`` section 6). A violation is named
``rule|file|test``. If that line is in ``tests/hygiene_baseline.txt`` the session passes; otherwise it fails with a message
naming the rule, the file, the test and the section of the policy. A baseline line that matches no violation fails too,
so the baseline only shrinks. A run of a few files does not report the baseline lines of the files it did not check.
Temporary-test folders under ``tests/_temp/`` need a README and an open reference, and a ``kind: report`` folder runs only
when the command line names it. Conditional-test folders under ``tests/_conditional/`` are checked like permanent tests and need
a README with the condition that ends them (``until``), a date to check it again (``expires``) and a ``purpose``; after the date
the session fails.

These tests build small fake repositories below ``tmp_path``. Two of them start a real pytest process to show the checks
are wired into a session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

from tests.support import hygiene

REPOSITORY = Path(__file__).resolve().parents[2]

CLEAN_MODULE = textwrap.dedent('''\
    """A feature as its user sees it."""

    import pytest

    from gupkg._version import __version__


    def test_the_feature_does_its_job() -> None:
        """The feature does its job."""
        assert __version__


    @pytest.mark.parametrize("n", [pytest.param(1, id="one"), pytest.param(2, id="two")])
    def test_each_class_of_input_is_accepted(n) -> None:
        """Each class of input is accepted."""
        assert n


    def test_a_named_tuple_may_use_its_public_underscore_methods() -> None:
        """A named tuple's ``_asdict`` is public API, not a private name."""
        from gupkg import cli

        assert cli._asdict
''')

BROKEN_MODULES = {
    "H1": CLEAN_MODULE + "\nfrom tests.test_other import helper\n",
    "H2": CLEAN_MODULE + "\nfrom gupkg.engine import _hidden\n",
    "H3": CLEAN_MODULE + '\n\ndef test_issue_99_fix() -> None:\n    """Fixes the old behaviour."""\n',
    "H4": CLEAN_MODULE + '\n\n@pytest.mark.parametrize("n", [1, 2])\ndef test_a_case(n) -> None:\n    """A case."""\n',
    "H5": CLEAN_MODULE + "\n\ndef test_a_test_without_a_docstring() -> None:\n    pass\n",
}
SELF_CONTAINED = '"""A module."""\n\n\ndef test_issue_99_fix() -> None:\n    """Fixes the old behaviour."""\n'
CLEAN_SELF_CONTAINED = '"""A module."""\n\n\ndef test_it_works() -> None:\n    """It works."""\n'
BROKEN_NAMES = {
    "H1": "",
    "H2": "",
    "H3": "test_issue_99_fix",
    "H4": "test_a_case",
    "H5": "test_a_test_without_a_docstring",
}


def _write(path: Path, text: str) -> Path:
    """Write dedented *text* to *path*, creating its folder, and return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _module(root: Path, source: str) -> Path:
    """Write *source* as ``tests/test_subject.py`` of the fake repository, next to a second test module."""
    _write(root / "tests" / "test_other.py", '"""Another module."""\n')
    return _write(root / "tests" / "test_subject.py", source)


def _baseline(root: Path, *lines: str) -> None:
    """Write the baseline file of the fake repository."""
    _write(root / "tests" / "hygiene_baseline.txt", "# header\n" + "\n".join(lines) + "\n")


def _temp_folder(root: Path, name: str, readme: str | None, issue_file: str | None = None) -> None:
    """Create ``tests/_temp/<name>/`` with a README and, if given, the tracker file the README names."""
    _write(root / "tests" / "_temp" / name / "test_scaffold.py", "def test_x():\n    pass\n")
    if readme is not None:
        _write(root / "tests" / "_temp" / name / "README.md", readme)
    if issue_file is not None:
        _write(root / issue_file, "# an issue\n")


def _readme(ref: str, kind: str = "guard") -> str:
    return f"ref: {ref}\nkind: {kind}\npurpose: Pin the behaviour during the work.\nfate: test_scaffold.py: undecided\n"


def test_a_module_that_follows_every_rule_passes(tmp_path: Path) -> None:
    """A test module with docstrings, public names only, named cases and no history passes all checks."""
    module = _module(tmp_path, CLEAN_MODULE)

    assert hygiene.check(tmp_path, [module]) is None


@pytest.mark.parametrize("rule", [pytest.param(name, id=name) for name in sorted(BROKEN_MODULES)])
def test_a_new_violation_fails_with_its_rule_and_a_baselined_one_passes(tmp_path: Path, rule: str) -> None:
    """A module that breaks a rule fails the check naming the rule, the file, the test and the policy; its baseline line lets it pass."""
    module = _module(tmp_path, BROKEN_MODULES[rule])

    message = hygiene.check(tmp_path, [module])

    where = f"tests/test_subject.py::{BROKEN_NAMES[rule]}" if BROKEN_NAMES[rule] else "tests/test_subject.py"
    assert f"{rule} {where}" in message
    assert "_docs/tests" in message
    assert "never add a line to hygiene_baseline.txt" in message
    _baseline(tmp_path, f"{rule}|tests/test_subject.py|{BROKEN_NAMES[rule]}")
    assert hygiene.check(tmp_path, [module]) is None


def test_a_baseline_line_without_a_violation_fails_and_says_to_delete_it(tmp_path: Path) -> None:
    """A baseline line for a checked file that no longer breaks the rule is an error, so the baseline cannot go stale."""
    module = _module(tmp_path, CLEAN_MODULE)
    _baseline(tmp_path, "H3|tests/test_subject.py|test_the_feature_does_its_job", "H5|tests/test_gone.py|test_x")

    message = hygiene.check(tmp_path, [module])

    assert "H3|tests/test_subject.py|test_the_feature_does_its_job" in message
    assert "H5|tests/test_gone.py|test_x" in message  # the file no longer exists
    assert "Delete them" in message


def test_baseline_lines_of_files_outside_the_run_are_left_alone(tmp_path: Path) -> None:
    """Running only some files does not report the baseline lines of the files it did not check."""
    module = _module(tmp_path, CLEAN_MODULE)
    _baseline(tmp_path, "H3|tests/test_other.py|test_x")  # the file exists but is not part of this run

    assert hygiene.check(tmp_path, [module]) is None


def test_a_temporary_folder_without_a_readme_fails(tmp_path: Path) -> None:
    """A folder ``tests/_temp/<ref>/`` with no README is refused, naming rule H6 and the folder."""
    _temp_folder(tmp_path, "issue999", None)

    message = hygiene.check(tmp_path, [])

    assert "H6 tests/_temp/issue999" in message
    assert "no README.md" in message
    _baseline(tmp_path, "H6|tests/_temp/issue999|")
    assert hygiene.check(tmp_path, []) is None


@pytest.mark.parametrize(
    ("folder", "ref", "extra_file"),
    [
        pytest.param("issue999", "_issues/_closed/issue999-x.md", None, id="closed-issue"),
        pytest.param("issue999", "_issues/issue999-gone.md", None, id="issue-file-missing"),
        pytest.param("job77", ".agents/jobs/77-x-controller.md", ".agents/jobs/77-x-progress.md", id="closed-job"),
    ],
)
def test_a_temporary_folder_whose_reference_is_not_open_fails(tmp_path: Path, folder: str, ref: str, extra_file: str | None) -> None:
    """A temporary folder must name an open issue or job: a closed or missing reference fails until the exit review is done."""
    _temp_folder(tmp_path, folder, _readme(ref), ref if "gone" not in ref else None)
    if extra_file:
        _write(tmp_path / extra_file, "Status: Closed.\n")

    message = hygiene.check(tmp_path, [])

    assert f"H6 tests/_temp/{folder}" in message


def test_a_complete_temporary_folder_with_an_open_reference_passes(tmp_path: Path) -> None:
    """A folder with ``ref``, ``kind``, ``purpose`` and ``fate`` whose issue is open passes."""
    _temp_folder(tmp_path, "issue999", _readme("_issues/issue999-x.md"), "_issues/issue999-x.md")

    assert hygiene.check(tmp_path, []) is None


def _conditional_folder(root: Path, name: str, readme: str | None) -> None:
    """Create ``tests/_conditional/<name>/`` with a clean test module and, if given, a README."""
    _write(root / "tests" / "_conditional" / name / "test_compat.py", CLEAN_SELF_CONTAINED)
    if readme is not None:
        _write(root / "tests" / "_conditional" / name / "README.md", readme)


def _conditional_readme(until: str = "No registry in the old layout is supported.", expires: str = "2027-04-30") -> str:
    return f"until: {until}\nexpires: {expires}\npurpose: Convert registries made in the old layout.\n"


def test_a_complete_conditional_folder_passes_until_its_date_and_fails_after_it(tmp_path: Path) -> None:
    """A folder with ``until``, ``expires`` and ``purpose`` passes up to its date; the day after, the session fails until the date is moved or the tests are retired."""
    _conditional_folder(tmp_path, "old_layout", _conditional_readme(expires="2027-04-30"))

    assert hygiene.check(tmp_path, [], today=date(2027, 4, 30)) is None
    message = hygiene.check(tmp_path, [], today=date(2027, 5, 1))

    assert "H7 tests/_conditional/old_layout" in message
    assert "expired on 2027-04-30" in message and "retire the tests" in message


@pytest.mark.parametrize(
    ("readme", "reason"),
    [
        pytest.param(None, "no README.md", id="no-readme"),
        pytest.param("expires: 2027-04-30\npurpose: Convert.\n", "README lacks until", id="no-condition"),
        pytest.param("until: Gone.\npurpose: Convert.\n", "README lacks expires", id="no-date"),
        pytest.param(_conditional_readme(expires="next spring"), "expires must be a date written YYYY-MM-DD", id="date-not-a-date"),
    ],
)
def test_a_conditional_folder_needs_a_condition_and_a_date(tmp_path: Path, readme: str | None, reason: str) -> None:
    """A conditional folder without a README, or whose README gives no condition or no valid date, is refused, naming rule H7 and the folder."""
    _conditional_folder(tmp_path, "old_layout", readme)

    message = hygiene.check(tmp_path, [], today=date(2026, 11, 1))

    assert "H7 tests/_conditional/old_layout" in message
    assert reason in message


def test_a_test_module_may_not_import_from_a_conditional_folder(tmp_path: Path) -> None:
    """Nothing imports from ``tests/_conditional/``, because the tests there end with their condition (H1)."""
    _conditional_folder(tmp_path, "old_layout", _conditional_readme())
    module = _module(tmp_path, CLEAN_MODULE + "\nfrom tests._conditional.old_layout import helper\n")

    message = hygiene.check(tmp_path, [module], today=date(2026, 11, 1))

    assert "H1 tests/test_subject.py" in message


def _pytest(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run pytest with the hygiene plugin in the fake repository *root*."""
    _write(root / "pytest.ini", "[pytest]\n")
    env = {**os.environ, "PYTHONPATH": str(REPOSITORY), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.support.hygiene", "-p", "no:cacheprovider", "-q", *args],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


def test_a_pytest_session_fails_on_a_new_violation_and_names_the_rule(tmp_path: Path) -> None:
    """A real pytest session with the plugin stops with the message when a test module breaks a rule."""
    _module(tmp_path, SELF_CONTAINED)

    done = _pytest(tmp_path)

    assert done.returncode != 0
    assert "H3 tests/test_subject.py::test_issue_99_fix" in done.stdout + done.stderr


def test_a_report_folder_runs_only_when_the_command_line_names_it(tmp_path: Path) -> None:
    """A ``kind: report`` temporary folder is left out of a normal run and collected when its path is given."""
    _module(tmp_path, CLEAN_SELF_CONTAINED)
    ref = "_issues/issue999-x.md"
    _temp_folder(tmp_path, "issue999", _readme(ref, kind="report"), ref)

    normal = _pytest(tmp_path, "--collect-only")
    named = _pytest(tmp_path, "--collect-only", "tests/_temp/issue999")

    assert "test_scaffold" not in normal.stdout
    assert "test_scaffold" in named.stdout
