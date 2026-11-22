# Changelog

The version lives in `romtools/__init__.py` (`__version__`) and is bumped for every fix or feature:
patch (1.2.x) for fixes, minor (1.x.0) for new features. Builds are named after it.

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
