"""Cover the user documentation: its links and the command reference of the README.

Behaviour covered: every relative link in ``README.md`` and in ``_docs/*.md`` points to a file or folder that exists (and, for a
link into a Markdown file, to a heading that exists there); the commands that the README command reference lists are exactly the
commands that ``gupkg --help`` lists, and so are the subcommand groups; the README mentions no removed 0.17 command as a command
to run; the issue folder holds no finished issue outside ``_issues/_closed``.

Real: the files of the repository and the real argument parser (through the ``CliWorld`` helper). Mocked: ``FakeSystem`` for the
help run. Out of scope: external ``http(s)`` links (no network in tests) and the wording of the documents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.cli_support import CliWorld

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [ROOT / "README.md", *sorted((ROOT / "_docs").glob("*.md")), ROOT / "src" / "gupkg" / "README.md", ROOT / "tests" / "manual_smoke.md"]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```")


def _without_code(text: str) -> str:
    """Return *text* with fenced code blocks and inline code removed (a link-shaped example is not a link)."""
    lines, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            inside = not inside
            continue
        if not inside:
            lines.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(lines)


def _slug(heading: str) -> str:
    """Return the anchor that GitHub gives *heading*: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`", "", heading.strip()).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Return the heading anchors of a Markdown file."""
    found: set[str] = set()
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line.strip()):
            inside = not inside
        elif not inside and (match := re.match(r"#{1,6}\s+(.*)", line)):
            found.add(_slug(match.group(1)))
    return found


def _links(path: Path) -> list[str]:
    """Return the link targets of *path* that point inside the repository."""
    targets = LINK.findall(_without_code(path.read_text(encoding="utf-8")))
    return [target for target in targets if not re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE)]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_relative_link_points_to_something_that_exists(document: Path) -> None:
    """Each relative link of a document names an existing file or folder, and an anchor names an existing heading."""
    problems: list[str] = []
    for target in _links(document):
        name, _, anchor = target.partition("#")
        destination = (document.parent / name).resolve() if name else document
        if not destination.exists():
            problems.append(f"{target}: {destination} does not exist")
        elif anchor and destination.suffix == ".md" and anchor not in _anchors(destination):
            problems.append(f"{target}: no heading '{anchor}' in {destination.name}")

    assert problems == []


def _reference_block(readme: str) -> str:
    """Return the fenced command list under 'Commands and options' of the README."""
    start = readme.index("## Commands and options")
    block = readme.index("```text", start)
    return readme[block:readme.index("```", block + 7)]


def test_the_readme_command_reference_lists_the_commands_of_the_help() -> None:
    """The commands of the README command reference are exactly the commands that `gupkg --help` lists."""
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        help_text = CliWorld(Path(temporary)).cli("--help").out
    in_help = set(re.findall(r"^  ([a-z][a-z-]*)\s{2,}\S", help_text, re.MULTILINE))
    in_readme = set()
    for line in _reference_block((ROOT / "README.md").read_text(encoding="utf-8")).splitlines():
        for match in re.finditer(r"(?:^|\| )gupkg ([a-z][a-z-]*)", line):
            in_readme.add(match.group(1))

    assert in_help and in_help == in_readme


def test_the_readme_lists_every_subcommand_of_the_groups() -> None:
    """The README reference names every action of `registry`, `settings`, `pkg` and `self`."""
    block = _reference_block((ROOT / "README.md").read_text(encoding="utf-8"))
    expected = {
        "registry": "sync|status|list|add|remove", "settings": "show|path|init|set KEY VALUE",
        "pkg": "new|check|pin|bundle|export", "self": "status|repair|check-updates|update",
    }

    for group, actions in expected.items():
        assert f"gupkg {group} {actions}" in block


def test_the_readme_runs_no_removed_command() -> None:
    """No command line in the README starts a command that 1.0 removed (the prose may name them as removed)."""
    removed = ("config-check", "config-fix", "doctor", "repair-deps", "update-deps")
    for document in (ROOT / "README.md", *sorted((ROOT / "_docs").glob("*.md"))):
        if document.name in ("rehaul_design.md",):
            continue
        inside, text = False, document.read_text(encoding="utf-8")
        for line in text.splitlines():
            if FENCE.match(line.strip()):
                inside = not inside
            elif inside and re.match(rf"\s*(gupkg|python -m gupkg) .*\b({'|'.join(removed)})\b", line):
                pytest.fail(f"{document.name} runs a removed command: {line.strip()}")


def test_no_finished_issue_is_left_in_the_open_folder() -> None:
    """Issues 020 to 033 are closed: the open folder holds only issues that are still open."""
    open_issues = sorted(path.name for path in (ROOT / "_issues").glob("issue*.md"))

    assert not [name for name in open_issues if re.match(r"issue0(2\d|3[0-3])-", name)]
