#!/usr/bin/env bash
# Adds the packaged ROM Verifier / Renamer / Converter to your app menu and ~/.local/bin.
# Run from inside the extracted romtools-<version>-linux-x64 folder. Re-run after moving the folder.
# Needs chdman (mame-tools) and 7z (7zip) from your distribution.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$BIN" "$APPS" "$ICONS"
for t in chdman 7z; do
  command -v "$t" >/dev/null || echo "Note: '$t' not found - install mame-tools / 7zip for CHD and archive support."
done
declare -A NAME=([verifier]="ROM Verifier" [renamer]="ROM Renamer" [converter]="ROM Converter")
declare -A DESC=([verifier]="Verify ROMs and CHDs against No-Intro and Redump DATs"
                 [renamer]="Rename ROMs to their No-Intro / Redump names, with preview and undo"
                 [converter]="Convert to and from CHD with chdman, batch jobs, chdman updates")
for app in verifier renamer converter; do
  ln -sf "$HERE/rom-$app" "$BIN/rom-$app"
  cp "$HERE/icons/rom-$app.svg" "$ICONS/rom-$app.svg"
  cat > "$APPS/rom-$app.desktop" <<D
[Desktop Entry]
Type=Application
Name=${NAME[$app]}
Comment=${DESC[$app]}
Exec=$HERE/rom-$app
Icon=rom-$app
Terminal=false
Categories=Game;Utility;Emulator;
StartupWMClass=rom-$app
D
done
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "Installed ROM Verifier, ROM Renamer and ROM Converter (menu: Games / Utilities)."
