#!/bin/bash
# Real speech (macOS `say`) with long pauses, plus a B-roll clip and a still:
# transcribe with mlx-whisper, cut the dead air, and check the FCPXML.
set -euo pipefail
mkdir -p smoke/Footage
cd smoke/Footage
say -o part1.aiff "Welcome to Orange County. This weekend the food festival takes over the park."
say -o part2.aiff "Over forty chefs are cooking, and tickets start at twenty dollars."
ffmpeg -loglevel error -y -i part1.aiff -f lavfi -t 4 -i anullsrc=r=22050:cl=mono -i part2.aiff \
    -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1,aresample=48000[a]" -map "[a]" speech.wav
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 speech.wav)
ffmpeg -loglevel error -y -f lavfi -i "testsrc2=size=1920x1080:rate=24" -i speech.wav -t "$DUR" \
    -map 0:v -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest talk.mp4
ffmpeg -loglevel error -y -f lavfi -i "mandelbrot=size=1920x1080:rate=24" -t 6 -c:v libx264 -pix_fmt yuv420p broll_park.mp4
ffmpeg -loglevel error -y -f lavfi -i "color=c=orange:size=1600x1200" -frames:v 1 still_festival.jpg
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=220:duration=40:sample_rate=48000" -c:a aac ../bed.m4a
rm part1.aiff part2.aiff speech.wav
cd ..
echo "$DUR" > source_seconds.txt
python -m roughcut doctor --offline || true
python -m roughcut build Footage --provider none --whisper-model mlx-community/whisper-tiny --no-menu --music bed.m4a --preset venture-oc
OUT=Footage_roughcut
python -m roughcut render "$OUT" --style loose --format vertical
ls -la "$OUT" "$OUT/history"
python - "$OUT" <<'PY'
import sys
from fractions import Fraction
from pathlib import Path
from lxml import etree

out = Path(sys.argv[1])
dtd = etree.DTD(str(Path("..") / "tests" / "fixtures" / "FCPXMLv1_10.dtd"))
for f in [out / "Footage v2.fcpxml", *out.glob("history/*/Footage v1.fcpxml")]:
    assert dtd.validate(etree.parse(str(f))), (f, dtd.error_log.filter_from_errors())
fcpxml = next(out.glob("history/*/Footage v1.fcpxml"))
doc = etree.parse(str(fcpxml))
assert "music" in etree.tostring(doc).decode(), "the --music bed isn't in the cut"
vertical = etree.parse(str(out / "Footage v2.fcpxml")).find(".//format")
assert int(vertical.get("height")) > int(vertical.get("width")), "render --format vertical didn't make a vertical sequence"
analysis = (out / "analysis.json").read_text()
assert "festival" in analysis.lower(), "mlx-whisper transcript is missing the spoken words"
projects = doc.findall(".//project")
assert projects, "no projects in the FCPXML"
seq = projects[0].find("sequence")
total = Fraction(seq.get("duration").rstrip("s"))
print(f"Rough cut: {float(total):.2f} s from {fcpxml.name}")
source = float(Path("source_seconds.txt").read_text())
assert float(total) < source - 2.5, f"the 4 s pause wasn't cut: {float(total):.2f} s from {source:.2f} s of speech"
print("engine smoke test passed")
PY
