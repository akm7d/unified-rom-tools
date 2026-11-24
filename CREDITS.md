# Credits

Unified Rom Tools stands on the work of many open-source projects and community databases. Thank you to all
of their authors and contributors. Every bundled program is the official, unmodified release (pinned and
checksum-verified in `packaging/versions.toml`) and keeps its own license; the license notes ship next to each
one in the builds' `tools/` folder.

## Bundled programs

| Tool | Used for | Project | License |
|---|---|---|---|
| **chdman** (part of MAME) | creating, extracting, verifying CHDs | [mamedev/mame](https://github.com/mamedev/mame) · [mamedev.org](https://www.mamedev.org) | GPL-2.0-or-later |
| **7-Zip** (7zz / 7z.exe) | reading zip / 7z / rar archives | [7-zip.org](https://www.7-zip.org) · [ip7z/7zip](https://github.com/ip7z/7zip) | LGPL-2.1 (+ unRAR restriction, BSD parts) |
| **xdelta3** | applying xdelta / VCDIFF patches | [jmacd/xdelta](https://github.com/jmacd/xdelta) | Apache-2.0 |
| **nodtool** (nod) | GameCube / Wii RVZ, CISO, GCZ, WIA, WBFS: converting and verifying | [encounter/nod](https://github.com/encounter/nod) | Apache-2.0 or MIT |
| **nsz** | Switch NSP / XCI ↔ NSZ / XCZ | [nicoboss/nsz](https://github.com/nicoboss/nsz) | MIT |
| **nstool** | Switch RomFS / ExeFS extraction, applying updates | [jakcron/nstool](https://github.com/jakcron/nstool) | MIT |

## Libraries and runtimes in the builds

| Library | Used for | Project | License |
|---|---|---|---|
| **Python** | the programs themselves | [python.org](https://www.python.org) | PSF License |
| **python-build-standalone** | the portable Python inside the Linux build | [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) | MPL-2.0 / PSF |
| **Qt** via **PySide6** (Qt for Python) | the windows and widgets | [qt.io](https://www.qt.io/qt-for-python) | LGPL-3.0 |
| **cryptography** | reading Switch package metadata with your own keys | [pyca/cryptography](https://github.com/pyca/cryptography) | Apache-2.0 or BSD-3-Clause |
| **PyInstaller** | packaging the Linux and Windows builds | [pyinstaller.org](https://pyinstaller.org) | GPL-2.0 with bootloader exception |
| **Wine** | building the Windows version on Linux (build machine only) | [winehq.org](https://www.winehq.org) | LGPL-2.1 |

## Databases and online sources

| Source | Used for |
|---|---|
| **[Redump](http://redump.org)** | disc-game DATs (PlayStation, Dreamcast, GameCube, Xbox, …) |
| **[No-Intro](https://no-intro.org)**, via the **[libretro-database](https://github.com/libretro/libretro-database)** mirror | cartridge and handheld DATs |
| **[libretro-database](https://github.com/libretro/libretro-database)** System.dat | known BIOS hashes (*BIOS files*, downloaded on first use) |
| **[blawar/titledb](https://github.com/blawar/titledb)** | newest official Switch update versions (*Check game updates*) |
| **[orbispatches.com](https://orbispatches.com)** | PS4 patch pages (opened in your browser, never scraped) |
| **[MAME releases on GitHub](https://github.com/mamedev/mame/releases)** | chdman version check and updates |
| **GitHub Releases** | checking for and downloading new versions of Unified Rom Tools |

## Documentation and inspiration

* **[Switchbrew](https://switchbrew.org)** and the hactool / LibHac / nstool sources: documentation of the Switch
  NSP, XCI, NCA, CNMT, NACP and RomFS formats that the package reader follows.
* **[LibOrbisPkg](https://github.com/maxton/LibOrbisPkg)** and the shadPS4 sources: how the PS4 PFS / PFSC formats are laid out.
* **[ZArchive](https://github.com/Exzap/ZArchive)** by Exzap: the layout of Cemu's `.wua` archives.
* **[PS4 Developer Wiki](https://www.psdevwiki.com/ps4/)**: the PS4 PKG and PARAM.SFO formats.
* **[PS4PKGTool](https://github.com/pearlxcore/PS4PKGTool)** by pearlxcore: the ideas for PS4 package
  identification, renaming and update checks.
* **[RomM](https://github.com/rommapp/romm)**, **ES-DE** and **Batocera**: the library folder layout.
* **[ClrMamePro](https://mamedev.emulab.it/clrmamepro/)** / Logiqx: the DAT formats.

## Made with AI

Unified Rom Tools was written by **Claude**, an AI model made by [Anthropic](https://www.anthropic.com), working
with and directed by its maintainer ([akm7d](https://github.com/akm7d)), who tested it on a real library and
publishes it. See the disclaimer in the [README](README.md#disclaimer).

## Not included

No console keys, BIOS files, firmware or game data are included or downloaded. You supply your own
`prod.keys` / `title.keys` and your own dumps. Nintendo, Sony, Sega and Microsoft names are trademarks of their
owners; this project is not affiliated with or endorsed by them or by any of the projects above.
