# exe-shim

`exe-shim` turns a TOML file into a small, native Windows launcher. Copy a
launcher executable to the command name you want, place a matching
`.config.toml` file beside it, and the launcher starts the configured target
with predictable arguments, environment changes, working directory, and exit
code handling. By default, the target keeps the caller's current working
directory; the launcher's directory and the target's directory are not used
implicitly. It does not invoke a shell, so it avoids shell-specific quoting
and command-injection behavior; a batch-file target is the one exception, run
through `cmd.exe` with every argument quoted as text (see
[Start a batch file](#start-a-batch-file)).

For example, `gs.exe` reads `gs.config.toml`. It can launch Git with fixed
arguments, append arguments supplied by the caller, and return Git's exit code.

## Use a launcher

1. Choose a launcher (see [Console and GUI launchers](#console-and-gui-launchers)).
2. Copy it to the command name you want to provide.
3. Create `<command>.config.toml` next to that executable.
4. Add the directory containing the executable to `PATH` if you want to invoke
   it by name.

For a `gs` command installed in `C:\Bin`:

```bat
copy shim-console.exe C:\Bin\gs.exe
xcopy /e /i exe-shim-lib C:\Bin\exe-shim-lib
```

The `exe-shim-lib` copy is needed only once per directory, and only for the
dynamic launchers; the `.static.exe` launchers need no extra files.

Create `C:\Bin\gs.config.toml`:

```toml
target = "C:\\Program Files\\Git\\bin\\git.exe"

[[argument]]
value = "status"

[[argument]]
value = "-uno"
```

Now `gs -s` is equivalent to:

```powershell
& 'C:\Program Files\Git\bin\git.exe' status -uno -s
```

The launcher always reads the configuration next to itself, rather than from
the current directory. Relative `target`, `working_dir`, and `path_prepend`
paths are resolved from that configuration directory.

### Create a shim from the command line

`make-shim.cmd` creates a launcher and a commented configuration
file in the current directory. Its target is the required first argument:

```bat
make-shim.cmd "%LOCALAPPDATA%\Programs\Example\tool.exe" --name=tool --type=cli --elevate=false
```

Options are `--name=NAME`, `--type=cli|gui`, `--elevate=true|false`,
`--forward-arguments=true|false`, and `--working-dir=PATH`. The defaults are
the target's basename, `cli`, `false`, and `true`, respectively. The generated
`.config.toml` includes commented examples for fixed arguments, child-only
environment variables, variable removal, and `PATH` prefixes. It refuses to
overwrite an existing executable or configuration file.

## Console and GUI launchers

Build output contains two launchers with identical TOML configuration behavior:

| Launcher | Use it for | Behavior |
| --- | --- | --- |
| `shim-console.exe` | Command-line and terminal tools | Attaches to the calling console. The target inherits terminal behavior, including Ctrl+C handling. Started without a console, it starts the target without one and with the caller's standard handles. |
| `shim-gui.exe` | Desktop applications opened from Explorer, shortcuts, or file associations | Uses the Windows GUI subsystem, so it does not create a console window when launched outside a terminal. |

Use the console launcher for console targets. A GUI launcher can start a
console program, but its output and interactive console behavior will not be
available when it is launched from Explorer. Conversely, starting a GUI target
through the console launcher from a terminal is valid, but the launcher remains
attached to that terminal while it waits for the target to exit.

## Common use cases

### Create a Git shortcut

The `gs` example above supplies `status -uno` and forwards additional caller
arguments. `gs -s` therefore adds `-s` after those fixed arguments.

### Run a project-local tool from any directory

This launcher is relocatable because its paths are relative to its TOML file:

```toml
target = "bin\\real-tool.exe"
working_dir = "work"
forward_arguments = false

[[argument]]
value = "serve"
```

Copy the directory containing `tool.exe`, `tool.config.toml`, `bin`, and
`work` together. `tool.exe` always runs `bin\real-tool.exe` in `work`, and it
always supplies `serve`; caller arguments are deliberately ignored.

### Set a child-only environment

Use this for tools that need configuration without changing the parent shell:

```toml
target = "%LOCALAPPDATA%\\Programs\\Example\\tool.exe"
path_prepend = ["tools"]
remove_environment = ["VIRTUAL_ENV"]

[environment]
RUST_LOG = "info"
TOOL_CACHE = "%LOCALAPPDATA%\\Example\\cache"
```

Windows `%NAME%` references expand from the launcher's inherited environment.
The child receives the added `tools` directory first on `PATH`, has
`VIRTUAL_ENV` removed, and receives the two configured values. These changes
also apply to its descendants, never to the shell that started the launcher.

### Start an application with elevation

Use a GUI launcher for a desktop application that must request UAC elevation:

```toml
target = "C:\\Program Files\\Example\\Admin App\\admin-app.exe"
elevate = true
```

Windows shows the UAC prompt. If elevation is declined or fails, the launcher
returns a non-zero exit code and does not start the target unelevated.

### Find the target on PATH

Set `search_path = true` to start whichever program of that name the child's
`PATH` provides, like typing the name at a prompt:

```toml
target = "python"
search_path = true
```

The target must then be a bare file name. Without an extension the launcher
tries `.com`, `.exe`, `.bat` and `.cmd`, in that order, in each `PATH` folder
in turn; with one of those extensions it looks for exactly that name. The
search uses the `PATH` the target will receive, after `path_prepend` and
`[environment]`. It differs from a prompt in three ways that keep it
predictable: the current directory is never searched, relative `PATH` entries
are skipped, and the launcher never finds itself, so `python.exe` can be a
shim for `python` further down `PATH`. If nothing matches, the launcher fails
and lists the folders it searched.

App execution aliases, such as the `python.exe` in
`%LOCALAPPDATA%\Microsoft\WindowsApps`, count as matches. Set
`skip_app_execution_aliases = true` to pass over them, for example when a
WindowsApps alias would open the Microsoft Store instead of a real program.

Searching suits programs that any installed version can serve. A program that
needs one specific interpreter or its own dependencies should name that
interpreter by path instead, because `PATH` is the caller's and differs
between a terminal, an editor and a scheduled task.

### Start a batch file

A `.bat` or `.cmd` target, found on `PATH` or named by path, runs through the
system's `cmd.exe /d /v:off /s /c`. Every argument is quoted so that cmd treats
`&`, `|`, `<`, `>`, `^`, `!`, quotes and `%NAME%` as text: a batch file that
forwards `%*` to a program passes each argument on unchanged. Inside the batch
file, `%1` holds the argument with its quotes, and any quote within it doubled.
An argument containing a line break cannot be passed through cmd and makes the
launcher fail before starting the target.

## Configuration reference

`target` is required. All other settings are optional:

```toml
target = "%LOCALAPPDATA%\\Programs\\Example\\tool.exe"
forward_arguments = true # Defaults to true.
elevate = false           # Defaults to false.
search_path = false       # Defaults to false; true finds a bare target name on PATH.
skip_app_execution_aliases = false # Defaults to false; requires search_path = true.
target_dir_as_working_dir = false # Defaults to false.
working_dir = "project"
remove_environment = ["VIRTUAL_ENV"]
path_prepend = ["tools"]

[environment]
RUST_LOG = "info"

[[argument]]
value = "--color=always"
```

Configured `[[argument]]` entries appear before caller arguments.
`forward_arguments = false` suppresses caller arguments. `remove_environment`
and `[environment]` may not name the same variable (Windows variable names are
case-insensitive). Invalid TOML, unknown keys, invalid values, and references
to unset `%NAME%` variables cause the launcher to fail before starting a
target. Configuration files must be valid UTF-8.

When `working_dir` is omitted, the target inherits the launcher's current
working directory. Set `target_dir_as_working_dir = true` to instead use the
directory containing the resolved `target` (for `search_path`, the folder
where it was found). `working_dir` and
`target_dir_as_working_dir` may not be used together.

## Build

The project requires a Windows C++23 toolchain, CMake 3.23 or newer, and Conan
2. Dependencies, including `fmt` and `tomlplusplus`, are provided by Conan.
From a Windows command prompt, an MSYS2 UCRT64 shell, or a Visual Studio
developer prompt:

```bat
build.cmd
```

`build.cmd` creates a default Conan profile when necessary, installs
dependencies, configures CMake, and builds the project. To run those steps
individually:

```bat
conan profile detect --force
conan install . --output-folder=.build --build=missing
cmake --preset conan-release
cmake --build --preset conan-release
```

Before running the script from a normal Windows command prompt, set one of
these environment variables to locate the UCRT64 toolkit:

```bat
set "EXE_SHIM_UCRT64_ROOT=C:\path\to\msys2\ucrt64"
build.cmd
```

Alternatively, set `MSYS2_INSTALL_PATH` to the MSYS2 installation directory;
the script uses its `ucrt64` subdirectory. If neither variable is set,
`build.cmd` detects `g++` from `PATH`. It also detects Ninja when available,
otherwise it uses `mingw32-make`.

Conan and CMake generate all build-system files, intermediate objects, static
libraries, and test helpers in `.build/`. Final distribution artifacts are
written directly to `.artifacts/`: `shim-console.exe`, `shim-gui.exe`, their
`.static.exe` counterparts, the dynamic launchers' required runtime DLLs and
license notices (in the `exe-shim-lib/` subdirectory), the project's `LICENSE-exe-shim-MIT.txt` and
`LICENSE-exe-shim-UNLICENSE.txt`, `README-exe-shim.md`, and `make-shim.cmd`.
The normal-named launchers are dynamic;
keep the `exe-shim-lib/` directory alongside them when distributing them; they
load their DLLs from there, not from their own folder.

## Repository layout

- `src/` contains the shared launcher implementation and its console and GUI
  entry points.
- `tests/` contains the Python integration suite, its TOML fixtures, and
  test-only helper executables in `tests/helpers/`.
- `docs/` contains project guidance and design notes.
- `.build/` is generated CMake/Conan build output and is not committed.
- `.artifacts/` contains final distribution artifacts and is not committed.

## Testing

Run the automated integration suite after building:

```bat
ctest --preset conan-release --output-on-failure
```

The suite uses Python's standard library and exercises configuration lookup,
arguments, relative paths, environment edits, validation failures, and exit
code propagation. For checks that require Explorer, console behavior, or UAC,
follow the [manual testing checklist](tests/MANUAL_TESTING.md).

## Process behavior

The launcher waits for the target and returns its exit code. Non-elevated
launches run the target in a Windows job object, so child processes are
terminated when the launcher is terminated. Console launchers suppress the
launcher's default Ctrl+C handling so the target can receive console control
events.

A console launcher started without any console (for example with
`DETACHED_PROCESS`, as some editors and agents start their shells) starts the
target detached as well and passes it its own standard input, output and error.
Otherwise Windows would give a console target a new visible console window,
and the target's output would go to that window instead of the caller's pipes.

## License

SPDX-License-Identifier: MIT OR Unlicense
