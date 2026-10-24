"""Build the result documents of ``--json`` and render results as text (design sections 10.4 and 14).

Every command that prints a machine document builds it here, so the documents share one envelope: ``"api": 1``, the
``command``, ``ok`` and ``exit_code``, then the command's own keys. The plan document of ``--plan --json`` and the result
document of a lifecycle call are built from the typed objects of ``gupkg.model.plan``; the human texts of ``list``,
``info`` and ``--plan`` are rendered from the plain data of ``gupkg.viewdata``. Nothing here prints.

Usage and API
-------------
``result_document(command, batch)``, ``plan_document(command, plans)`` and ``error_document(command, ...)`` return the
documents; ``dumps(document)`` is the single serialiser (stable key order, ``default=str`` for paths and dates).
``format_listing``, ``format_info`` and ``format_plan`` return the human text. ``API_VERSION`` is the contract version.

Implementation Approach
-----------------------
The documents are plain dictionaries with keys that never change within one ``API_VERSION``; a contract test pins them.
A plan entry always carries ``sha256`` and ``url`` (``null`` when the plan downloads nothing) so a caller never has to
guess whether a missing key means "not needed" or "forgotten".
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..model.plan import EXIT_FAILED, EXIT_OK, BatchResult, InstallPlan, PackageResult
from ..redact import redact_text
from .events import API_VERSION


def envelope(command: str, *, ok: bool, exit_code: int, **fields: Any) -> dict[str, Any]:
    """Return the common head of every document: ``api``, ``command``, ``ok`` and ``exit_code``, then *fields*."""
    return {"api": API_VERSION, "command": command, "ok": ok, "exit_code": exit_code, **fields}


def error_document(
    command: str, code: str, message: str, exit_code: int, hint: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Return the document of a call that failed before any target was handled (a usage error, a question that could not be asked)."""
    error: dict[str, Any] = {"code": code, "message": redact_text(message)}
    if hint:
        error["hint"] = hint
    return envelope(command, ok=False, exit_code=exit_code, errors=[error], **fields)


def result_document(command: str, batch: BatchResult, *, warnings: Sequence[str] = ()) -> dict[str, Any]:
    """Return the ``--json`` document of a lifecycle call: one entry per target, in the order they were given.

    ``relaunched`` is true when this process started an elevated child for the system targets; the child writes its own result
    to the events file named with ``--events-file``.
    """
    return envelope(
        command, ok=batch.ok, exit_code=batch.exit_code, relaunched=batch.relaunched,
        results=[item.to_data() for item in batch.results], warnings=[redact_text(item) for item in warnings],
    )


def plan_document(command: str, plans: Sequence[InstallPlan | PackageResult], targets: Sequence[str]) -> dict[str, Any]:
    """Return the ``--plan --json`` document: per target either its plan or why it cannot be planned.

    Parameters
    ----------
    command : str
        The command that was planned.
    plans : sequence
        What ``lifecycle.plan`` returned, one item per target.
    targets : sequence of str
        The targets as the user wrote them, in the same order.
    """
    entries: list[dict[str, Any]] = []
    for target, item in zip(targets, plans):
        if isinstance(item, InstallPlan):
            data = item.to_data()
            data.setdefault("sha256", None)
            data.setdefault("url", None)
            entries.append({"target": target, "ok": True, "exit_code": EXIT_OK, "plan": data})
        else:
            entries.append({"target": target, "ok": item.ok, "exit_code": item.exit_code, "result": item.to_data()})
    failed = [entry["exit_code"] or EXIT_FAILED for entry in entries if not entry["ok"]]
    exit_code = max(failed) if failed else EXIT_OK
    return envelope(command, ok=not failed, exit_code=exit_code, plan=True, plans=entries)


def dumps(document: Mapping[str, Any]) -> str:
    """Serialise a document as one JSON line-free text with a trailing newline (``default=str`` renders paths and dates)."""
    return json.dumps(document, indent=2, default=str, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Human text
# ---------------------------------------------------------------------------


def format_listing(rows: Sequence[Mapping[str, Any]], *, details: bool) -> str:
    """Return the table of ``gupkg list``: name, version, mode and status, with the newest known version of an outdated one."""
    if not rows:
        return "No packages."
    lines: list[str] = []
    width = max(len(str(row["name"])) for row in rows)
    for row in rows:
        version = row["version"] or "-"
        status = row["status"]
        if status == "outdated" and row["newest_known"]:
            status = f"outdated -> {row['newest_known']}"
        lines.append(f"{row['name']:<{width}}  {version:<12} {row['mode'] or '-':<9} {status}")
        if details:
            lines.extend(f"    {line}" for line in _detail_lines(row))
    return "\n".join(lines)


def _detail_lines(row: Mapping[str, Any]) -> list[str]:
    """Return the indented detail lines of one package: path, versions, tracker, link, verification and problems."""
    lines: list[str] = []
    if row.get("path"):
        lines.append(f"path: {row['path']}")
    versions = row.get("versions") or []
    if versions:
        shown = ", ".join(
            f"{item['name']} ({item['status']}{', ' + item['label'] if item.get('label') else ''})" for item in versions
        )
        lines.append(f"versions: {shown}")
    if row.get("registry_versions"):
        lines.append(f"registry versions: {', '.join(row['registry_versions'])}")
    if row.get("tracker") is not None:
        lines.append("tracker: " + ("valid" if row["tracker"]["valid"] else "invalid: " + "; ".join(row["tracker"]["problems"])))
    if row.get("following"):
        lines.append(f"follows: {row['following']}")
    if row.get("not_followed"):
        lines.append(row["not_followed"])
    for item in row.get("notes") or []:
        lines.append(f"note: {item['message']}")
    lines.extend(f"problem: {item}" for item in row.get("problems") or [])
    lines.extend(f"warning: {item}" for item in row.get("warnings") or [])
    return lines


def format_info(row: Mapping[str, Any]) -> str:
    """Return the text of ``gupkg info`` for one package: the summary line, then every detail."""
    head = f"{row['name']}  {row['version'] or '-'}  {row['mode'] or '-'}  {row['status']}"
    lines = [head, *(f"  {line}" for line in _detail_lines(row))]
    checksum = row.get("checksum")
    if checksum:
        lines.append(f"  checksum: {checksum.get('decision')} (url {checksum.get('url', '-')}, final url {checksum.get('final_url', '-')})")
    if row.get("last_check"):
        check = row["last_check"]
        lines.append(f"  last check: {check.get('checked', '?')}, found {check.get('found', '?')} ({check.get('outcome', '?')})")
    if row.get("log"):
        lines.append(f"  log: {row['log']}")
    if row.get("receipts"):
        lines.append(f"  receipts: {', '.join(row['receipts'])}")
    return "\n".join(lines)


def format_plan(entry: Mapping[str, Any]) -> str:
    """Return the text of one entry of a plan document: the phases with their steps, the effects and the checksum status."""
    if "plan" not in entry:
        result = entry["result"]
        reason = (result.get("error") or {}).get("message") or result.get("status")
        return f"{entry['target']}: {reason}"
    plan = entry["plan"]
    lines = [f"Plan: {plan['operation']} {plan['package']} ({plan['mode']}"
             + (", needs administrator rights" if plan["requires_elevation"] else "") + ")"]
    for phase in plan["phases"]:
        marker = "skipped" if phase["skipped"] else ""
        lines.append(f"  {phase['name']}{' (' + marker + ')' if marker else ''}")
        lines.extend(f"    {step}" for step in phase["steps"])
    for effect in plan["effects"]:
        shown = effect.get("path") or effect.get("entry") or effect.get("name") or ""
        lines.append(f"  {effect['action']} {effect['kind']} {shown}")
    if plan.get("sha256") is not None:
        lines.append(f"  sha256: {plan['sha256']}" + (f"  url: {plan['url']}" if plan.get("url") else ""))
    lines.extend(f"  note: {note}" for note in plan["notes"])
    return "\n".join(lines)
