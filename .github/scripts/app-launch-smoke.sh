#!/bin/bash
# Start the packaged app, give it a few seconds, and fail if it crashed.
set -euo pipefail
APP_BIN="mac/build/Roughcut.app/Contents/MacOS/Roughcut"
"$APP_BIN" > app.log 2>&1 &
PID=$!
sleep 8
if kill -0 "$PID" 2>/dev/null; then
    echo "Roughcut.app is running after 8 s"
    kill "$PID"
else
    wait "$PID" || true
    echo "Roughcut.app exited early:"
    cat app.log
    ls ~/Library/Logs/DiagnosticReports 2>/dev/null | tail -5
    exit 1
fi
