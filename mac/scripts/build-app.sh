#!/bin/bash
# Build Roughcut.app from this Swift package.
#
#   mac/scripts/build-app.sh            -> mac/build/Roughcut.app
#   mac/scripts/build-app.sh --install  -> also copies it to /Applications
#
# Signing: ad-hoc by default, which gives each rebuild a new identity, so
# macOS may ask again for Keychain and folder access after rebuilding. To
# avoid that, sign with your (free) Apple Development certificate:
#   ROUGHCUT_SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" mac/scripts/build-app.sh
# (`security find-identity -v -p codesigning` lists yours.)
#
# Needs Xcode or the Command Line Tools (xcode-select --install). The app
# remembers where this repo's CLI lives (.venv/bin/roughcut), so set up the
# Python side first (see the repo README).
set -euo pipefail

MAC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$MAC_DIR/.." && pwd)"
CLI_PATH="$REPO_ROOT/.venv/bin/roughcut"
APP="$MAC_DIR/build/Roughcut.app"
VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"

# Which code this app is built from, written into Info.plist for Settings to show beside the engine's commit
# (`roughcut --version`). RoughcutAppCommit: the checkout's short commit, with -dirty after it when the app's own files
# (APP_PATHS: not mac/'s README or tests) have uncommitted changes, edited or new. RoughcutAppSourceCommit: the last commit
# that touched them, which is what says whether the app is behind after `roughcut update`. Both are "unknown" outside a
# git checkout of its own (a folder inside some other repository is not one) and when git can't say whether the files are
# changed: never a clean commit nobody checked. The same git commands and the same list as roughcut/update.py's commit_info
# (tests/test_presets_paths_update.py runs this block), so the two compare. --no-optional-locks: don't touch .git/index.
# --- app stamp (begin)
APP_PATHS=(mac/Sources mac/Package.swift mac/Resources mac/scripts)
APP_COMMIT="unknown"
APP_SOURCE_COMMIT="unknown"
TOP="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$TOP" ] && [ "$TOP" -ef "$REPO_ROOT" ]; then
    if SHA="$(git -C "$REPO_ROOT" rev-parse --short=7 HEAD 2>/dev/null)" && [ -n "$SHA" ] \
        && CHANGED="$(git --no-optional-locks -C "$REPO_ROOT" status --porcelain -- "${APP_PATHS[@]}" 2>/dev/null)"; then
        APP_COMMIT="$SHA"
        if [ -n "$CHANGED" ]; then
            APP_COMMIT="${APP_COMMIT}-dirty"
        fi
        APP_SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-list -1 --abbrev-commit --abbrev=7 HEAD -- "${APP_PATHS[@]}" 2>/dev/null || true)"
        APP_SOURCE_COMMIT="${APP_SOURCE_COMMIT:-unknown}"
    fi
fi
# --- app stamp (end)

cd "$MAC_DIR"
swift build -c release --product Roughcut
BIN_DIR="$(swift build -c release --show-bin-path)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/Roughcut" "$APP/Contents/MacOS/Roughcut"

ICON_KEY=""
if [ -f "$MAC_DIR/Resources/AppIcon.png" ]; then
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 128 256 512; do
        sips -z $size $size "$MAC_DIR/Resources/AppIcon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z $double $double "$MAC_DIR/Resources/AppIcon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
    ICON_KEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Roughcut</string>
    <key>CFBundleDisplayName</key><string>Roughcut</string>
    <key>CFBundleIdentifier</key><string>com.roughcut.app</string>
    <key>CFBundleExecutable</key><string>Roughcut</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
    <key>NSHighResolutionCapable</key><true/>
    ${ICON_KEY}
</dict>
</plist>
PLIST
# Added with plutil so any characters in the path are escaped properly.
plutil -insert RoughcutCLIPath -string "$CLI_PATH" "$APP/Contents/Info.plist"
plutil -insert RoughcutAppCommit -string "$APP_COMMIT" "$APP/Contents/Info.plist"
plutil -insert RoughcutAppSourceCommit -string "$APP_SOURCE_COMMIT" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

# Enough to run locally on Apple Silicon. Not notarized.
codesign --force --sign "${ROUGHCUT_SIGN_IDENTITY:--}" "$APP" >/dev/null
echo "Built $APP (commit $APP_COMMIT, app source $APP_SOURCE_COMMIT)"

if [ ! -x "$CLI_PATH" ]; then
    echo "Note: $CLI_PATH doesn't exist yet. Set up the Python CLI (repo README) or pick it in Settings."
fi

if [ "${1:-}" = "--install" ]; then
    rm -rf /Applications/Roughcut.app
    cp -R "$APP" /Applications/
    echo "Installed /Applications/Roughcut.app"
fi
