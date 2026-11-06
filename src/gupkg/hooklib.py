"""Offer the supported helper API for ``pkg.local`` hook modules (hook API 2).

A hook module declares ``PKG_MODULE_API = 2``, defines ``track(ctx)``, ``unpack(ctx)``,
``prepare(ctx)`` or ``populate(ctx)``, and imports what it needs from here. Every helper
that touches the network goes through the context's shared HTTP client, so gupkg's
User-Agent, timeout, retry and ``GITHUB_TOKEN`` rules apply to hooks too. Hooks are
trusted code and may also use the network by other means (decision D28).

Usage and API
-------------
``fetch_text(ctx, url)`` and ``fetch_json(ctx, url)`` fetch a page; ``resolve_redirects(ctx,
url)`` returns the final address of a link; ``github_release(ctx, repo, tag=None)`` returns a
release object; ``extract_archive(ctx, archive, destination, **options)`` and
``safe_extract(ctx, archive, destination)`` unpack safely; ``flatten(directory, levels)``
removes wrapper folders; ``find_digest(text, name)`` looks up a digest in a sums file; ``seven_zip(ctx,
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

# Headers GitHub's REST API asks for: JSON media type and a pinned API version.
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def fetch_text(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> str:
    """Return the text served at *url* through the shared client.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; its HTTP client applies gupkg's User-Agent, timeout and retry rules.
    url : str
        The address to fetch.
    headers : Mapping[str, str], optional
        Extra request headers.

    Returns
    -------
    str
        The decoded response body.
    """
    return ctx.http.text(url, headers)


def fetch_json(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> Any:
    """Return the JSON document served at *url* through the shared client.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; its HTTP client applies gupkg's User-Agent, timeout and retry rules.
    url : str
        The address to fetch.
    headers : Mapping[str, str], optional
        Extra request headers.

    Returns
    -------
    Any
        The parsed JSON value.
    """
    return ctx.http.json(url, headers)


def resolve_redirects(ctx: HookContext, url: str, headers: Mapping[str, str] | None = None) -> str:
    """Return the address *url* finally leads to, without downloading its body.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; its HTTP client is used.
    url : str
        The address to follow.
    headers : Mapping[str, str], optional
        Extra request headers.

    Returns
    -------
    str
        The final address after all redirects.
    """
    return ctx.http.resolve(url, headers).url


def github_release(ctx: HookContext, repo: str, tag: str | None = None) -> dict[str, Any]:
    """Return the GitHub release object of ``owner/name`` (the latest one, or the one tagged *tag*).

    Parameters
    ----------
    ctx : HookContext
        The hook's context; its HTTP client is used.
    repo : str
        The repository as ``owner/name``.
    tag : str, optional
        The release tag; the latest release when omitted.

    Returns
    -------
    dict[str, Any]
        The release object as GitHub's API returns it.

    Raises
    ------
    HookError
        If GitHub answers with something other than a release object.
    """
    # Choose the endpoint for the requested release and fetch it with GitHub's API headers.
    suffix = f"tags/{tag}" if tag else "latest"
    data = ctx.http.json(f"https://api.github.com/repos/{repo}/releases/{suffix}", _GITHUB_HEADERS)
    if not isinstance(data, dict):
        raise HookError(f"GitHub returned no release object for {repo}")
    return data


def extract_archive(ctx: HookContext, archive: Path, destination: Path, **options: Any) -> UnpackReport:
    """Unpack *archive* into the new folder *destination*, with the options of ``[source]``.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; its system services run the unpacking.
    archive : Path
        The archive to unpack.
    destination : Path
        The folder to create.
    **options : Any
        Fields of ``UnpackPlan``: ``kind``, ``strip``, ``subdir``, ``nested``, ``extract``, ``require``
        and ``verify_manifest``.

    Returns
    -------
    UnpackReport
        What the unpacking produced.
    """
    return unpack_artifact(ctx.system, Path(archive), Path(destination), UnpackPlan(**options))


def safe_extract(ctx: HookContext, archive: Path, destination: Path) -> UnpackReport:
    """Unpack *archive* into the new folder *destination* with no option beyond the safety checks.

    Parameters
    ----------
    ctx : HookContext
        The hook's context.
    archive : Path
        The archive to unpack.
    destination : Path
        The folder to create.

    Returns
    -------
    UnpackReport
        What the unpacking produced.
    """
    return extract_archive(ctx, archive, destination)


def flatten(directory: Path, levels: int | str = "auto") -> None:
    """Remove wrapper folders from *directory*, lifting their contents one level up.

    Parameters
    ----------
    directory : Path
        The folder to flatten.
    levels : int or str
        How many wrapper levels to remove, or ``"auto"`` for as many as there are.
    """
    strip_levels(Path(directory), levels)


def seven_zip(ctx: HookContext, *arguments: Any) -> str:
    """Run the bundled 7-Zip with *arguments* and return its output.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; it locates and runs the bundled 7-Zip.
    *arguments : Any
        Command-line arguments passed to 7-Zip.

    Returns
    -------
    str
        The output of the command.

    Raises
    ------
    ProcessFailed
        If 7-Zip exits with a failure (raised by the context's runner).
    """
    return ctx.run([ctx.seven_zip(), *arguments])


def run_prepare(ctx: HookContext, steps: Sequence[PrepareStep]) -> None:
    """Run declarative ``[[prepare]]`` steps on ``ctx.paths.app``, with the safety of the built-in steps.

    Parameters
    ----------
    ctx : HookContext
        The hook's context; ``ctx.paths.app`` is the folder the steps act on.
    steps : Sequence[PrepareStep]
        The ``PrepareRemove``, ``PrepareRename``, ``PrepareCopy`` and ``PrepareWrite`` steps to run.

    Raises
    ------
    HookError
        If the hook has no staged App.
    """
    if ctx.paths.app is None:
        raise HookError("This hook has no staged App to prepare")
    _run_prepare(steps, ctx.paths.app, ctx)
