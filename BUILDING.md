# Building and maintaining romtools (Linux + Windows)

One source tree produces both builds. Everything platform-specific lives in **`romtools/compat.py`**, and
everything external the builds depend on is pinned in **`packaging/versions.toml`**.

```
romtools/              the application (same code on Linux and Windows)
  compat.py            ALL Linux/Windows differences: paths, temp dirs, RAM mode, trash, tools, processes
  __main__.py          entry point for every launcher + the --selftest used by the builds
packaging/
  versions.toml        pinned versions + checksums (Python for Windows, PySide6, PyInstaller, chdman, 7-Zip)
  build.py             builds Linux and/or Windows, runs the self-test, writes dist/
  romtools.spec        PyInstaller spec shared by both platforms (3 launchers, 1 runtime)
  linux/install.sh     menu/desktop installer shipped inside the Linux build
  windows/README-Windows.txt   notes shipped inside the Windows build
install.sh             developer install: run straight from this source folder on Linux
Makefile               shortcuts (below)
```

## Everyday commands

| Command | What it does |
|---|---|
| `make run` | start ROM Verifier from source (`python3 -m romtools --app renamer` / `converter` for the others) |
| `make selftest` | quick health check from source |
| `make linux` | `dist/romtools-<version>-linux-x64.tar.gz` |
| `make windows` | `dist/romtools-<version>-windows-x64.zip` (built with Wine on Linux) |
| `make all` | both |
| `make clean` / `make distclean` | remove `build/` + `dist/` / also the download, venv and Wine cache |

Requirements on the build machine (Arch / CachyOS): `python`, `pyside6` (renders the icons), `7zip`, `wine`
(Windows build only), and internet access the first time. Downloads, the Linux venv and the Wine prefix are
cached in `~/.cache/romtools-build` (set `ROMTOOLS_BUILD_CACHE` to move it), so later builds take a few
minutes. The first Windows build also installs Windows Python into that private Wine prefix; your own
`~/.wine` is never touched.

Every build runs the **packaged** program with `--selftest` (under Wine for Windows): imports, chdman,
7-Zip, hashing, DAT parsing and all three windows. A failing self-test stops the build, so a broken
archive never lands in `dist/`.

## Releasing a new version

1. Make and test your changes from source (`make run`, `make selftest`).
2. Bump `__version__` in `romtools/__init__.py`.
3. `make all` and check both self-tests print `RESULT OK`.
4. Commit, then tag: `git tag v<version>`.
5. Ship `dist/romtools-<version>-linux-x64.tar.gz` and `dist/romtools-<version>-windows-x64.zip`.

## Updating a dependency

Edit `packaging/versions.toml`, then `make all`:

- **PySide6 / PyInstaller**: change the version under `[pip]`. Both builds use the same pins.
- **Windows Python**: take the `-amd64.exe` URL and its SHA-256 from python.org's release page.
  Use a release that still ships Windows installers (security-only releases don't).
- **Bundled chdman.exe**: change `mame_tag` (e.g. `mame0290`). Its checksum is taken from MAME's
  `SHA256SUMS` automatically. Users can also update chdman from inside ROM Converter.
- **7-Zip**: change `version`, `url` and `sha256` (compare the GitHub and 7-zip.org copies).

Downloads whose SHA-256 doesn't match are refused.

## Adding features without breaking either platform

- Write features once, in the normal modules. If something needs a different approach per OS (a path, a
  system call, an external program), add a small function to `compat.py` and call that. Never test
  `os.name` / `sys.platform` anywhere else.
- External programs: find them with `compat.find_tool("name")`. It finds the copy bundled in
  `tools/` on Windows or the one on `PATH` on Linux. Launch them with `subprocess` as usual; console windows
  are hidden on Windows automatically.
- Paths for settings/data/cache: `config.CONFIG_DIR`, `DATA_DIR` and `CACHE_DIR`. Files shipped with the
  program (icons): `compat.resource_dir()`.
- Linux-only features (like the RAM streaming mode) should check a `compat.*_supported()` function and
  hide or disable their UI elsewhere.
- New module or GUI window? Add it to the `imports` check in `romtools/__main__.py` (the self-test).
  Add a check there for anything a build could silently lose, such as a bundled tool or a data file.
- New data files that must ship with the program go in the spec's `datas=` (icons are already there).
- Test GUI changes with a throwaway config: `XDG_CONFIG_HOME=/tmp/x XDG_CACHE_HOME=/tmp/x make run`.
