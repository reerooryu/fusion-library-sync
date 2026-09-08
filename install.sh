#!/usr/bin/env bash
# Detent installer.
#
#   curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
#
# Downloads the latest release and unpacks it into Fusion 360's AddIns folder.
# Your config.json and state/ are left alone, so this is also the upgrade path.
#
# Piping a script into a shell means trusting whatever it serves. Read it first
# if you would rather: the URL above is the file, unrendered.

set -euo pipefail

REPO="reerooryu/fusion-library-sync"
ASSET="Detent.tgz"
NAME="Detent"

die() { printf '\n%s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# --- where Fusion keeps add-ins
case "$(uname -s)" in
  Darwin)
    ADDINS="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns" ;;
  MINGW*|MSYS*|CYGWIN*)
    ADDINS="${APPDATA:-$HOME/AppData/Roaming}/Autodesk/Autodesk Fusion 360/API/AddIns" ;;
  *)
    die "Fusion 360 runs on macOS and Windows only; this looks like $(uname -s)." ;;
esac

command -v curl >/dev/null || die "curl is required."
command -v tar  >/dev/null || die "tar is required."

# The AddIns folder exists once Fusion has been run at least once. Creating it
# ourselves would install into a path Fusion may never look at, so say so
# instead of guessing.
[ -d "$ADDINS" ] || die "Fusion's AddIns folder is not there:
  $ADDINS
Install and launch Fusion 360 once, then run this again."

DEST="$ADDINS/$NAME"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

URL="https://github.com/$REPO/releases/latest/download/$ASSET"
say "Downloading $ASSET ..."
curl -fsSL "$URL" -o "$TMP/$ASSET" \
  || die "Download failed: $URL
No published release, or no network."

tar xzf "$TMP/$ASSET" -C "$TMP" || die "Archive is not readable."
[ -f "$TMP/$NAME/Detent.py" ] || die "Archive does not contain $NAME/Detent.py."

# Old layouts shipped the package as core/, which collides with other add-ins;
# leaving it behind would let a stale copy win on sys.path.
if [ -d "$DEST" ]; then
  say "Updating $DEST"
  rm -rf "$DEST/core" "$DEST/__pycache__" "$DEST/detent_core"
else
  say "Installing to $DEST"
fi

mkdir -p "$DEST"
# -m so extracted files are newer than any .pyc left from a previous version.
tar xzmf "$TMP/$ASSET" -C "$ADDINS"
find "$DEST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$DEST/Detent.manifest" 2>/dev/null || true)"

say ""
say "Detent ${VERSION:-?} installed."
say ""
say "In Fusion: Utilities > ADD-INS > Scripts and Add-Ins > Add-Ins tab,"
say "select Detent, then Run. The command appears under Utilities > ADD-INS."
say ""
say "Config: $DEST/config.json"
