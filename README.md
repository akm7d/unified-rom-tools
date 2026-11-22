# romtools — ROM Verifier, ROM Renamer & ROM Converter

Three Linux-native Qt (PySide6) desktop apps for a RomM-style library (`roms/<system>/…`).

## Install

**Linux (from source, for development):**

```bash
./install.sh
```

**Linux / Windows (portable builds):** `romtools-<version>-linux-x64.tar.gz` and
`romtools-<version>-windows-x64.zip` need no installation. Extract and run `rom-verifier` / `ROM Verifier.exe`.
Both bundle chdman and 7-Zip, and keep all settings, DATs, tags and remembered scans in a `data` folder next to
the programs, so the folder can be moved or carried on a USB stick (delete `portable.txt` to use per-user
settings instead). On Linux, `./install.sh` inside the folder optionally adds menu entries. The Linux build
runs on distros with glibc 2.38 or newer (Ubuntu 24.04+, Debian 13, Fedora 39+, current Arch/CachyOS). To
build either yourself, see [BUILDING.md](BUILDING.md).

Adds `rom-verifier`, `rom-renamer` and `rom-converter` to `~/.local/bin` and to your app menu (Games / Utilities).
Requirements (already present on this machine): `pyside6`, `mame-tools` (chdman), `7zip`.

## ROM Verifier (read-only)

Point it at **any folder**. Sub-folders recognised as systems (`psx/`, `snes/`, … as RomM, ES-DE or Batocera
lay them out) are checked against their own DAT. Everything else (ROMs and zip/7z/rar archives lying directly
in the folder, game-named folders like `Blue Stinger (USA)/`, nested collections up to 3 levels deep) appears
as **Other folders & loose files** and is identified against all your DATs at once, so no system has to be
picked. If the folder you choose is itself a system folder (e.g. `…/gb`), its own DAT is used.

**Check file/folder** (toolbar, `Ctrl+O` / `Ctrl+Shift+O`, or drag files and folders onto the window) verifies
any ROM, archive or folder right away, wherever it is, without touching your library setting or ticked systems.

1. **Manage DATs…** → *Download all missing*. Disc systems get official **Redump** DATs; cartridge systems
   get **No-Intro** DATs (libretro mirror). *Add DAT file…* imports any Logiqx / ClrMamePro DAT
   (Dat-o-Matic packs, translation DATs, …); several DATs per folder are merged.
2. Tick systems → **Verify** (F5).

| Status | Meaning |
|---|---|
| Verified | hashes match the DAT and the name is right |
| Misnamed | good dump, wrong name → fix with ROM Renamer |
| Partial | some tracks match (e.g. a Dreamcast CHD built from a TOSEC GDI: data OK, audio offset differs) |
| Bad dump | name is in the DAT but data differs (bad dump, hack, trimmed, patched) |
| Unknown | hashes aren't in the DAT (hacks, translations, homebrew) |
| Modified | tagged as a hack / translation / undub / other mod, so it's expected not to match |

**Hacks & translations:** a modified ROM can never match No-Intro/Redump, so it would otherwise show as
*Bad dump* (original name) or *Unknown*. Files are tagged automatically from common naming conventions
(`[T-En by …]`, `(T-En)`, `English Patched`, `(Hack)`, `[h1]`, `Undub`, `[f1]`, `[t1]`, `(Proto)`,
`(PD)`/Homebrew, …), and **right-click → Mark as…** sets *Translation, Hack, Undub, Improvement, Homebrew,
Trainer, Prototype, Modified* or a custom tag; *Not modified* overrides name detection and *Automatic* goes
back to it. Marks are stored by file content in `~/.local/share/romtools/marks.json`, so they survive
renames and moves. Tagged files get the *Modified* status (with its own filter chip), a *Tag* column (✎ =
marked by you) and are never renamed by the Renamer's DAT mode.

How formats are checked:
- **CHD**: the original tracks are rebuilt with `chdman` (CD split per track, GD-ROM via GDI, DVD as ISO)
  and hashed. The CHD itself is only read. The toolbar switch picks where the rebuilt data goes:
  - **RAM (default, no SSD writes)**: `chdman` writes into `/dev/shm` while the tool hashes right behind
    it and frees each chunk once hashed. If hashing falls behind, `chdman` is paused. Peak RAM is about
    35 MB per CHD, even for 8.5 GB PS2 discs.
  - **SSD**: full temp files in the Settings temp folder (default `/var/tmp/romtools`), deleted after
    hashing. Writes up to a whole disc image per CHD to disk.
  - Switching during a run only affects the next run.
- **zip / 7z / rar**: every member is hashed by streaming (no extraction to disk). An archive holding several
  games shows one row per game (`collection.zip › Game (USA).gb`), each with its own status; files of one game
  (e.g. a zipped .cue/.bin) stay together. The Renamer never renames an archive after one game inside it.
- **cue/bin, gdi** sets: every track hashed.
- **Dreamcast GD-ROM CHDs** are rebuilt as split .cue/.bin (not .gdi), and a 2-second pregap that chdman
  leaves out of the track file is hashed back in as silence, the way Redump's track files contain it.
  (.gdi output drops or misplaces that pregap, so every Dreamcast CHD used to show *Partial*.)
- **NES / FDS / Lynx / 7800 / SNES copier headers**: hashed with and without the header in one pass.
- **Quick scan (F5)** reuses results remembered from earlier scans and only hashes ROMs that are new or
  changed (by size/date). **Full scan (Shift+F5)** re-hashes everything and replaces what's remembered.
  Right-click → *Full re-scan of selected* does the same for a few rows. Remembered results show up as soon
  as the window opens (`~/.cache/romtools/hashes.sqlite`; Settings → *Forget all remembered scans*).

**CHD type check:** every CHD gets a *CHD type* column (CD / DVD / GD). A CHD stored as the wrong type is
flagged (e.g. a PS2 DVD game packed with `createcd`), using: a "CD" bigger than any CD can hold, the Redump
DAT (.iso = DVD, .bin/.cue = CD), or the system (PS1 = CD, PSP = DVD, Dreamcast = GD-ROM). The *Wrong CHD
type* chip filters to them. **Fix CHD types…** (toolbar, or right-click) converts them: the disc image is
unpacked to RAM or the SSD temp folder (per the CHD temp switch), a new CHD is created next to the original,
`chdman verify`'d and read back to prove its data SHA-1 is identical; only then is the original moved to the
trash and the new CHD given its name. Any failure leaves the original untouched. Single-track 2048-byte CD
CHDs (made from an .iso) convert to DVD losslessly; DVD → CD is also supported. GD-ROM mismatches are reported
but must be re-created from the original .gdi/.cue.

Right-click rows for: open folder, copy paths / SHA-1s / DAT names, re-check. **Export report** writes a CSV
(per track hashes) or a plain-text summary.

## ROM Renamer (preview first, undoable)

- **Match DAT names**: renames verified-good ROMs to their exact DAT name (identified by hash, shares the
  Verifier's cache). By default it skips CHDs that haven't been verified yet, so it doesn't start long
  extractions.
- **Cleanup rules**: for systems without DATs (Switch, PS4…): strip ` (1)` duplicate markers,
  underscores → spaces, move leading "The", remove SMB-unsafe characters, find & replace / regex,
  optionally folders too.

Every proposal shows in a table: untick or edit the new name (double-click). Problems (target exists, two
items → same name, invalid name) are flagged and never applied. **`.m3u` playlists are updated** when
discs are renamed (and renamed themselves when all of their discs are). Nothing changes until **Apply**;
existing files are never overwritten. Every batch is journaled in `~/.local/share/romtools/rename-journal/`
and can be reverted from **History / Undo**.

## ROM Converter (chdman GUI)

- **Batch** tab — pick a task, add files/folders (or drag & drop), *Start queue*:
  - *Create CHD (auto)*: `.cue/.gdi/.toc/.nrg` → `createcd`. For `.iso` it **detects CD vs DVD** and shows why
    in the *Detected as* column: the disc's own contents first (PSP `PSP GAME`; PS2 `BOOT2` vs PS1 `BOOT` in
    `SYSTEM.CNF`; Xbox / Xbox 360 / GameCube / Wii / Saturn / Sega CD / Dreamcast headers), then for small PS2
    images the Redump DAT (CD games are listed as .bin/.cue, DVD games as .iso), then the system folder,
    then size. It warns when the target emulator can't read CHD (Dolphin, xemu, Xenia, Cemu, RPCS3).
    *Always DVD* / *Always CD* override it.
  - *Extract CHD (auto)*: CD → `.cue` + `.bin` (one per track for multi-track discs), GD-ROM → `.gdi`,
    DVD → `.iso`, HD → `.img`.
  - *Verify*, *Recompress (copy with new codecs)*, *Info*.
  - Options: output next to source or into a folder, CD/DVD codec presets (default / Zstandard / LZMA) or
    custom codecs, hunk size, jobs at once, CPU threads per job, overwrite, verify-after-create, and
    *move sources to the trash after a verified success* (off by default; only after `chdman verify` passes).
  - While it runs: a **queue status bar** (overall progress weighted by file size, counts per state,
    elapsed, ETA, throughput), a **Now running** card per active job (step "2/2 · Verifying CHD", chdman's
    live phase and compression ratio, step ETA, speed, output name), running rows highlighted with their
    step, and *Follow running job* keeps the live chdman log on screen. Progress is mirrored in the window
    title, the Batch tab label and the bottom status bar, so it's visible from every tab.
  - Existing outputs are skipped unless *Overwrite* is ticked; half-written CHDs are removed on failure or
    Stop; a free-space check runs before starting. Every job keeps its full chdman log.
- **Patch** tab — apply **IPS / EBP, BPS, UPS, PPF (1/2/3) and xdelta** patches (translations, hacks, fixes),
  several in a row if needed. Works on plain ROMs, single-game zip/7z/rar, `.cue` sets, `.iso` and **CHDs**
  (unpacked, patched and rebuilt as a new CHD of the same type, then `chdman verify`'d). The original is
  identified against your DATs first; BPS/UPS checksums are enforced (wrong ROM or already-patched ROM is
  refused) and copier headers are handled automatically. The original is never modified, an existing file is
  never overwritten, and the result can be tagged (Translation, Hack…) so ROM Verifier lists it as *Modified*.
- **chdman commands** tab — every chdman command with a form generated from `chdman help` (so new options
  appear automatically after an update), live command-line preview, *Run now* or *Add to batch queue*.
- **chdman version & updates** tab — shows the selected chdman, the latest official MAME release and your
  pacman package. Update via *pacman* (opens a terminal: `sudo pacman -Syu mame-tools`) or *Build latest
  from official source* (MAME's GitHub tag, tools only, built in RAM when possible, installed to
  `~/.local/share/romtools/chdman/<tag>/`). Choose which chdman all three apps use.

## Files

| Path | What |
|---|---|
| `~/.config/romtools/config.json` | library path, folder → system/DAT mapping, settings |
| `~/.local/share/romtools/dats/` | downloaded / imported DATs |
| `~/.local/share/romtools/rename-journal/` | rename history (undo) |
| `~/.local/share/romtools/marks.json` | your hack / translation tags |
| `~/.cache/romtools/hashes.sqlite` | hash cache |
| `~/.local/share/romtools/chdman/` | chdman builds made by the updater |
