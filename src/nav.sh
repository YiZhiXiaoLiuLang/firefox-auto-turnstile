#!/bin/sh
#
# Focus Firefox and navigate to the URL given as the first argument.
# Kept as a separate script so it can also be invoked manually:
#   docker exec <container> /opt/turnstile-relay/nav.sh https://example.com
#
set -u

URL="${1:?usage: nav.sh <url>}"
DISPLAY="${DISPLAY:-:0}"
export DISPLAY

# Wait briefly for a visible Firefox window.
i=0
while [ "$i" -lt 60 ]; do
    if xdotool search --onlyvisible --class firefox >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

xdotool search --onlyvisible --class firefox windowactivate --sync
xdotool key --clearmodifiers ctrl+l
xdotool type --clearmodifiers --delay 40 "$URL"
sleep 0.2
xdotool key --clearmodifiers Return
