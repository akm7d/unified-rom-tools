# Changelog

The version lives in `romtools/__init__.py` (`__version__`) and is bumped for every fix or feature:
patch (1.2.x) for fixes, minor (1.x.0) for new features. Builds are named after it.

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
