# Changelog

The version lives in `romtools/__init__.py` (`__version__`) and is bumped for every fix or feature:
patch (1.2.x) for fixes, minor (1.x.0) for new features. Builds are named after it.

## 1.15.1
- ROM Optimizer, PS4: a game that gets its update merged in is listed as "Game v1.00; update v1.02 (next row) is merged
  in → CUSA…/" instead of "Game + update v1.02", which read as if the package itself held both.

## 1.15.0
- ROM Optimizer, PS4 (beta): **updates are merged into the game's folder**, like for Switch games. A game's newest update
  (picked with it, or lying next to it) is put on top file by file while extracting, shown as its own row, so there's
  one finished CUSA… folder. An update on its own goes into the already extracted game in the output folder: it's
  extracted and checked completely first, everything it replaces is kept in a folder next to the game, and the game is
  left as it was if anything goes wrong. Can be turned off (then: CUSA…-patch folders as before). Updates that can't be
  used (official, older) are listed with the reason.
- **Split packages** (…_0.pkg, …_1.pkg) are read as one, and shown as one item in ROM Verifier; ROM Renamer keeps their
  pieces' names.
- Official Sony packages are named as such instead of "or a fake one with another passcode".

## 1.14.0
- ROM Renamer: **Check file/folder**, like in ROM Verifier (also Ctrl+O, Ctrl+Shift+O and drag and drop): previews the
  ticked sections for picked files or folders only. Inside the library, sorting uses its system folders; elsewhere the
  folders are made right there. CHDs picked by hand are checked even if "Skip CHDs…" is on, and packages are still
  named with the rest of their game in mind.
- Picking one track of a disc (in ROM Verifier too) checks the whole disc, and picked folders inside a system folder
  know which system they are in.
- The cleanup rules no longer rename the tracks a .cue / .gdi lists (the sheet couldn't find them afterwards).

## 1.13.0
- **ROM Verifier identifies ROMs by their content**, not by the folder, name or extension:
  - A file its folder's DAT doesn't know is looked up in all your DATs. New status **Wrong folder** for good dumps that
    lie in another system's folder ("gb → snes" in the System column).
  - Recognises ROMs, disc images, archives and packages by their own signature (new `identify.py`, 25+ systems). What no
    DAT knows still gets its system and internal title; missing DATs for systems found that way are offered.
  - Folders are searched at any depth, files without or with a wrong extension are found, zips inside zips are opened,
    a zip called .sfc is read as a zip, and disc tracks without their .cue are checked as one disc.
  - Copier / iNES headers, .v64 / .n64 byte order and .smd dumps are recognised by content. Trimmed dumps and dumps with
    extra bytes are named as such instead of "Bad dump".
  - A matching *name* alone no longer makes a file a "Bad dump" of another system's game of that name.
  - Folder names people use are understood: "Super Nintendo", "GBA Games", "Nintendo - Game Boy" …
- **ROM Renamer: Sort into system folders** (off by default): moves loose and misplaced ROMs into their system's folder,
  with tracks, playlists and saves, and fixes extensions the system doesn't use. Preview, Apply and Undo as usual.
- **Fixed: a move could replace a file of the same name** in the target folder (Renamer, "One folder per game": the
  check for names that only differ in case ignored the folder). It's now refused in the preview and when applying.
- "(SGB Enhanced)" and similar in official names no longer count as a hack tag.
- The search for Switch keys looks two levels deep, once: without saved keys every start and scan walked the whole
  library, which froze the windows for minutes on a network share.
- All DATs are parsed only once, and folders are listed with fewer network requests.

## 1.12.1
- ROM Optimizer: changing the **output folder** (or any other option) after files were added was silently ignored:
  queued jobs kept the options from the moment they were added. *Start queue* now notices, shows both folders and
  asks whether to use the current options or keep them as added.
- Adding a file that is already queued for the same task updates its job to the current options (it was silently
  dropped, also when it was queued for a *different* task).
- "Into this folder:" without a folder chosen now asks for one instead of writing next to the source.
- The job queue's **Output** column shows the full path, not just the name (shortened in the middle when needed).
- **Retry failed** works for every task. It only retried chdman jobs: PS4, Switch, RVZ, NSZ, trim, CHD-type and
  playlist jobs were left alone. Jobs are looked at again with the current options (so a missing key or an
  "output exists, tick Overwrite" can be put right and retried), and their old log, timing and progress are cleared.
- One unreadable or cut-off file no longer keeps all the others from being added: it gets its own *Skipped* row with
  the reason.

## 1.12.0
- ROM Optimizer: **PS4 PKG → extracted game folder (BETA - use with care)**, with romtools' own reader for the
  encrypted PS4 package file system (`ps4pkg.py`): no other program to install and no key material. Both key schemes
  are supported. All game data is checked against the packer's signatures and all system files against the package's
  digests while reading, every written file is read back, and output that doesn't match is removed; the package is
  never changed. Official packages and licence-only DLC are skipped with the reason. A warning is shown when the task
  is used (until turned off) and in its options.
- `./install.sh` (running from source) also checks for the Python `cryptography` package.

## 1.11.4
- ROM Renamer's preview makes moves obvious: a new **Action** column says *Rename*, *Move into ‹folder›/* or
  *Rename + move into ‹folder›/* (with "(new folder)" when it doesn't exist yet), moves are shown in blue with a
  folder icon, a pure move shows "(same name)", the summary counts moves and new folders separately, and the Apply
  confirmation lists moves as "MOVE ‹file› → ‹folder›/".

## 1.11.3
- Every app has buttons to open the other two, in the same order and place (next to Theme / Updates) with the
  apps' own icons. ROM Verifier and ROM Renamer couldn't open ROM Optimizer before.

## 1.11.2
- ROM Verifier and ROM Renamer: the "CHD temp: RAM / SSD" switch now reads *Unpack CHDs in RAM (slower, spares the
  SSD)* / *Unpack CHDs on the SSD (faster)*, with a plain explanation of the trade-off in its tooltip.

## 1.11.1
- ROM Renamer: the DAT-extension option now reads *Change the extension to the one the DAT uses (e.g. .smc → .sfc)*,
  with a tooltip showing both outcomes and when it never applies.

## 1.11.0
- **ROM Renamer redesigned**: the three tabs are one panel with three sections - *Official names from DATs*,
  *Console packages (PS4 / Switch)* and *Cleanup rules* - each switched on by its title, with one combined preview.
  When two would rename the same file, the DAT name wins, then the package name, then the cleanup rules; the
  *Reason* column shows the section. Choices are remembered.
- The RAM / SSD choice left the Renamer's toolbar: it only matters when checking CHDs ROM Verifier hasn't checked
  yet, so it's now next to that option (and greyed out while those CHDs are skipped).
- A PS4 DLC without its game nearby is named from the game its own title mentions:
  "Gravity Rush 2 [CUSA03694] (DLC - Dark Angel)" instead of "Dark Angel(GRAVITY RUSH 2) [CUSA03694] (DLC)".

## 1.10.1
- Fix: NSP → NSZ (and back) showed no progress, so long runs looked stuck. nsz prints no readable progress outside a
  terminal, so ROM Optimizer now shows how much has been written, then *Verifying* while nsz reads the result back.
- When nsz's own check finds that the NSZ wouldn't give back the identical file (common with non-standard dumps,
  e.g. NSPs whose content was decrypted), the job now says so plainly; nothing is made and the original is untouched.
- Fix: ROM Optimizer stopped responding during big Switch extractions. nstool prints a line per extracted file
  (tens of thousands for a large game) and the window rebuilt the whole log on every line. Log lines are now picked
  up on the twice-a-second refresh and appended, jobs keep their newest 20,000 lines, and the extraction progress
  counts the output only every 10 s.
- Fix: the progress bar of a RomFS / ExeFS extraction **with an update** reached 100% long before it was done: it
  estimated the size from the small update file (1 GB for Breath of the Wild) instead of the whole patched game
  (15 GB). It now uses the exact size recorded in the game data, counts the copy and the extraction by bytes, and
  shows "x of y GB". Tool runs (nstool, nsz, nodtool) also no longer reset a job's bar to 0% between its steps.
- ROM Optimizer's task list starts each task with the systems it's for (`[GC/Wii]`, `[NSW]`, `[NDS/3DS]`,
  `[PS1/PS2/PSP/DC/SAT/SCD/PCE…]`), with the full system names in the tooltip.
- The Switch RomFS / ExeFS task is now called *NSP / XCI → RomFS + ExeFS folders (auto-applies the newest update
  NSP / XCI)*, and the update it applies gets **its own row** in the job list ("↳ apply update v…") right under the
  game, showing queued / running / applied (or why not). It's still applied in the same pass, which nstool needs.
- ROM Optimizer tabs renamed to say what they do: **Convert & Extract** (was Tasks), **ROM Storage Advisor** (was
  Save space), **Patches & Translations** (was Patch).
- ROM Optimizer's window title is just "ROM Optimizer" (the chdman version stays on the chdman tab).
- Convert & Extract: the log under the job list is now a **Job log** for any tool, with a summary of the selected job
  (status, source → output, time taken, result or error), errors in red, warnings in orange, commands dimmed, and
  **Copy**, **Save…** and **Logs folder** buttons. The full live output is kept.
- NSP → NSZ skips NSPs that bundle several titles with their own tickets (e.g. a game plus its update) with a clear
  reason, and ROM Storage Advisor no longer suggests them: nsz 5.0.0 crashes on those (a known nsz bug, fixed
  upstream on 2026-10-25 but not in a release yet). The originals were never affected.
- Switch package names are also read from NSZ / XCZ files (nsz leaves the small control data uncompressed).

## 1.10.0
- **Signed releases**: each release's SHA256SUMS is signed with the project's Ed25519 key (kept off GitHub); the
  updater only installs a release whose signature matches the key built into the apps, so a compromised GitHub
  account alone can't push an update.
- ROM Verifier: **extracted Switch games** (folders with `exefs/main.npdm`, e.g. made by the RomFS + ExeFS task) are
  identified from the NPDM's Title ID (name from a `control.nacp` if present, else the folder) instead of
  *Skipped*. Package folders are walked without descending into extracted games' RomFS.
- ROM Optimizer: adding files plans the jobs in the background, so the window no longer freezes while it reads a
  big Switch folder over the network.
- Fix: renames that depend on each other (A→B while B→C, or two names swapped) passed the check but then failed
  half-way when applied; they now go through temporary names, for both Apply and Undo.
- **Test suite**: `make test` runs pytest over renaming/undo, patching, console packages, trim/untrim, scanning
  and signed updates, always with throwaway settings folders.

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
