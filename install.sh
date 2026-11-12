#!/bin/sh
# Keep the fleet dashboard running on macOS with launchd: it starts at login and is started again if it stops.
#
#   install.sh server <id> [name ...]   on the always-on Mac: the dashboard, open to the home network on port 8766.
#                                       <id> is that Mac's id in DEVICES; each name is one more DNS name the page
#                                       may be opened under (a Tailscale name, say).
#   install.sh push <ssh-alias> [id]    on a laptop: push its readings to the server (its alias in ~/.ssh/config,
#                                       and this laptop's id in devices.json, default laptop).
#   install.sh remove server|push       stop it and delete its launchd entry.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

label() {
  case "$1" in
    server) echo com.lovespark.fleet-dashboard ;;
    push) echo com.lovespark.fleet-push ;;
    *) echo "unknown service: $1" >&2; exit 2 ;;
  esac
}

stop() {    # label
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  n=0
  while launchctl print "$DOMAIN/$1" >/dev/null 2>&1 && [ $n -lt 20 ]; do sleep 1; n=$((n + 1)); done
}

start() {   # label, log file, then the program and its arguments
  name=$1; log=$2; shift 2
  mkdir -p "$AGENTS"
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0"><dict>\n'
    printf '  <key>Label</key><string>%s</string>\n' "$name"
    printf '  <key>ProgramArguments</key><array>\n'
    for a in "$@"; do printf '    <string>%s</string>\n' "$a"; done
    printf '  </array>\n'
    printf '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>10</integer>\n'
    printf '  <key>StandardOutPath</key><string>%s</string>\n  <key>StandardErrorPath</key><string>%s</string>\n' "$log" "$log"
    printf '</dict></plist>\n'
  } > "$AGENTS/$name.plist"
  stop "$name"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$name.plist"
  echo "$name is running (log: $log)"
}

case "${1:-}" in
  server)
    [ $# -ge 2 ] || { echo "usage: install.sh server <id> [name ...]" >&2; exit 2; }
    id=$2; shift 2
    set -- /usr/bin/python3 -u "$DIR/fleet_dashboard.py" --here "$id" --host 0.0.0.0 --port 8766 \
      $(for n in "$@"; do printf -- '--allow-host %s ' "$n"; done)
    start "$(label server)" "$DIR/server.log" "$@"
    ;;
  push)
    [ $# -ge 2 ] || { echo "usage: install.sh push <ssh-alias> [id]" >&2; exit 2; }
    start "$(label push)" "$DIR/push.log" /bin/sh "$DIR/push_mac.sh" "$2" "${3:-laptop}"
    ;;
  remove)
    name=$(label "${2:-}")
    stop "$name"
    rm -f "$AGENTS/$name.plist"
    echo "$name removed"
    ;;
  *)
    sed -n '2,10p' "$0" >&2; exit 2 ;;
esac
