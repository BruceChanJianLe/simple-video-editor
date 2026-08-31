#!/usr/bin/env bash
# Run a command against a throwaway X server.
#
# GUI end-to-end checks need a real X server, not Qt's `offscreen` platform:
# offscreen skips QVideoWidget's compositing path, which is the thing those
# checks exist to verify. It also cannot be captured with x11grab.
#
#   tools/with-xvfb.sh [--size WxH] -- command...
set -euo pipefail

SIZE=1600x1000
while [[ $# -gt 0 ]]; do
  case "$1" in
    --size) SIZE="$2"; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done

for n in $(seq 90 120); do
  if [[ ! -e "/tmp/.X${n}-lock" ]]; then DISP=":${n}"; break; fi
done
: "${DISP:?could not find a free X display number}"

Xvfb "$DISP" -screen 0 "${SIZE}x24" -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
trap 'kill $XVFB_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  if xdpyinfo -display "$DISP" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

export DISPLAY="$DISP"
export QT_QPA_PLATFORM=xcb
export SVE_XVFB_SIZE="$SIZE"
"$@"
