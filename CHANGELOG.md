# Changelog

The version lives in `romtools/__init__.py` (`__version__`) and is bumped for every fix or feature:
patch (1.2.x) for fixes, minor (1.x.0) for new features. Builds are named after it.

## 1.9.0
- **PS4, Switch and Switch 2 are systems** now (folders `ps4`, `switch`, `switch-2`, …) instead of "Other folders &
  loose files". They need no DAT: Manage DATs shows *No DAT needed* and a **Read package data** toggle per folder
  (on by default; off lists the packages without reading them).
- ROM Verifier's system list shows the short system name ("Game Boy Color") instead of the DAT file name, which
  was cut off to "Nintendo - Gam…" for Game Boy, Game Boy Color and Game Boy Advance alike.

## 1.8.2
- Fix: **untrim** read the whole ROM into memory plus a full-size block of padding (4-8 GB for a large 3DS game);
  trim's reversibility check built the padding in memory too. Both are streamed in 8 MB blocks now.
- Safer in-place replacement (trim / untrim in place, Fix CHD types): the original steps aside as
  "Name (original).ext", the new file is put in place, and only then is the original trashed. Before, the original
  was trashed first, so a failed final rename (e.g. a network share hiccup) could leave the game only in the trash.
  In the trash it now has a readable name.

## 1.8.1
- Fix: the packaged builds were missing the `cryptography` library, so Switch packages couldn't be read there
  (running from source was fine). It's now pinned and the self-test decrypts with it.
- **AI disclaimer** in the README and the About box: this software was written by Claude (Anthropic's AI model),
  directed and tested by its maintainer.
- **Credits**: `CREDITS.md` lists every bundled tool, library and database used, with links and licenses; it ships
  in the builds and opens from *Updates ▸ Credits* in every app.

## 1.8.0
- **Updates**: an *Updates* button in all three apps (*Check for updates now*, *Check for updates when an app starts*
  (on by default, at most twice a day), *About*). Packaged builds download the new release, check it against the
  release's SHA256SUMS and unpack it next to the current folder, bringing a portable build's settings and DATs
  along (the old version stays). A git checkout updates with `git pull --ff-only`. *Skip this version* is
  remembered.
- Builds also write `dist/SHA256SUMS` and `dist/RELEASE_NOTES.md` for publishing a release.

## 1.7.0
- **Title keys**: a `title.keys` setting (ROM Verifier → Settings, ROM Optimizer → Options), shared by all apps. Used
  to read Switch packages without a ticket and passed to nstool for RomFS / ExeFS extraction.
- ROM Optimizer asks you to **locate prod.keys / title.keys** when a Switch task needs one that isn't set (and
  saves it for all apps); games that still lack a title key are skipped with the reason.
- ROM Verifier offers to **download missing DATs** when a Quick or Full scan includes systems without one.

## 1.6.1
- Fix: Switch RomFS / ExeFS extraction asked nstool for partition "/0" instead of "/0/" and failed with "Failed to
  extract virtual path". The output folder is created first, and if a part still comes out empty the log now
  includes nstool's listing of the NCA.
- ROM Optimizer: a failed job's full log is saved in `~/.local/share/romtools/logs/` (portable: `data/logs/`).

## 1.6.0
- ROM Optimizer: **Switch NSP / XCI → RomFS + ExeFS folders** task (bundled nstool 1.9.2, MIT). One job per game
  with the newest update applied (from the same package or one next to it) and one per DLC. Needs your
  prod.keys; NSZ/XCZ must be decompressed first.

## 1.5.0
- ROM Verifier: **PS4 PKG and Switch NSP/NSZ/XCI/XCZ are identified** from their own data (title, Title ID,
  type, version, region, firmware, bundle contents) with new statuses *Identified* and *Check*. *Check* flags an
  update/DLC without its base game, DLC for another region's version, duplicates and older updates.
- ROM Verifier: **Check game updates** (Switch, via the public titledb) and *Look up updates online* for PS4.
- ROM Verifier → Settings: **Switch keys** (shared by all three apps).
- ROM Renamer: new **Console packages** tab (names from package data, optional one folder per game; undoable,
  including the moves).

## 1.4.1
- Fix: running from the source folder (the `./install.sh` menu launchers), RVZ/CISO files showed *Error*
  ("nodtool not found") because nodtool, nsz and xdelta3 only came with the packaged builds. `./install.sh` now
  fetches the same pinned, checksum-verified binaries into `./tools` (`make tools` does only that), and the
  error says how to fix it.

## 1.4.0
- ROM Optimizer → Save space: **Run ticked now** converts the ticked files right there, without going to Tasks.
  A new *Status* column shows each file's progress (Queued, ▶ 45% · Compressing, ✔ Done, ✖ Failed with the
  reason), finished files are un-ticked so they aren't redone, and *Stop* stops the run. A *Move the originals to
  the trash once verified* box on the tab frees the space. *Add ticked to Tasks* is still there for checking
  options first. Files added while a run is going start as soon as it ends.

## 1.3.0
- ROM Converter is now **ROM Optimizer** (`rom-optimizer`, `ROM Optimizer.exe`); `rom-converter` still starts it and
  its settings carry over.
- Clearer tabs: **Tasks** (was Batch), **Save space** (was Space saver), **Patch**, and **chdman (advanced)**, which
  holds *Run a chdman command* and *chdman version & updates*. Every tab has a tooltip saying what it's for.

## 1.2.0
- ROM Verifier: compressed GameCube / Wii images (RVZ, CISO, GCZ, WIA, WBFS) are checked by the disc inside
  them (via the bundled nodtool, in memory) instead of showing as Bad dump.
- ROM Converter → Space saver: every file is listed in its own row under its suggestion, with its own size
  estimate and tick box; queue just the files you want.

## 1.1.0
- ROM Converter: auto-match patches, RVZ, NSZ, playlists, NDS/3DS trim, Fix CHD types, Space saver.
- Patch tab (IPS/EBP, BPS, UPS, PPF, xdelta).
