# Issue 017 worker log

Branch: rehaul/issue017. Baseline on main: 257 passed. After: 271 passed.

## Files
- src/gupkg/shell_link.py (new), src/gupkg/windows.py, src/gupkg/shortcuts_to_gupkg_toml.py
- README.md, pytest.ini (marker), tests/test_shell_link.py (new), tests/test_helper_scripts.py, tests/manual_smoke.md

## Acceptance
- No powershell call except origin.py: grep; `system_tool("powershell")` kept only for origin.py.
- 10 shortcuts faster: 6.75 s before, 0.15 s after (in-process, 10 `create_shortcut` calls).
- Explorer Properties identical: WScript oracle test + manual_smoke 1.
- TUI worker thread / elevated: thread test + manual_smoke 2.
- Docs: README sentence removed, windows.py docstring updated.

## Notes
- S_FALSE from CoInitializeEx is balanced with CoUninitialize (MS docs), unlike the issue text.
- Importer maps "no icon" to "" (the old WScript path gave ",0").
- Junction part done (DeviceIoControl FSCTL_SET_REPARSE_POINT).
