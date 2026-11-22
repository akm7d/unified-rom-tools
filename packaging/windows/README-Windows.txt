romtools for Windows - ROM Verifier, ROM Renamer, ROM Optimizer
================================================================

Start:  double-click "ROM Verifier.exe", "ROM Renamer.exe" or "ROM Optimizer.exe".
        Keep the whole folder together (the _internal and tools folders are needed).
        Tip: right-click an .exe > Send to > Desktop (create shortcut).

Portable: everything is kept in the "data" folder next to the programs (settings, DATs, tags, rename
history, remembered scans), so the whole folder can live on a USB stick or be copied to another PC.
Nothing is installed and nothing is written to your user profile. Delete portable.txt if you'd rather
keep settings per Windows user (in %APPDATA%). CHDs are unpacked to the system temp folder (%TEMP%).

Included tools (tools folder):
  chdman.exe  - from MAME's official release (see chdman-LICENSE.txt)
  7z.exe/.dll - 7-Zip (see 7-Zip-License.txt), used for .7z/.rar archives

Differences from the Linux version:
  * CHDs are always unpacked to temporary files ("SSD mode"). The RAM streaming mode
    relies on Linux features and is not available on Windows. Temp folder: Settings in
    ROM Verifier (default %TEMP%\romtools).
  * chdman updates: ROM Optimizer > "chdman (advanced)" > "chdman version & updates" > "Download latest official
    chdman.exe" downloads MAME's official Windows release, checks its SHA-256 and keeps only
    chdman.exe.
  * Files moved to the trash go to the Recycle Bin.

Where things are kept:
  portable (default): data\config\config.json, data\dats, data\marks.json, data\cache next to the programs
  without portable.txt: %APPDATA%\romtools, %LOCALAPPDATA%\romtools, %LOCALAPPDATA%\Cache\romtools

Uninstall: delete this folder.
