#!/usr/bin/env bash
# claude-code-autoresume installer
#
#   ./install.sh          link ccwatch into PATH and print the hook snippet
#   ./install.sh --hook   also merge the StopFailure hook into ~/.claude/settings.json
#   ./install.sh --help
set -euo pipefail

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CC_AUTORESUME_BIN:-$HOME/.local/bin}"
BASE="${CC_AUTORESUME_HOME:-$HOME/.claude/cc-autoresume}"
SETTINGS="$HOME/.claude/settings.json"
PY="$(command -v python3 || echo /usr/bin/python3)"
# The hook is spawned by Claude Code, not by your shell, so bind it to the most
# stable interpreter available rather than whatever a conda/venv PATH resolves to.
HOOK_PY="$PY"
if [ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
  HOOK_PY=/usr/bin/python3
fi
WITH_HOOK=0

for arg in "$@"; do
  case "$arg" in
    --hook) WITH_HOOK=1 ;;
    -h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

echo "==> Checking prerequisites"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' || {
  echo "    python3 >= 3.6 is required"; exit 1; }
echo "    python3: $PY"

case "$(uname -s)" in
  Darwin)
    echo "    platform: macOS (Terminal.app / iTerm2 supported)"
    command -v tmux >/dev/null 2>&1 && echo "    tmux: found (also supported)" ;;
  *)
    if command -v tmux >/dev/null 2>&1; then
      echo "    platform: $(uname -s) -- tmux backend"
    else
      echo "    platform: $(uname -s) -- WARNING: no tmux found."
      echo "      Off macOS, tmux is the only supported way to reach a session."
    fi ;;
esac

echo "==> Creating state directory"
mkdir -p "$BASE" "$BASE/triggers"
echo "    $BASE"

echo "==> Linking ccwatch into $BIN_DIR"
mkdir -p "$BIN_DIR"
chmod +x "$CODE_DIR/ccwatch" "$CODE_DIR/watchdog.py" "$CODE_DIR/hook_stopfailure.py"
ln -sf "$CODE_DIR/ccwatch" "$BIN_DIR/ccwatch"
echo "    $BIN_DIR/ccwatch -> $CODE_DIR/ccwatch"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH. Add it to your shell rc." ;;
esac

HOOK_CMD="$HOOK_PY $CODE_DIR/hook_stopfailure.py"

if [ "$WITH_HOOK" = "1" ]; then
  echo "==> Registering the StopFailure hook in $SETTINGS"
  "$PY" - "$SETTINGS" "$HOOK_CMD" <<'PYEOF'
import json, os, shutil, sys
settings, cmd = sys.argv[1], sys.argv[2]
try:
    with open(settings, encoding="utf-8") as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
except Exception as e:
    print("    settings.json is not valid JSON, nothing changed: %s" % e)
    sys.exit(1)

entries = cfg.get("hooks", {}).get("StopFailure", [])
# Drop any entry we previously installed so re-running is idempotent.
kept = []
for e in entries:
    inner = [h for h in (e.get("hooks") or []) if "hook_stopfailure.py" not in (h.get("command") or "")]
    if inner:
        kept.append(dict(e, hooks=inner))
    elif not e.get("hooks"):
        kept.append(e)
kept.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})

hooks = dict(cfg.get("hooks") or {})
hooks["StopFailure"] = kept
cfg["hooks"] = hooks

if os.path.exists(settings):
    shutil.copy(settings, settings + ".bak")
    print("    backed up to %s.bak" % settings)
os.makedirs(os.path.dirname(settings), exist_ok=True)
tmp = settings + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, settings)
print("    hook registered")
PYEOF
else
  cat <<EOF
==> StopFailure hook (optional but recommended)

    Without it the watchdog still works, just by polling the screen every few
    seconds instead of reacting in about one second.

    Re-run with --hook to have this merged for you, or add it to
    $SETTINGS yourself:

    "hooks": {
      "StopFailure": [
        { "hooks": [ { "type": "command", "command": "$HOOK_CMD", "timeout": 10 } ] }
      ]
    }
EOF
fi

cat <<EOF

==> Done.

    ccwatch check     see what it thinks of your terminals right now
    ccwatch start     start the watchdog (on macOS: from a real terminal window)
    ccwatch hook      verify the hook is registered

    Claude Code sessions that are already running will not pick up a newly
    added hook until they restart.
EOF
