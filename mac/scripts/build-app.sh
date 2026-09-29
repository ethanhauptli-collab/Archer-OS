#!/bin/bash
# Build Roughcut.app from this Swift package.
#
#   mac/scripts/build-app.sh            -> mac/build/Roughcut.app
#   mac/scripts/build-app.sh --install  -> also copies it to /Applications
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
    <key>RoughcutCLIPath</key><string>${CLI_PATH}</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough to run locally on Apple Silicon. Not notarized.
codesign --force --sign - "$APP" >/dev/null
echo "Built $APP"

if [ ! -x "$CLI_PATH" ]; then
    echo "Note: $CLI_PATH doesn't exist yet. Set up the Python CLI (repo README) or pick it in Settings."
fi

if [ "${1:-}" = "--install" ]; then
    rm -rf /Applications/Roughcut.app
    cp -R "$APP" /Applications/
    echo "Installed /Applications/Roughcut.app"
fi
