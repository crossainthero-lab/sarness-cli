#!/usr/bin/env bash
# Install or upgrade Sarness CLI for the current user. No sudo, idempotent, safe to re-run.
#   ./install.sh            install/upgrade from this source folder (isolated pipx environment)
#   ./install.sh --check    only report what would happen
# Upgrade later with:  sarness upgrade        (re-runs this script from the recorded source folder)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
PY="${SARNESS_PYTHON:-/usr/bin/python3}"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1
say() { printf '%s\n' "$*"; }
run() { if [ "$CHECK" = 1 ]; then say "  would run: $*"; else "$@"; fi; }

command -v pipx >/dev/null || { say "pipx is required (https://pipx.pypa.io). Install it with your package manager, then re-run."; exit 1; }
[ -x "$PY" ] || { say "Python interpreter $PY not found; set SARNESS_PYTHON."; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { say "$PY is older than Python 3.11"; exit 1; }

# --- another executable called 'sarness'? (the desktop app's launcher lives here on some machines)
TARGET="$BIN/sarness"
is_ours() { [ -L "$TARGET" ] && readlink -f "$TARGET" | grep -q '/pipx/venvs/sarness-cli/'; }
if [ -e "$TARGET" ] && ! is_ours; then
  if grep -q 'Launches the Sarness build' "$TARGET" 2>/dev/null; then
    say "• $TARGET is the Sarness DESKTOP launcher; keeping it as $BIN/sarness-desktop"
    [ -e "$BIN/sarness-desktop" ] && { say "  $BIN/sarness-desktop already exists; refusing to overwrite. Resolve by hand."; exit 1; }
    run mkdir -p "$HOME/.local/share/sarness-cli/backups"
    run cp -p "$TARGET" "$HOME/.local/share/sarness-cli/backups/sarness-desktop-launcher.$(date +%Y%m%d%H%M%S)"
    run mv "$TARGET" "$BIN/sarness-desktop"
    # user-level app-grid entry overrides the system one; repoint it so the icon keeps opening the app
    for d in "$HOME/.local/share/applications"/*.desktop; do
      [ -e "$d" ] || continue
      if grep -q "^Exec=$TARGET\$" "$d"; then
        say "  repointing $(basename "$d") -> sarness-desktop"
        run sed -i "s|^Exec=$TARGET\$|Exec=$BIN/sarness-desktop|" "$d"
      fi
    done
    if [ -e /usr/share/applications/Sarness.desktop ] && ! [ -e "$HOME/.local/share/applications/Sarness.desktop" ]; then
      say "  ! /usr/share/applications/Sarness.desktop execs bare 'sarness' and has no user-level override; the icon would now start the CLI."
      say "    Create ~/.local/share/applications/Sarness.desktop with Exec=$BIN/sarness-desktop (or /usr/bin/sarness)."
    fi
  else
    say "$TARGET exists and is not the Sarness desktop launcher. Move it aside first, then re-run."; exit 1
  fi
fi
OTHER="$(type -ap sarness 2>/dev/null | grep -v "^$TARGET\$" || true)"
[ -n "$OTHER" ] && say "• other 'sarness' on PATH (kept, shadowed by the CLI in terminals): $(echo "$OTHER" | tr '\n' ' ')"

say "• installing sarness-cli into an isolated pipx environment (python: $PY)"
run pipx install --editable --force --python "$PY" "$SRC[keyring]"

run mkdir -p "$HOME/.config/sarness-cli"
if [ "$CHECK" = 0 ]; then
  printf '{"source": "%s"}\n' "$SRC" > "$HOME/.config/sarness-cli/install.json"
  chmod 600 "$HOME/.config/sarness-cli/install.json"
fi

# --- PATH: ~/.local/bin must come first in new shells. Add it only if a login shell does not already see it.
if ! bash -lc 'case ":$PATH:" in *":$HOME/.local/bin:"*) exit 0;; *) exit 1;; esac'; then
  say "• ~/.local/bin was not on PATH for login shells; adding it to ~/.profile"
  run sh -c "printf '\n# added by sarness-cli\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n' >> \"$HOME/.profile\""
fi

if [ "$CHECK" = 0 ]; then
  hash -r
  say "• verifying in a fresh login shell:"
  bash -lc 'command -v sarness && sarness --version'
  if bash -lc 'sarness tui path' >/dev/null 2>&1; then
    say "Done. Next: sarness setup (if not done yet), then 'sarness' in a project"
  else
    say "Done. Next: sarness setup, then 'sarness tui install' to build the Sarness terminal"
  fi
fi
