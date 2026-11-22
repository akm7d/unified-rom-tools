# romtools — ROM Verifier & ROM Renamer

Two Linux-native Qt (PySide6) desktop apps for a RomM-style library (`roms/<system>/…`).

## Install

```bash
./install.sh
```

Adds `rom-verifier` and `rom-renamer` to `~/.local/bin` and to your app menu (Games / Utilities).
Requirements (already present on this machine): `pyside6`, `mame-tools` (chdman), `7zip`.

## ROM Verifier (read-only)

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

How formats are checked:
- **CHD**: the original tracks are rebuilt with `chdman` (CD split per track, GD-ROM via GDI, DVD as ISO)
  and hashed. The CHD itself is only read. The toolbar switch picks where the rebuilt data goes:
  - **RAM (default, no SSD writes)**: `chdman` writes into `/dev/shm` while the tool hashes right behind
    it and frees each chunk once hashed. If hashing falls behind, `chdman` is paused. Peak RAM is about
    35 MB per CHD, even for 8.5 GB PS2 discs.
  - **SSD**: full temp files in the Settings temp folder (default `/var/tmp/romtools`), deleted after
    hashing. Writes up to a whole disc image per CHD to disk.
  - Switching during a run only affects the next run.
- **zip / 7z / rar**: every member is hashed by streaming (no extraction to disk).
- **cue/bin, gdi** sets: every track hashed.
- **NES / FDS / Lynx / 7800 / SNES copier headers**: hashed with and without the header in one pass.
- **Quick scan (F5)** reuses results remembered from earlier scans and only hashes ROMs that are new or
  changed (by size/date). **Full scan (Shift+F5)** re-hashes everything and replaces what's remembered.
  Right-click → *Full re-scan of selected* does the same for a few rows. Remembered results show up as soon
  as the window opens (`~/.cache/romtools/hashes.sqlite`; Settings → *Forget all remembered scans*).

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

## Files

| Path | What |
|---|---|
| `~/.config/romtools/config.json` | library path, folder → system/DAT mapping, settings |
| `~/.local/share/romtools/dats/` | downloaded / imported DATs |
| `~/.local/share/romtools/rename-journal/` | rename history (undo) |
| `~/.cache/romtools/hashes.sqlite` | hash cache |
