"""Offer the supported helper API for ``pkg.local`` hook modules (hook API 2).

A hook module declares ``PKG_MODULE_API = 2``, defines ``track(ctx)``, ``unpack(ctx)``,
``prepare(ctx)`` or ``populate(ctx)``, and imports what it needs from here. Every helper
that touches the network goes through the context's shared HTTP client, so gupkg's
User-Agent, timeout, retry and ``GITHUB_TOKEN`` rules apply to hooks too. Hooks are
trusted code and may also use the network by other means (decision D28).

Usage and API
-------------
``fetch_text(ctx, url)`` and ``fetch_json(ctx, url)`` read a page; ``resolve_redirects(ctx,
url)`` returns the final address of a link; ``github_release(ctx, repo, tag=None)`` returns a
release object; ``extract_archive(ctx, archive, destination, **options)`` and
``safe_extract(ctx, archive, destination)`` unpack safely; ``flatten(directory, levels)``
removes wrapper folders; ``find_digest(text, name)`` reads a sums file; ``seven_zip(ctx,
*arguments)`` runs the bundled 7-Zip; ``run_prepare(ctx, steps)`` runs declarative
``[[prepare]]`` steps built from ``PrepareRemove``, ``PrepareRename``, ``PrepareCopy`` and
``PrepareWrite``. ``HookContext``, ``TrackResult`` and ``DiscoveredChecksum`` are the types a
hook receives and returns.

Implementation Approach
-----------------------
Each helper is a thin function over a service (``gupkg.services``), so a hook gets exactly
the behavior of the built-in providers and unpack kinds, including their safety checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .model.manifest import (
    DiscoveredChecksum,
    PrepareCopy,
    PrepareRemove,
    PrepareRename,
    PrepareStep,
    PrepareWrite,
    TrackResult,
)
from .services.checksum import find_digest
from .services.hooks import PKG_MODULE_API, HookContext, HookError
from .services.prepare import run_prepare as _run_prepare
from .services.unpack import UnpackPlan, UnpackReport, unpack_artifact
from .services.unpack.transform import strip_levels

__all__ = [
    "PKG_MODULE_API", "DiscoveredChecksum", "HookContext", "HookError", "PrepareCopy", "PrepareRemove",
    "PrepareRename", "PrepareWrite", "TrackResult", "extract_archive", "fetch_json", "fetch_text",
    "find_digest", "flatten", "github_release", "resolve_redirects", "run_prepare", "safe_extract",
    "seven_zip",
]

_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def fetch_text(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> str:
    """Return the text served at *url* through the shared client."""
    return ctx.http.text(url, headers)


def fetch_json(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> Any:
    """Return the JSON document served at *url* through the shared client."""
    return ctx.http.json(url, headers)


def resolve_redirects(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> str:
    """Return the address *url* finally leads to, without downloading its body."""
    return ctx.http.resolve(url, headers).url


def github_release(ctx: HookContext, repo: str, tag: str | None = None) -> dict[str, Any]:
    """Return the GitHub release object of ``owner/name`` (the latest one, or the one tagged *tag*)."""
    suffix = f"tags/{tag}" if tag else "latest"
    data = ctx.http.json(f"https://api.github.com/repos/{repo}/releases/{suffix}", _GITHUB_HEADERS)
    if not isinstance(data, dict):
        raise HookError(f"GitHub returned no release object for {repo}")
    return data


def extract_archive(ctx: HookContext, archive: Path, destination: Path, **options: Any) -> UnpackReport:
    """Unpack *archive* into the new folder *destination*, with the options of ``[source]``.

    The options are those of ``UnpackPlan``: ``kind``, ``strip``, ``subdir``, ``nested``,
    ``extract``, ``require`` and ``verify_manifest``.
    """
    return unpack_artifact(ctx.system, Path(archive), Path(destination), UnpackPlan(**options))


def safe_extract(ctx: HookContext, archive: Path, destination: Path) -> UnpackReport:
    """Unpack *archive* into the new folder *destination* with no option beyond the safety checks."""
    return extract_archive(ctx, archive, destination)


def flatten(directory: Path, levels: int | str = "auto") -> None:
    """Remove wrapper folders from *directory*: *levels* of them, or as many as there are with ``"auto"``."""
    strip_levels(Path(directory), levels)


def seven_zip(ctx: HookContext, *arguments: Any) -> str:
    """Run the bundled 7-Zip with *arguments* and return its output; a failure raises ``ProcessFailed``."""
    return ctx.run([ctx.seven_zip(), *arguments])


def run_prepare(ctx: HookContext, steps: Sequence[PrepareStep]) -> None:
    """Run declarative ``[[prepare]]`` steps on ``ctx.paths.app``, with the safety of the built-in steps."""
    if ctx.paths.app is None:
        raise HookError("This hook has no staged App to prepare")
    _run_prepare(steps, ctx.paths.app, ctx)
