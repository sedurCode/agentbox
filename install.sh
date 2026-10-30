#!/usr/bin/env bash
# Install agentbox into ~/.local/share/agentbox, link ~/.local/bin/agentbox,
# and seed ~/.config/agentbox (never overwrites an existing config).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${AGENTBOX_HOME:-$HOME/.local/share/agentbox}"
BIN="$HOME/.local/bin"
CFG="${AGENTBOX_CONFIG_DIR:-$HOME/.config/agentbox}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 - $2" >&2; exit 1; }; }

need sbx "brew trust docker/tap && brew install docker/tap/sbx"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3,11))'; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "need Python 3.11+ (brew install python)" >&2; exit 1; }

mkdir -p "$DEST/bin" "$DEST/share" "$BIN" "$CFG/kits" "$CFG/gdb"
cp "$SRC/share/hosttool" "$DEST/share/hosttool"
# pin the interpreter so `agentbox` works even if python3 on PATH is older
{ echo "#!$PY"; tail -n +2 "$SRC/bin/agentbox"; } > "$DEST/bin/agentbox"
chmod 0755 "$DEST/bin/agentbox" "$DEST/share/hosttool"
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
echo "done. next: sbx login, then edit $CFG/agentbox.toml, then: agentbox doctor"
