#!/usr/bin/env bash
# Detent installer for Mac (and Git Bash on Windows).
#
#   curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
#
# Downloads the latest release into Fusion 360's AddIns folder. Running it
# again updates Detent and keeps your config.json and state/.
#
# On Windows without Git Bash, use Detent.zip instead. See the README.

set -euo pipefail

REPO="reerooryu/fusion-library-sync"
ASSET="Detent.tgz"
NAME="Detent"

die() { printf '\n%s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# Where Fusion keeps add-ins.
case "$(uname -s)" in
  Darwin)
    ADDINS="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns" ;;
  MINGW*|MSYS*|CYGWIN*)
    ADDINS="${APPDATA:-$HOME/AppData/Roaming}/Autodesk/Autodesk Fusion 360/API/AddIns" ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then
      die "This is WSL, which can't see Fusion's folders.
Download Detent.zip from the latest release instead:
  https://github.com/$REPO/releases/latest
The README has step-by-step instructions."
    fi
    die "Fusion 360 only runs on Mac and Windows." ;;
  *)
    die "Fusion 360 only runs on Mac and Windows." ;;
esac

command -v curl >/dev/null || die "curl is required."
command -v tar  >/dev/null || die "tar is required."

# This folder exists once Fusion has been opened.
[ -d "$ADDINS" ] || die "Can't find Fusion's AddIns folder:
  $ADDINS
Open Fusion 360 once, then run this again."

DEST="$ADDINS/$NAME"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

URL="https://github.com/$REPO/releases/latest/download/$ASSET"
say "Downloading $ASSET ..."
curl -fsSL "$URL" -o "$TMP/$ASSET" \
  || die "Download failed: $URL
Check your internet connection."

tar xzf "$TMP/$ASSET" -C "$TMP" || die "The download is damaged. Try again."
[ -f "$TMP/$NAME/Detent.py" ] || die "The download doesn't contain Detent.py."

# Remove old code before copying the new version in. Versions before 0.3.1
# used a folder called core/, which would conflict if left behind.
if [ -d "$DEST" ]; then
  say "Updating $DEST"
  rm -rf "$DEST/core" "$DEST/__pycache__" "$DEST/detent_core"
else
  say "Installing to $DEST"
fi

mkdir -p "$DEST"
# -m makes the new files newer than any leftover compiled files.
tar xzmf "$TMP/$ASSET" -C "$ADDINS"
find "$DEST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$DEST/Detent.manifest" 2>/dev/null || true)"

say ""
say "Detent ${VERSION:-} installed."
say ""
say "In Fusion: Utilities > ADD-INS > Scripts and Add-Ins > Add-Ins tab,"
say "select Detent, click Run, and tick Run on Startup."
say ""
say "Settings: $DEST/config.json"
