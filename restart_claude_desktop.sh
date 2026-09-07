#!/usr/bin/env bash
# Restart Claude Desktop so it picks up the atspi-desktop MCP server.
#
# Runs detached and sleeps first, because it kills the process tree that
# launched it. The relaunch goes through gtk-launch so the overridden
# .desktop file applies (which also gives Claude Desktop itself
# --force-renderer-accessibility); a direct exec is the fallback.

set -u

RESTART_DELAY_SECONDS="${1:-25}"
LOG_FILE=/tmp/claude-desktop-restart.log

export DISPLAY="${DISPLAY:-:0}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

log() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LOG_FILE"; }

: >"$LOG_FILE"
log "waiting ${RESTART_DELAY_SECONDS}s before restarting Claude Desktop"
sleep "$RESTART_DELAY_SECONDS"

# The bracket keeps this pattern from matching the script's own command line.
log "stopping claude-desktop"
pkill -TERM -f '/usr/lib/claude-desktop/claude-[d]esktop'
for _ in $(seq 1 15); do
    pgrep -f '/usr/lib/claude-desktop/claude-[d]esktop' >/dev/null || break
    sleep 1
done
if pgrep -f '/usr/lib/claude-desktop/claude-[d]esktop' >/dev/null; then
    log "did not exit on TERM, sending KILL"
    pkill -KILL -f '/usr/lib/claude-desktop/claude-[d]esktop'
    sleep 3
fi
log "stopped"

sleep 2

if setsid gtk-launch com.anthropic.Claude >>"$LOG_FILE" 2>&1 </dev/null; then
    log "relaunched via gtk-launch com.anthropic.Claude"
else
    log "gtk-launch failed, falling back to direct exec"
    setsid /usr/bin/claude-desktop >>"$LOG_FILE" 2>&1 </dev/null &
fi

sleep 12
if pgrep -f '/usr/lib/claude-desktop/claude-[d]esktop' >/dev/null; then
    log "OK: claude-desktop is running"
    log "flag present: $(ps -eo args | grep -c 'force-renderer-[a]ccessibility')"
else
    log "PROBLEM: claude-desktop is not running; start it from the menu"
fi
