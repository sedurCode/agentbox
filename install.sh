#!/usr/bin/env bash
# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Install agentbox into ~/.local/share/agentbox, link ~/.local/bin/agentbox, then run `agentbox setup`,
# which seeds ~/.config/agentbox (never overwrites an existing config).
#
# From an unpacked release: ./install.sh. Without one (piped, the one-line install):
#   curl -fsSL https://raw.githubusercontent.com/sedurCode/agentbox/main/install.sh | bash
# downloads the newest release (pre-releases too; AGENTBOX_VERSION=vX.Y.Z picks one), checks it against the
# release's SHA256SUMS and runs that release's own install.sh.
set -euo pipefail

SELF="${BASH_SOURCE[0]:-}"                  # empty when the script is piped into bash
SRC="$(cd "$(dirname "${SELF:-.}")" && pwd)"
DEST="${AGENTBOX_HOME:-$HOME/.local/share/agentbox}"
BIN="$HOME/.local/bin"
CFG="${AGENTBOX_CONFIG_DIR:-$HOME/.config/agentbox}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 - $2" >&2; exit 1; }; }

need sbx "on macOS: brew trust docker/tap && brew install docker/tap/sbx; on Linux see docs.docker.com/sandboxes"
PY=""
for c in python3.15 python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3,11))'; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "need Python 3.11+ (macOS: brew install python; Linux: your package manager)" >&2; exit 1; }
echo "found $PY"

if [ -z "$SELF" ] || [ ! -f "$SRC/bin/agentbox" ] || [ ! -d "$SRC/share" ]; then
  # no unpacked release here: fetch one
  need curl "install curl"
  need tar "install tar"
  REPO="${AGENTBOX_REPO:-sedurCode/agentbox}"
  TAG="${AGENTBOX_VERSION:-}"
  if [ -z "$TAG" ]; then                  # the newest release, pre-releases included (/releases/latest skips them)
    TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=1" | "$PY" -c \
      'import json, sys; r = json.load(sys.stdin); print(r[0]["tag_name"] if r else "")')"
    [ -n "$TAG" ] || { echo "no agentbox release found at github.com/$REPO" >&2; exit 1; }
  fi
  case "$TAG" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "not a release tag: $TAG" >&2; exit 1;; esac
  NAME="agentbox-${TAG#v}"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "downloading agentbox $TAG"
  curl -fsSL "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" -o "$TMP/$NAME.tar.gz" ||
    { echo "could not download agentbox $TAG from github.com/$REPO" >&2; exit 1; }
  curl -fsSL "https://github.com/$REPO/releases/download/$TAG/SHA256SUMS" -o "$TMP/SHA256SUMS" ||
    { echo "release $TAG has no SHA256SUMS: not installing an unverified download" >&2; exit 1; }
  "$PY" - "$TMP" "$NAME.tar.gz" <<'CHECK'
import hashlib, sys
d, name = sys.argv[1], sys.argv[2]
want = {l.split()[1].lstrip("*"): l.split()[0] for l in open(f"{d}/SHA256SUMS") if len(l.split()) == 2}
got = hashlib.sha256(open(f"{d}/{name}", "rb").read()).hexdigest()
if want.get(name) != got:
    sys.exit(f"checksum mismatch for {name}: not installing it")
print(f"verified {name} (sha256 {got[:12]}...)")
CHECK
  tar -xzf "$TMP/$NAME.tar.gz" -C "$TMP"
  [ -f "$TMP/$NAME/install.sh" ] || { echo "$NAME has no install.sh" >&2; exit 1; }
  bash "$TMP/$NAME/install.sh"
  exit $?
fi

mkdir -p "$DEST/bin" "$DEST/share" "$BIN"
cp "$SRC/share/hosttool" "$DEST/share/hosttool"
cp "$SRC/share/jobwrap" "$DEST/share/jobwrap"
cp "$SRC/share/jobprogress" "$DEST/share/jobprogress"
mkdir -p "$DEST/share/skill"
cp -R "$SRC/share/skill/." "$DEST/share/skill/"
# the defaults `agentbox setup` copies into the config dir; replaced whole, so removed files go
rm -rf "$DEST/share/kits" "$DEST/share/config"
cp -R "$SRC/share/kits" "$DEST/share/kits"
cp -R "$SRC/share/config" "$DEST/share/config"
# the VS Code extension (`agentbox vscode install` packs it from here); replaced whole, so removed files go
rm -rf "$DEST/share/vscode"
cp -R "$SRC/share/vscode" "$DEST/share/vscode"
# the Rex actions (used only once you run `agentbox rex setup`; ~/.config/rex/ is never touched here)
rm -rf "$DEST/share/rex"
cp -R "$SRC/share/rex" "$DEST/share/rex"
# the script is installed unchanged; a launcher picks a Python 3.11+ at run time, so
# removing the interpreter found above doesn't break agentbox
cp "$SRC/bin/agentbox" "$DEST/bin/agentbox.py"
cp "$SRC/share/agentbox-launcher" "$DEST/bin/agentbox"
chmod 0755 "$DEST/bin/agentbox" "$DEST/bin/agentbox.py" "$DEST/share/hosttool"
ln -sf "$DEST/bin/agentbox" "$BIN/agentbox"

AGENTBOX_CONFIG_DIR="$CFG" "$DEST/bin/agentbox" setup

case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to PATH";; esac
echo "done (to upgrade, run the new install.sh: it runs \`agentbox setup\` again)"
