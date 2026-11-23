#!/usr/bin/env bash
# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Install agentbox into ~/.local/share/agentbox, link ~/.local/bin/agentbox,
# and seed ~/.config/agentbox (never overwrites an existing config).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${AGENTBOX_HOME:-$HOME/.local/share/agentbox}"
BIN="$HOME/.local/bin"
CFG="${AGENTBOX_CONFIG_DIR:-$HOME/.config/agentbox}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 - $2" >&2; exit 1; }; }

need sbx "on macOS: brew trust docker/tap && brew install docker/tap/sbx; on Linux see docs.docker.com/sandboxes"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3,11))'; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "need Python 3.11+ (macOS: brew install python; Linux: your package manager)" >&2; exit 1; }
echo "found $PY"

mkdir -p "$DEST/bin" "$DEST/share" "$BIN" "$CFG/kits" "$CFG/gdb"
cp "$SRC/share/hosttool" "$DEST/share/hosttool"
cp "$SRC/share/jobwrap" "$DEST/share/jobwrap"
cp "$SRC/share/jobprogress" "$DEST/share/jobprogress"
mkdir -p "$DEST/share/skill"
cp -R "$SRC/share/skill/." "$DEST/share/skill/"
# the script is installed unchanged; a launcher picks a Python 3.11+ at run time, so
# removing the interpreter found above doesn't break agentbox
cp "$SRC/bin/agentbox" "$DEST/bin/agentbox.py"
cp "$SRC/share/agentbox-launcher" "$DEST/bin/agentbox"
chmod 0755 "$DEST/bin/agentbox" "$DEST/bin/agentbox.py" "$DEST/share/hosttool"
ln -sf "$DEST/bin/agentbox" "$BIN/agentbox"

for k in "$SRC"/kits/*/; do
  name="$(basename "$k")"
  if [ -e "$CFG/kits/$name" ]; then echo "kept existing kit $CFG/kits/$name"
  else cp -R "$k" "$CFG/kits/$name"; echo "installed kit $name"; fi
done
for g in "$SRC"/config/gdb/*.gdb; do
  [ -e "$CFG/gdb/$(basename "$g")" ] || cp "$g" "$CFG/gdb/"
done
if [ -e "$CFG/agentbox.toml" ]; then
  echo "kept existing $CFG/agentbox.toml"
else
  cp "$SRC/config/agentbox.example.toml" "$CFG/agentbox.toml"
  echo "created $CFG/agentbox.toml - edit the profiles before first use"
fi
chmod 700 "$CFG"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to PATH";; esac
echo "agents on this machine (Claude Code, Codex, OpenCode, Pi): \`agentbox skill setup\` installs the agentbox skill"
echo "done. next: sbx login, then edit $CFG/agentbox.toml, then: agentbox doctor"
