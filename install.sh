#!/usr/bin/env bash
# Install atspi-mcp: create the venv, register the MCP server, run a smoke test.
#
# Nothing here is machine-specific -- paths come from the checkout location and
# the D-Bus address from the current user's id, so a clone-and-run works as-is.

set -euo pipefail

REPOSITORY_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIRTUALENV_DIRECTORY="$REPOSITORY_DIRECTORY/.venv"
SERVER_NAME="atspi-desktop"

info()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites

info "Checking prerequisites"

[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || warn \
    "No DISPLAY or WAYLAND_DISPLAY. Run this inside your graphical session."

if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    warn "Wayland session: reading the accessibility tree works, but the"
    warn "set_text_via_clipboard fallback needs X11 (wmctrl/xdotool) and will fail."
fi

MISSING_SYSTEM_PACKAGES=()
python3 -c 'import gi' 2>/dev/null || MISSING_SYSTEM_PACKAGES+=("PyGObject (python3-gi)")
python3 - <<'PY' 2>/dev/null || MISSING_SYSTEM_PACKAGES+=("AT-SPI typelib (gir1.2-atspi-2.0)")
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
PY

if [ ${#MISSING_SYSTEM_PACKAGES[@]} -gt 0 ]; then
    printf '\n'
    fail "Missing system packages: ${MISSING_SYSTEM_PACKAGES[*]}
  Debian/Ubuntu/Mint:  sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core xclip xdotool wmctrl
  Fedora:              sudo dnf install python3-gobject at-spi2-core xclip xdotool wmctrl
  Arch:                sudo pacman -S python-gobject at-spi2-core xclip xdotool wmctrl
These cannot be pip-installed into an isolated venv, which is why the venv below
is created with --system-site-packages."
fi

for optional_tool in xclip xdotool wmctrl; do
    command -v "$optional_tool" >/dev/null \
        || warn "$optional_tool not found; set_text_via_clipboard will be unavailable."
done

if command -v gsettings >/dev/null; then
    if [ "$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null)" = "false" ]; then
        info "Enabling toolkit-accessibility (required for GTK apps to publish a tree)"
        gsettings set org.gnome.desktop.interface toolkit-accessibility true
    fi
fi

pgrep -x at-spi-bus-launcher >/dev/null || warn \
    "at-spi-bus-launcher is not running; the accessibility bus may be unavailable."

# ------------------------------------------------------------------------ venv

info "Creating virtualenv at .venv (with --system-site-packages, for PyGObject)"
python3 -m venv --system-site-packages "$VIRTUALENV_DIRECTORY"
"$VIRTUALENV_DIRECTORY/bin/pip" install --quiet --upgrade pip
"$VIRTUALENV_DIRECTORY/bin/pip" install --quiet -r "$REPOSITORY_DIRECTORY/requirements.txt"

info "Verifying the server imports"
"$VIRTUALENV_DIRECTORY/bin/python" -c "import sys; sys.path.insert(0, '$REPOSITORY_DIRECTORY'); import server" \
    2>/dev/null || fail "server.py failed to import. Run it by hand to see why:
  $VIRTUALENV_DIRECTORY/bin/python $REPOSITORY_DIRECTORY/server.py"

# -------------------------------------------------------------- registration

DBUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

if command -v claude >/dev/null; then
    info "Registering '$SERVER_NAME' with Claude Code"
    claude mcp remove "$SERVER_NAME" --scope user >/dev/null 2>&1 || true
    claude mcp add "$SERVER_NAME" --scope user \
        --env "DISPLAY=${DISPLAY:-:0}" \
        --env "DBUS_SESSION_BUS_ADDRESS=$DBUS_ADDRESS" \
        -- "$VIRTUALENV_DIRECTORY/bin/python" "$REPOSITORY_DIRECTORY/server.py"
else
    warn "The 'claude' CLI was not found; skipping Claude Code registration."
fi

CLAUDE_DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
if [ -f "$CLAUDE_DESKTOP_CONFIG" ]; then
    info "Adding '$SERVER_NAME' to Claude Desktop config"
    cp "$CLAUDE_DESKTOP_CONFIG" "$CLAUDE_DESKTOP_CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
    SERVER_NAME="$SERVER_NAME" \
    VENV_PYTHON="$VIRTUALENV_DIRECTORY/bin/python" \
    SERVER_SCRIPT="$REPOSITORY_DIRECTORY/server.py" \
    DISPLAY_VALUE="${DISPLAY:-:0}" \
    DBUS_ADDRESS="$DBUS_ADDRESS" \
    CONFIG_PATH="$CLAUDE_DESKTOP_CONFIG" \
    python3 - <<'PY'
import json, os

config_path = os.environ["CONFIG_PATH"]
with open(config_path) as handle:
    configuration = json.load(handle)

configuration.setdefault("mcpServers", {})[os.environ["SERVER_NAME"]] = {
    "command": os.environ["VENV_PYTHON"],
    "args": [os.environ["SERVER_SCRIPT"]],
    "env": {
        "DISPLAY": os.environ["DISPLAY_VALUE"],
        "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_ADDRESS"],
    },
}

with open(config_path, "w") as handle:
    json.dump(configuration, handle, indent=2)
    handle.write("\n")
print("   updated (a timestamped backup was kept alongside it)")
PY
    warn "Claude Desktop must be fully quit and reopened to load the server."
else
    info "No Claude Desktop config found; skipping (Claude Code registration is enough)."
fi

# ------------------------------------------------------------------ smoke test

info "Smoke test: listing windows on the accessibility bus"
if "$VIRTUALENV_DIRECTORY/bin/python" - <<PY
import sys
sys.path.insert(0, "$REPOSITORY_DIRECTORY")
import server
listing = server.list_windows()
print("\n".join(listing.splitlines()[:12]))
sys.exit(0 if "application" in listing else 1)
PY
then
    info "Installed."
else
    warn "The bus answered but no applications were visible. Open a GTK or Qt app and retry."
fi

cat <<'NEXT'

Next steps
  - Electron/Chromium apps expose nothing until their launcher carries a flag:
        python3 enable_chromium_accessibility.py --list     # see what was detected
        python3 enable_chromium_accessibility.py            # apply
        python3 enable_chromium_accessibility.py --revert   # undo
    Restart those apps afterwards.
  - Full tool reference and measured limitations: README.md
NEXT
