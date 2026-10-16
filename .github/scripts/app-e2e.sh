#!/bin/bash
# The packaged Mac app, driven the way a person drives it (.github/scripts/ui-drive.swift: the Accessibility tree and real mouse
# clicks), on an Apple Silicon Mac with a window server:
#   python -m venv .venv && .venv/bin/python -m pip install -e '.[mac,dev]'
#   mac/scripts/build-app.sh && .github/scripts/app-e2e.sh WORKFOLDER
# It needs ffmpeg, and Accessibility and Screen Recording for this shell (a GitHub macOS runner has both).
#
#   1. First launch with nothing signed in: the Get ready page, Continue without AI, footage handed to the app (`open -a`),
#      Footage > Brief > Add > Ready, Build with no AI through the app's own process runner, the finished cut (checked against
#      Apple's DTD), Adjust > Quick change > Re-cut (version 2, version 1 in history/).
#   2. A run Cancel stops (a stand-in for the command-line tool that speaks the progress protocol and waits).
#   3. A run that ends on a usage limit.
# A picture and the Accessibility tree of the steps go to WORKFOLDER/pictures.
set -euo pipefail

ROOT="$(pwd)"
WORK="${1:?usage: app-e2e.sh WORKFOLDER}"
APP="$ROOT/mac/build/Roughcut.app"
BUNDLE="com.roughcut.app"
SHOTS="$WORK/pictures"
PYTHON="$ROOT/.venv/bin/python"
mkdir -p "$SHOTS"

# The Mac stays awake and the screen lit while the app is driven, and the Dock gets out of the way: on a runner's small screen
# it covers the bottom of the window, where the footer's buttons are, and a click there would land on the Dock.
caffeinate -d -i -t 3600 &
trap 'kill %1 2>/dev/null || true' EXIT
defaults write com.apple.dock autohide -bool true
defaults write com.apple.dock autohide-delay -float 0
killall Dock || true
sleep 3

DRIVE="$WORK/ui-drive"
swiftc -O .github/scripts/ui-drive.swift -o "$DRIVE"

quit_app() {
    osascript -e "tell application id \"$BUNDLE\" to quit" > /dev/null 2>&1 || true
    for _ in $(seq 1 40); do
        pgrep -x Roughcut > /dev/null || return 0
        sleep 0.5
    done
    pkill -x Roughcut || true
}

drive() {
    "$DRIVE" "$BUNDLE" "$1" "$SHOTS"
}

# --- Made-up footage: a person talking (macOS `say`) with a 4 s pause, B-roll, a still; a transcript beside the talk so no
# speech model has to be downloaded. ---
SHOOT="$WORK/Walk and talk"
mkdir -p "$SHOOT"
(
    cd "$SHOOT"
    say -o part1.aiff "Welcome to Orange County. This weekend the food festival takes over the park."
    say -o part2.aiff "Over forty chefs are cooking, and tickets start at twenty dollars."
    ffmpeg -loglevel error -y -i part1.aiff -f lavfi -t 4 -i anullsrc=r=22050:cl=mono -i part2.aiff \
        -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1,aresample=48000[a]" -map "[a]" speech.wav
    DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 speech.wav)
    ffmpeg -loglevel error -y -f lavfi -i "testsrc2=size=1920x1080:rate=24" -i speech.wav -t "$DUR" \
        -map 0:v -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest talk.mp4
    ffmpeg -loglevel error -y -f lavfi -i "mandelbrot=size=1920x1080:rate=24" -t 6 -c:v libx264 -pix_fmt yuv420p broll_park.mp4
    ffmpeg -loglevel error -y -f lavfi -i "color=c=orange:size=1600x1200" -frames:v 1 still_festival.jpg
    D1=$(ffprobe -v error -show_entries format=duration -of csv=p=0 part1.aiff)
    D2=$(ffprobe -v error -show_entries format=duration -of csv=p=0 part2.aiff)
    "$PYTHON" - "$D1" "$D2" talk.srt <<'PY'
import sys

d1, d2, out = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]


def stamp(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


cues = [
    (0.0, d1, "Welcome to Orange County. This weekend the food festival takes over the park."),
    (d1 + 4.0, d1 + 4.0 + d2, "Over forty chefs are cooking, and tickets start at twenty dollars."),
]
with open(out, "w") as f:
    for i, (a, b, text) in enumerate(cues, 1):
        f.write(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n\n")
PY
    rm -f part1.aiff part2.aiff speech.wav
)
ls -la "$SHOOT"

# --- 1. The first launch, a build with no AI, the finished cut, a re-cut ---
defaults delete "$BUNDLE" 2> /dev/null || true
open -a "$APP"
cat > "$WORK/first-run.txt" <<'EOF'
wait-window 90
# Nothing is signed in on this Mac, so the page that says what to do comes first.
see "One thing before your first cut" 120
shot 01-get-ready
dump 01-get-ready
press "Continue without AI"
see "Start with your footage" 30
shot 02-footage-empty
dump 02-footage-empty
EOF
drive "$WORK/first-run.txt"

# Footage handed to the app from outside, as `open -a Roughcut FOLDER` (or Open With) does.
open -a "$APP" "$SHOOT"
cat > "$WORK/build.txt" <<'EOF'
see "Your footage" 30
see "*Walk and talk" 10
shot 03-footage
dump 03-footage
press "^Next"
see "What is this video?" 30
# A preset fills the brief, the length and the feel; the brief takes typing.
press "Venture OC"
see "*Pauses trimmed hard" 10
focus AXTextArea
key cmd-a
type "Open on the line about the pier. Keep the numbers."
see "*the line about the pier" 10
shot 04-brief
dump 04-brief
press "^Next"
see "Add anything else to the line?" 30
shot 05-add
press "^Next"
see "Ready to build" 30
shot 06-ready
dump 06-ready
press "Fine-tune…"
see "Fine-tune" 20
scroll-to "Keep um and uh"
shot 06b-fine-tune
dump 06b-fine-tune
press "Keep um and uh"
press "Done"
see "Ready to build" 20
press "Build rough cut"
pause 1
shot 07-running
see "Your rough cut is ready" 300
see "*Made without AI" 10
see "*tight, no AI" 10
shot 08-done
dump 08-done
press "Not quite right? Adjust…"
see "What should change?" 30
shot 09-adjust
dump 09-adjust
press "Loose"
press "Re-cut"
see "^Version 2" 300
shot 10-recut-done
dump 10-recut-done
# Back to the line: the footage and brief stay.
press "New Cut"
see "Ready to build" 20
shot 10b-new-cut
EOF
drive "$WORK/build.txt"

OUT="$(ls -d "$WORK"/*_roughcut | head -1)"
echo "Output folder: $OUT"
ls -la "$OUT" "$OUT/history" || true
test -f "$OUT/result.json"
test -f "$OUT/versions.json"
V1="$(ls "$OUT"/history/*/*v1.fcpxml | head -1)"
V2="$(ls "$OUT"/*v2.fcpxml | head -1)"
test -f "$V1"
test -f "$V2"
# What the window chose reached the engine: the preset's tight pacing and the kept um/uh in version 1's project, the quick
# change's loose pacing in version 2 with the kept um/uh still in it.
"$PYTHON" - "$OUT/result.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
cut = result["cut"]
print("version", result["version"], "cut", cut)
assert result["version"] == 2, result["version"]
assert cut["style"] == "loose", cut
assert cut["keep_fillers"] is True, cut
PY
grep -q "tight, no AI" "$V1"
"$PYTHON" - "$V1" "$V2" "$ROOT/tests/fixtures/FCPXMLv1_10.dtd" <<'PY'
import sys
from fractions import Fraction

from lxml import etree

*files, dtd_path = sys.argv[1:]
dtd = etree.DTD(dtd_path)
for name in files:
    doc = etree.parse(name)
    assert dtd.validate(doc), (name, dtd.error_log.filter_from_errors())
    sequence = doc.find(".//project/sequence")
    total = Fraction(sequence.get("duration").rstrip("s"))
    print(f"{name.rsplit('/', 1)[-1]}: valid against Apple's DTD, {float(total):.2f} s")
    assert float(total) > 1, name
PY

# --- 2 and 3. A run that is cancelled, a run that hits a usage limit: a stand-in for the command-line tool ---
quit_app
FAKE="$WORK/fake-roughcut"
# The stand-in: a build is what the file given says, anything else (doctor, presets) is the real tool.
make_fake() {
    {
        echo '#!/bin/bash'
        echo 'if [ "$1" = build ]; then'
        cat "$1"
        echo 'fi'
        echo "exec \"$ROOT/.venv/bin/roughcut\" \"\$@\""
    } > "$FAKE"
    chmod +x "$FAKE"
}
cat > "$WORK/slow-build.txt" <<'EOF'
echo '{"event": "stage", "stage": "scan", "message": "Finding media"}'
echo '{"event": "stage", "stage": "probe", "message": "Reading 3 files"}'
echo '{"event": "stage", "stage": "silence", "message": "Finding the pauses"}'
exec sleep 300
EOF
make_fake "$WORK/slow-build.txt"
defaults write "$BUNDLE" roughcutCLIPath "$FAKE"
open -a "$APP" "$SHOOT"
cat > "$WORK/cancel.txt" <<'EOF'
wait-window 90
see "Your footage" 60
press "^Next"
see "What is this video?" 30
press "^Next"
see "Add anything else to the line?" 30
press "^Next"
see "Ready to build" 30
press "Build rough cut"
see "Cancel" 30
pause 1
shot 11-run-to-cancel
dump 11-run-to-cancel
press "Cancel"
see "*cancelled" 30
shot 12-cancelled
dump 12-cancelled
EOF
drive "$WORK/cancel.txt"

quit_app
cat > "$WORK/limit-build.txt" <<'EOF'
echo '{"event": "stage", "stage": "scan", "message": "Finding media"}'
echo '{"event": "stage", "stage": "plan", "message": "Planning the edit"}'
echo '{"event": "error", "message": "You have hit your usage limit. It resets later today.", "kind": "usage_limit"}'
exit 1
EOF
make_fake "$WORK/limit-build.txt"
open -a "$APP" "$SHOOT"
cat > "$WORK/limit.txt" <<'EOF'
wait-window 90
see "Your footage" 60
press "^Next"
see "What is this video?" 30
press "^Next"
see "Add anything else to the line?" 30
press "^Next"
see "Ready to build" 30
press "Build rough cut"
see "*usage limit" 60
shot 13-usage-limit
dump 13-usage-limit
EOF
drive "$WORK/limit.txt"

# --- 4. The cuts made before, from the toolbar's Recent menu, in a fresh launch with the real command-line tool ---
quit_app
defaults delete "$BUNDLE" roughcutCLIPath 2> /dev/null || true
open -a "$APP"
cat > "$WORK/recent.txt" <<'EOF'
wait-window 90
press "Clock" 60
see "^Walk and talk" 10
shot 14-recent-menu
dump 14-recent-menu
press "^Walk and talk"
press "Open"
see "^Version 2" 30
shot 15-recent-opened
EOF
drive "$WORK/recent.txt"

# --- 5. Venture Studio through the app: a made-up Studio folder (the tests' own fake), the footage handed over as the app starts,
# a song chosen in the "From Venture Studio" sheet, the cut that uses it and the paper edit that credits it ---
quit_app
STUDIO_DIR="$("$PYTHON" .github/scripts/fake-studio.py "$WORK/studio-fake")"
cp -R "$SHOOT" "$WORK/Studio shoot"
open --env "ROUGHCUT_STUDIO_DIR=$STUDIO_DIR" -a "$APP" "$WORK/Studio shoot"
cat > "$WORK/studio.txt" <<'EOF'
wait-window 90
see "Your footage" 60
press "^Next"
see "What is this video?" 30
press "^Next"
see "Add anything else to the line?" 30
press "Add"
see "Add a song" 20
press "From Venture Studio…" 60
see "Music from Venture Studio" 30
see "*Sunrise" 60
shot 16-studio-sheet
dump 16-studio-sheet
click "*Sunrise"
see "1 selected" 10
press "Add to Music bed"
see "Sunrise.m4a" 20
shot 17-music-added
press "^Next"
see "Ready to build" 30
shot 18-ready-with-music
press "Build rough cut"
see "Your rough cut is ready" 300
shot 19-done-with-studio-music
EOF
drive "$WORK/studio.txt"
SOUT="$WORK/Studio shoot_roughcut"
SV1="$(ls "$SOUT"/*v1.fcpxml | head -1)"
"$PYTHON" - "$SV1" "$ROOT/tests/fixtures/FCPXMLv1_10.dtd" <<'PY'
import sys
from urllib.parse import unquote

from lxml import etree

name, dtd_path = sys.argv[1:]
doc = etree.parse(name)
assert etree.DTD(dtd_path).validate(doc), etree.DTD(dtd_path).error_log.filter_from_errors()
sources = [unquote(a.get("src") or "") for a in doc.iter("asset", "media-rep")]
print("sources:", sources)
assert any(s.endswith("Studio Library/Music/Sunrise.m4a") for s in sources), "the Studio song isn't in the cut"
print(f"{name.rsplit('/', 1)[-1]}: valid against Apple's DTD, with Studio's song as the music bed")
PY
grep -n "Venture Studio" "$SOUT/edit_report.md" | head
grep -q "^## Venture Studio" "$SOUT/edit_report.md"
grep -q "Sunrise" "$SOUT/edit_report.md"

quit_app
defaults delete "$BUNDLE" 2> /dev/null || true
echo "The packaged app, driven end to end: passed"
