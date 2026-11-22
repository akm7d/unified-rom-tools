romtools for Windows - ROM Verifier, ROM Renamer, ROM Converter
================================================================

Start:  double-click "ROM Verifier.exe", "ROM Renamer.exe" or "ROM Converter.exe".
        Keep the whole folder together (the _internal and tools folders are needed).
        Tip: right-click an .exe > Send to > Desktop (create shortcut).

Included tools (tools folder):
  chdman.exe  - from MAME's official release (see chdman-LICENSE.txt)
  7z.exe/.dll - 7-Zip (see 7-Zip-License.txt), used for .7z/.rar archives

Differences from the Linux version:
  * CHDs are always unpacked to temporary files ("SSD mode"). The RAM streaming mode
    relies on Linux features and is not available on Windows. Temp folder: Settings in
    ROM Verifier (default %LOCALAPPDATA%\romtools\tmp).
  * chdman updates: ROM Converter > "chdman version & updates" > "Download latest official
    chdman.exe" downloads MAME's official Windows release, checks its SHA-256 and keeps only
    chdman.exe.
  * Files moved to the trash go to the Recycle Bin.

Settings:   %APPDATA%\romtools\config.json
DATs, tags: %LOCALAPPDATA%\romtools\
Cache:      %LOCALAPPDATA%\Cache\romtools\

Uninstall: delete this folder (and the folders above if you want to remove settings).
