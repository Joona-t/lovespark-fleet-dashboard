#!/bin/sh
# Push this Mac's readings to the fleet dashboard on the always-on Mac.
#
#   push_mac.sh <server-ssh-alias> [device-id]        device-id default: laptop
#
# A laptop has no SSH server to be read through and is not always on the network, so it sends instead: its agent's
# lines go over SSH into the named pipe the server reads for this device (push/<id>.fifo, made by the server).
# FLEET_DIR is the dashboard's folder on the server, relative to its home (default lovespark-fleet-dashboard).
# The pipeline ends when the agent's planned life is over or the link drops; launchd starts it again (install.sh push).
cd "$(dirname "$0")" || exit 1
ALIAS=${1:?usage: push_mac.sh <server-ssh-alias> [device-id]}
PIPE=${FLEET_DIR:-lovespark-fleet-dashboard}/push/${2:-laptop}.fifo
/usr/bin/python3 -u agent_mac.py 3 600 | /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=6 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 -o LogLevel=ERROR "$ALIAS" "test -p $PIPE && cat > $PIPE"
