"""Cover the ``[[shortcut]]`` keys ``run_as_admin`` and ``modes`` (issue 035, T08).

Behaviour covered: both keys are validated; ``run_as_admin`` reaches the recorded effect, the system port and the
drift check; a shortcut is created only in its ``modes``; gupkg's own ``src/pkg.toml`` gives the system scope the
"gupkg manager (administrator)" entry and the user scope only "gupkg manager"; a real ``.lnk`` keeps the flag.

Real: the manifest parser, the effect code, the real ``src/pkg.toml``, and (``windows_integration``) a real ``.lnk``
below ``tmp_path``. Mocked: ``FakeSystem``. Out of scope: opening the link (the UAC prompt is a manual check).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gupkg.engine.effects import EffectContext, apply_effect, desired_effects, verify_each
from gupkg.model.manifest import ManifestError, ManifestKind, manifest_to_toml, parse_manifest
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode, ShortcutSpec
from gupkg.system.windows import WindowsSystem

SOURCE = '[source]\ndownload_url = "https://x.test/a.zip"\n'
SHORTCUT = '[[shortcut]]\nname = "T"\ntarget = "a.exe"\n'


def problems(text: str) -> list[str]:
    """Return the problems ``parse_manifest`` reports for *text*, failing when there are none."""
    with pytest.raises(ManifestError) as caught:
        parse_manifest(text)
    return list(caught.value.problems)


def context(tmp_path: Path, mode: str, package: str = "tool") -> EffectContext:
    """Return an effect context of *mode* below *tmp_path*."""
    return EffectContext(tmp_path / "opt" / package, mode, tmp_path / "bin", tmp_path / "menu", Mode.USER, {}, root_data=tmp_path / "data")


def test_run_as_admin_must_be_a_boolean() -> None:
    """`run_as_admin = "yes"` is a problem that names the key; `true` is kept; absent is false."""
    base = "format = 2\n" + SOURCE + SHORTCUT

    assert any("shortcut[0].run_as_admin" in text for text in problems(base + 'run_as_admin = "yes"\n'))
    assert parse_manifest(base + "run_as_admin = true\n").shortcuts[0].run_as_admin is True
    assert parse_manifest(base).shortcuts[0].run_as_admin is False


def test_a_shortcut_mode_must_be_one_of_the_packages_modes() -> None:
    """A shortcut `modes` outside the package's modes, or "portable", is invalid; a subset is kept and round-trips."""
    assert any("shortcut[0].modes" in text for text in problems('format = 2\nmodes = ["user"]\n' + SOURCE + SHORTCUT + 'modes = ["system"]\n'))
    assert any("shortcut[0].modes" in text for text in problems("format = 2\n" + SOURCE + SHORTCUT + 'modes = ["portable"]\n'))
    kept = parse_manifest("format = 2\n" + SOURCE + SHORTCUT + 'modes = ["system"]\nrun_as_admin = true\n')
    assert kept.shortcuts[0].modes == ("system",)
    assert parse_manifest(manifest_to_toml(kept)).shortcuts == kept.shortcuts
    assert parse_manifest("format = 2\n" + SOURCE + SHORTCUT).shortcuts[0].modes is None


def test_run_as_admin_reaches_the_recorded_effect_and_the_machine(tmp_path: Path) -> None:
    """A shortcut with `run_as_admin = true` is recorded and written with the flag; verify notices a link that lost it."""
    system = FakeSystem(tmp_path / "machine")
    ctx = context(tmp_path, "user")
    body = SHORTCUT.replace('"T"', '"A"') + "run_as_admin = true\n" + SHORTCUT.replace('"T"', '"B"')
    effects, _ = desired_effects(parse_manifest("format = 2\n" + SOURCE + body), ctx)

    admin, plain = (apply_effect(system, effect, ctx) for effect in effects)

    admin_path = Path(str(admin.fields["path"]))
    assert admin.fields["run_as_admin"] is True and "run_as_admin" not in plain.fields
    assert system.read_shortcut(admin_path).run_as_admin is True
    assert system.read_shortcut(Path(str(plain.fields["path"]))).run_as_admin is False
    assert [item for item in verify_each(system, [admin, plain], mode=Mode.USER) if item[1]] == []
    system.write_shortcut(admin_path, replace(system.read_shortcut(admin_path), run_as_admin=False))
    assert [item[1] for item in verify_each(system, [admin], mode=Mode.USER)] == [f"shortcut differs: {admin_path}"]


def test_a_shortcut_is_created_only_in_its_modes(tmp_path: Path) -> None:
    """A shortcut with `modes = ["system"]` is not an effect of a user run; one without `modes` is an effect of both."""
    body = SHORTCUT.replace('"T"', '"Both"') + SHORTCUT.replace('"T"', '"Admin"') + 'modes = ["system"]\n'
    manifest = parse_manifest("format = 2\n" + SOURCE + body)

    found = {mode: [Path(str(e.fields["path"])).stem for e in desired_effects(manifest, context(tmp_path, mode))[0]] for mode in ("user", "system")}

    assert found == {"user": ["Both"], "system": ["Both", "Admin"]}


def test_gupkgs_own_package_has_the_administrator_shortcut_in_system_mode_only(tmp_path: Path) -> None:
    """The real `src/pkg.toml` creates "gupkg manager" in both modes and "gupkg manager (administrator)", with the flag, in system mode only."""
    manifest = parse_manifest((Path(__file__).resolve().parents[1] / "src" / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)

    found = {
        mode: {
            Path(str(e.fields["path"])).stem: e.fields.get("run_as_admin", False)
            for e in desired_effects(manifest, context(tmp_path, mode, "gupkg"))[0] if e.kind == "shortcut"
        }
        for mode in ("system", "user")
    }

    assert found == {
        "system": {"gupkg manager": False, "gupkg manager (administrator)": True},
        "user": {"gupkg manager": False},
    }


@pytest.mark.windows_integration
def test_a_real_shortcut_keeps_the_run_as_administrator_flag(tmp_path: Path) -> None:
    """A real `.lnk` below `tmp_path` written with `run_as_admin` reads back with the flag; one without it reads back without."""
    system = WindowsSystem()
    admin, plain = tmp_path / "admin.lnk", tmp_path / "plain.lnk"

    system.write_shortcut(admin, ShortcutSpec(target_path="%COMSPEC%", arguments="/c echo", description="d", run_as_admin=True))
    system.write_shortcut(plain, ShortcutSpec(target_path="%COMSPEC%", arguments="/c echo"))

    stored = system.read_shortcut(admin)
    assert (stored.target_path, stored.arguments, stored.description, stored.run_as_admin) == ("%COMSPEC%", "/c echo", "d", True)
    assert system.read_shortcut(plain).run_as_admin is False
