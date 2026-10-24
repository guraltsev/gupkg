"""Discover the newest commit of a Git ref as a version, and run a package's own ``track`` hook.

``track_git`` resolves a ref to a commit with ``git ls-remote``, fetches that one commit
shallowly to read its date, and returns ``YYYYMMDD-HHMMSS-<short sha>`` (UTC) as the
version with the full commit as a capture. The commit is the integrity anchor of such a
package: the pinned version writes it into ``[source.git] commit``. ``track_module`` calls
the ``track(ctx)`` function of a ``pkg.local`` module with a typed ``HookContext``.

Usage and API
-------------
``track_git(spec, env)`` and ``track_module(spec, env)`` return a ``TrackResult`` or
raise ``TrackError``.

Implementation Approach
-----------------------
Every Git command runs through ``System.run`` with prompts disabled and only safe
transports allowed. The module provider loads the hook through ``gupkg.services.hooks``,
which checks the API version and the return type, so a hook cannot hand back an
unvalidated dictionary.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ...model.manifest import TrackResult
from ...model.trackspec import GitSpec, ModuleSpec
from ...system.port import ProcessFailed, run_checked
from ..gitenv import GitUnavailable, git_command, git_environment
from ..hooks import HookContext, HookPaths, call_track, load_hook
from ..safepath import remove_tree
from .common import TrackEnvironment, TrackError, valid_version

_SHA = re.compile(r"[0-9a-f]{40}")


def track_git(spec: GitSpec, env: TrackEnvironment) -> TrackResult:
    """Return the newest commit of ``spec.ref`` as a dated version.

    Raises
    ------
    TrackError
        If ``git`` fails, the ref does not exist, or the environment has no scratch folder.
    """
    if env.work_dir is None:
        raise TrackError("The git provider needs a scratch folder")
    environment = git_environment(env.system, env.work_dir)

    def git(*arguments: str, cwd: Path | None = None) -> str:
        """Run one Git command and return its stripped output."""
        try:
            return run_checked(env.system, git_command(*arguments, system=env.system), cwd=cwd, env=environment, timeout=120)
        except (ProcessFailed, OSError, TimeoutError, GitUnavailable) as error:
            raise TrackError(f"git {arguments[0]} failed: {error}") from error

    # Resolve the ref to one commit; the option separator keeps a hostile URL from being a flag.
    listing = git("ls-remote", "--exit-code", "--", spec.repo_url, spec.ref)
    commit = listing.split()[0].lower() if listing.split() else ""
    if not _SHA.fullmatch(commit):
        raise TrackError(f"git ls-remote returned no commit for {spec.ref} of {spec.repo_url}")

    # A one-commit shallow fetch into a scratch repository gives the commit's date.
    scratch = Path(env.work_dir) / f"git-track-{commit[:12]}"
    remove_tree(scratch)
    scratch.mkdir(parents=True)
    try:
        git("init", "--quiet", str(scratch))
        git("fetch", "--quiet", "--depth", "1", "--no-tags", "--", spec.repo_url, commit, cwd=scratch)
        stamp = git("log", "-1", "--format=%ct", "FETCH_HEAD", cwd=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not stamp.isdigit():
        raise TrackError(f"git returned no commit date for {commit}")
    moment = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    version = valid_version(f"{moment:%Y%m%d-%H%M%S}-{commit[:7]}")
    captures = {
        "version": version, "commit": commit, "short_commit": commit[:7], "ref": spec.ref,
        "commit_date": moment.isoformat(),
    }
    return TrackResult(version, captures, None)


def track_module(spec: ModuleSpec, env: TrackEnvironment) -> TrackResult:
    """Run ``track(ctx)`` of the module *spec* names, from the tracker directory.

    Raises
    ------
    TrackError
        If the module cannot be loaded, declares another API, lacks ``track``, fails, or
        returns anything but a ``TrackResult`` with a usable version.
    """
    if env.directory is None:
        raise TrackError("The module provider needs the tracker directory")
    context = HookContext(
        package=env.package, version=None, system=env.system, http=env.http,
        paths=HookPaths(package_root=Path(env.directory).parent, version_root=Path(env.directory),
                        work=Path(env.work_dir) if env.work_dir else None),
        options=dict(env.extra),
    )
    try:
        module = load_hook(Path(env.directory), spec.module, env.system)
        result = call_track(module, context)
    except TrackError:
        raise
    except Exception as error:  # noqa: BLE001 - a hook may fail in any way; report it as the check's failure
        raise TrackError(f"{spec.module} failed: {error}") from error
    valid_version(result.version)
    return result
