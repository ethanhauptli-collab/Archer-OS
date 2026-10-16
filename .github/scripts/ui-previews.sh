#!/bin/bash
# A picture of every screen of the packaged Mac app, light and dark, taken on this Mac.
#   mac/scripts/build-app.sh && .github/scripts/ui-previews.sh FOLDER
# The app opens itself at each screen with made-up content (ROUGHCUT_UI_PREVIEW, see mac/Sources/Roughcut/UIPreview.swift).
# Beside each light picture goes the screen's Accessibility tree (FOLDER/ax/NAME.ax.txt): what a screen reader, and the
# "Mac app end to end" check, find on it.
set -euo pipefail

OUT="${1:?usage: ui-previews.sh FOLDER}"
APP_BIN="mac/build/Roughcut.app/Contents/MacOS/Roughcut"
SCREENS="getready footage-empty footage brief add music effects graphics ready ready-problem failed finetune running-listen running done done-stopped adjust"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT"
TOOLS="$(mktemp -d)"
WINDOW_ID="$TOOLS/window-id"
DRIVE="$TOOLS/ui-drive"
swiftc -O "$HERE/window-id.swift" -o "$WINDOW_ID"
swiftc -O "$HERE/ui-drive.swift" -o "$DRIVE" || echo "ui-drive did not compile: no Accessibility trees"

for appearance in light dark; do
    for screen in $SCREENS; do
        name="$screen"
        if [ "$appearance" = dark ]; then
            name="$screen-dark"
        fi
        ROUGHCUT_UI_PREVIEW="$screen" ROUGHCUT_UI_APPEARANCE="$appearance" "$APP_BIN" > "$OUT/$name.log" 2>&1 &
        pid=$!
        sleep 6
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "$name: the app exited early"
            cat "$OUT/$name.log" || true
            continue
        fi
        if id="$("$WINDOW_ID" "$pid" 2>/dev/null)" && screencapture -x -o -l "$id" "$OUT/$name.png" 2>/dev/null; then
            echo "$name: window $id"
        else
            echo "$name: no window found, taking the whole screen"
            screencapture -x "$OUT/$name.png" || true
        fi
        if [ "$appearance" = light ] && [ -x "$DRIVE" ]; then
            echo "dump $screen" > "$TOOLS/dump.txt"
            "$DRIVE" "$pid" "$TOOLS/dump.txt" "$OUT/ax" > /dev/null 2>&1 || echo "$name: no Accessibility tree"
        fi
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        rm -f "$OUT/$name.log"
        sleep 1
    done
done
ls -la "$OUT"
