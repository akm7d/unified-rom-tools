#!/usr/bin/env bash
# Optional: adds the portable ROM Verifier / Renamer / Optimizer to your app menu and ~/.local/bin.
# The programs also run straight from this folder without it. Re-run after moving the folder.
# chdman and 7-Zip are bundled in tools/, nothing else needs installing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$BIN" "$APPS" "$ICONS"
# the Exec= value of a menu entry: quoted, so any folder name works
exec_value() {
  local s="$1"
  s="${s//\\/\\\\\\\\}"; s="${s//\"/\\\\\"}"; s="${s//\`/\\\\\`}"; s="${s//\$/\\\\\$}"; s="${s//%/%%}"
  printf '"%s"' "$s"
}
declare -A NAME=([verifier]="ROM Verifier" [renamer]="ROM Renamer" [optimizer]="ROM Optimizer")
declare -A DESC=([verifier]="Verify ROMs and CHDs against No-Intro and Redump DATs"
                 [renamer]="Rename ROMs to their No-Intro / Redump names, with preview and undo"
                 [optimizer]="Convert, compress, trim, patch and fix ROMs and disc images; find space savings")
for app in verifier renamer optimizer; do
  ln -sf "$HERE/rom-$app" "$BIN/rom-$app"
  cp "$HERE/icons/rom-$app.svg" "$ICONS/rom-$app.svg"
  cat > "$APPS/rom-$app.desktop" <<D
[Desktop Entry]
Type=Application
Name=${NAME[$app]}
Comment=${DESC[$app]}
Exec=$(exec_value "$HERE/rom-$app")
Icon=rom-$app
Terminal=false
Categories=Game;Utility;Emulator;
StartupWMClass=rom-$app
D
done
# renamed from ROM Converter in 1.3.0: keep the old command working, drop the old menu entry
ln -sf "$HERE/rom-optimizer" "$BIN/rom-converter"
rm -f "$APPS/rom-converter.desktop" "$ICONS/rom-converter.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "Installed ROM Verifier, ROM Renamer and ROM Optimizer (menu: Games / Utilities)."
