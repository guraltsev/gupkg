"""Check that a published gupkg release is what ``RELEASES.md`` says it is.

This is a maintainer tool for the development tree; it is never shipped.
``python tools/check_release.py [version]`` (``scripts\\check-release.cmd``) downloads the
release from the same address the bootstrap uses and prints one PASS or FAIL line per check;
the exit status is 1 if any check failed.

Usage and API
-------------
``python tools/check_release.py [version] [--remote NAME] [--base-url URL]``. Without a version
it checks the one in ``src/gupkg/_version.py``. ``--base-url`` replaces
``https://github.com/<owner>/<repo>/releases/download/v<version>`` (a mirror, or a test server).
``check(root, version, base_url, remote, fetch)`` returns ``(passed, lines)``; *fetch* maps a URL to bytes.

Implementation Approach
-----------------------
1. Read the ``RELEASES.md`` row of the version: the commit and the ZIP's SHA-256.
2. Compare the local tag, the remote tag and the pushed branch with that commit.
3. Download the ZIP and its ``.sha256`` asset and compare both digests with the row.
4. Open the ZIP: ``RELEASE.txt`` must name the version and the commit, and every file listed in
   ``MANIFEST.sha256`` must exist with that SHA-256, with no other file in the tree.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402  (a sibling script, not an installed package)

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    """Run the checks named in the module docstring and return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", nargs="?", help="version to check (default: the one in _version.py)")
    parser.add_argument("--remote", help="Git remote (default: the first one)")
    parser.add_argument("--base-url", help="address that holds the release assets")
    args = parser.parse_args(argv)
    version = args.version or release.runtime_version(root)
    remote = args.remote or _git(root, "remote").splitlines()[0]
    base_url = args.base_url or _release_url(_git(root, "remote", "get-url", remote), version)
    passed, lines = check(root, version, base_url, remote, _download)
    print("\n".join(lines))
    print(f"gupkg {version}: {'all checks passed' if passed else 'CHECKS FAILED'}")
    return 0 if passed else 1


def check(root: Path, version: str, base_url: str, remote: str, fetch: Callable[[str], bytes]) -> tuple[bool, list[str]]:
    """Return whether every check passed, and one report line per check."""
    lines: list[str] = []

    def report(ok: bool, text: str) -> bool:
        lines.append(f"{'PASS' if ok else 'FAIL'}  {text}")
        return ok

    row = re.search(
        rf"^\|\s*{re.escape(version)}\s*\|\s*`([0-9a-f]{{40}})`\s*\|[^|]*\|\s*([0-9a-f]{{64}})\s*\|",
        (root / "RELEASES.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not report(row is not None, f"RELEASES.md has a row for {version} with a commit and a SHA-256"):
        return False, lines
    commit, digest = row.group(1), row.group(2)

    tag = f"v{version}"
    local = _git(root, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}", check=False)
    ok = report(local == commit, f"local tag {tag} points at the released commit {commit[:12]}")
    remote_tag = _git(root, "ls-remote", remote, f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}", check=False).split()
    ok &= report(commit in remote_tag[:1] or commit in remote_tag, f"{remote} has the tag {tag} at the released commit")
    pushed = _git(root, "ls-remote", remote, "refs/heads/main", check=False).split()
    ok &= report(
        bool(pushed) and _git(root, "merge-base", "--is-ancestor", "HEAD", pushed[0], check=False, status=True) == 0,
        f"{remote}/main contains this checkout's HEAD (the RELEASES.md row is pushed)",
    )

    name = f"gupkg-{version}-win-amd64.zip"
    try:
        archive = fetch(f"{base_url}/{name}")
    except OSError as error:
        report(False, f"download {base_url}/{name}: {error}")
        return False, lines
    actual = hashlib.sha256(archive).hexdigest()
    ok &= report(actual == digest, f"the downloaded ZIP has the SHA-256 of the RELEASES.md row ({actual[:16]}...)")
    try:
        sidecar = fetch(f"{base_url}/{name}.sha256").decode("ascii", "replace").split()
        ok &= report(bool(sidecar) and sidecar[0] == digest, f"{name}.sha256 names the same SHA-256")
    except OSError as error:
        ok &= report(False, f"download {name}.sha256: {error}")

    if actual != digest:
        return False, lines
    with zipfile.ZipFile(BytesIO(archive)) as zipped:
        try:
            release.check_release_zip_object(zipped, name, version, commit)
            ok &= report(True, "RELEASE.txt names this version and commit")
        except SystemExit as error:
            ok &= report(False, str(error))
        ok &= _check_manifest(zipped, report)
    return ok, lines


def _check_manifest(zipped: zipfile.ZipFile, report: Callable[[bool, str], bool]) -> bool:
    """Require every file of the bundled tree to match ``MANIFEST.sha256``, with no extra file."""
    manifests = [name for name in zipped.namelist() if re.fullmatch(r"(?:[^/]+/){1,2}gupkg/MANIFEST\.sha256", name)]
    if len(manifests) != 1:
        return report(False, "the ZIP has exactly one gupkg/MANIFEST.sha256")
    manifest = manifests[0]
    prefix = manifest[: -len("gupkg/MANIFEST.sha256")]
    listed: dict[str, str] = {}
    for line in zipped.read(manifest).decode("utf-8").splitlines():
        found = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if found:
            listed[prefix + found.group(2).replace("\\", "/")] = found.group(1)
    tree = {name for name in zipped.namelist() if name.startswith(prefix + "gupkg/") and not name.endswith("/")}
    tree.discard(manifest)
    bad = [name for name, expected in listed.items() if name not in tree or hashlib.sha256(zipped.read(name)).hexdigest() != expected]
    extra = sorted(tree - set(listed))
    ok = report(not bad and bool(listed), f"all {len(listed)} files of MANIFEST.sha256 exist with their SHA-256" + (f" (bad: {bad[:3]})" if bad else ""))
    ok &= report(not extra, "the tree holds no file the manifest does not list" + (f" (extra: {extra[:3]})" if extra else ""))
    return ok


def _release_url(remote_url: str, version: str) -> str:
    """Return the release asset folder of the GitHub repository that *remote_url* names."""
    found = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
    if found is None:
        raise SystemExit(f"Not a GitHub remote: {remote_url}; pass --base-url")
    return f"https://github.com/{found.group(1)}/{found.group(2)}/releases/download/v{version}"


def _download(url: str) -> bytes:
    """Return the bytes at *url* (``file:`` and ``https:`` only)."""
    if not url.startswith(("https://", "file:")):
        raise OSError(f"refusing a non-https address: {url}")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310  (scheme checked above)
        return response.read()


def _git(root: Path, *arguments: str, check: bool = True, status: bool = False) -> str | int:
    """Run Git in *root*; return its stripped output, or its exit status when *status* is set."""
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=check and not status)
    return result.returncode if status else result.stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
