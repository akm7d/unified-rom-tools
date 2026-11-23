#!/usr/bin/env bash
# Installs launchers and menu entries for ROM Verifier and ROM Renamer (per-user, no root).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$BIN" "$APPS" "$ICONS"

missing=()
python3 -c 'import PySide6' 2>/dev/null || missing+=(pyside6)
python3 -c 'import cryptography' 2>/dev/null || missing+=(python-cryptography)
command -v chdman >/dev/null || missing+=(mame-tools)
command -v 7z >/dev/null || missing+=(7zip)
if ((${#missing[@]})); then
  echo "Missing packages: ${missing[*]}"
  echo "Install with:  sudo pacman -S ${missing[*]}"
fi

# nodtool (RVZ/CISO checks and conversion), nsz (NSZ) and xdelta3 (patches): the same pinned, checksum-verified
# binaries the packaged builds bundle, fetched into ./tools
if [ "$(uname -s)" = Linux ]; then
  python3 "$HERE/packaging/build.py" tools || echo "Could not fetch nodtool/nsz/xdelta3 (offline?) - re-run later"
fi

for app in verifier renamer converter; do
  name="rom-$app"; [ "$app" = converter ] && name=rom-optimizer
  cat > "$BIN/$name" <<SH
#!/bin/sh
export PYTHONPATH="$HERE\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m romtools.gui.$app "\$@"
SH
  chmod +x "$BIN/$name"
  cp "$HERE/icons/$name.svg" "$ICONS/$name.svg"
done

cat > "$APPS/rom-verifier.desktop" <<D
[Desktop Entry]
Type=Application
Name=ROM Verifier
GenericName=ROM hash checker
Comment=Verify ROMs and CHDs against No-Intro and Redump DATs
Exec=$BIN/rom-verifier
Icon=rom-verifier
Terminal=false
Categories=Game;Utility;Emulator;
Keywords=rom;chd;redump;nointro;dat;hash;verify;
StartupWMClass=rom-verifier
D
cat > "$APPS/rom-renamer.desktop" <<D
[Desktop Entry]
Type=Application
Name=ROM Renamer
GenericName=ROM renaming tool
Comment=Rename ROMs to their No-Intro / Redump names, with preview and undo
Exec=$BIN/rom-renamer
Icon=rom-renamer
Terminal=false
Categories=Game;Utility;Emulator;
Keywords=rom;rename;redump;nointro;dat;m3u;
StartupWMClass=rom-renamer
D
cat > "$APPS/rom-optimizer.desktop" <<D
[Desktop Entry]
Type=Application
Name=ROM Optimizer
GenericName=ROM converter, compressor and patcher
Comment=Convert, compress, trim, patch and fix ROMs and disc images; find space savings
Exec=$BIN/rom-optimizer
Icon=rom-optimizer
Terminal=false
Categories=Game;Utility;Emulator;
Keywords=rom;chd;chdman;convert;compress;patch;rvz;nsz;trim;iso;cue;gdi;
StartupWMClass=rom-optimizer
D
# renamed from ROM Converter in 1.3.0: keep the old command working, drop the old menu entry
ln -sf "$BIN/rom-optimizer" "$BIN/rom-converter"
rm -f "$APPS/rom-converter.desktop" "$ICONS/rom-converter.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Installed: rom-verifier, rom-renamer, rom-optimizer (rom-converter still works) (menu: Games / Utilities)"
