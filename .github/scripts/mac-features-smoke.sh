#!/bin/bash
# What the Python tests can only fake: the macOS side of the Venture Studio, sound-effect and reference-image
# features, on a real Mac with the footage mac-engine-smoke.sh made (no model needed).
#   - a sound-effects folder becomes accents on an effects.SFX role in a DTD-valid FCPXML
#   - Roughcut reads a (fake) Venture Studio snapshot from a folder with spaces without touching Studio's files
#   - a HEIC reference picture (macOS sips) is stored as a JPEG
set -euo pipefail
cd smoke
WHISPER=(${SMOKE_TRANSCRIBE:---whisper-model mlx-community/whisper-tiny})

# --- sound effects ---------------------------------------------------------------------------------------
mkdir -p sfx
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=500:duration=0.8:sample_rate=48000" -af "afade=t=in:d=0.5,afade=t=out:st=0.5:d=0.3,volume=0.6" sfx/whoosh_soft_01.wav
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=900:duration=0.15:sample_rate=48000" -af "volume=0.8" sfx/pop_ui_01.wav
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=80:duration=0.6:sample_rate=48000" -af "afade=t=out:st=0.1:d=0.5,volume=0.9" sfx/hit_impact_01.wav
python -m roughcut build Footage -o Footage_sfx --provider none "${WHISPER[@]}" --no-menu --sfx sfx
python - <<'PY'
import json
from pathlib import Path
from lxml import etree

out = Path("Footage_sfx")
dtd = etree.DTD(str(Path("..") / "tests" / "fixtures" / "FCPXMLv1_10.dtd"))
fcpxml = next(out.glob("Footage v*.fcpxml"))
assert dtd.validate(etree.parse(str(fcpxml))), dtd.error_log.filter_from_errors()
pool = json.loads((out / "plan.json").read_text())["settings"]["sfx_pool"]["sounds"]
kinds = sorted(s["kind"] for s in pool)
assert kinds == ["hit", "pop", "whoosh"], f"the sound effects weren't classified from their names: {kinds}"
for s in pool:  # even a 150 ms pop gets a level (it is never left at its own)
    assert isinstance(s["lufs"], (int, float)) and isinstance(s["peak"], (int, float)), f"{s['media']['name']} wasn't measured: {s}"
assert "Sound effects" in (out / "edit_report.md").read_text(), "the paper edit has no Sound effects section"
accents = fcpxml.read_text().count("effects.SFX")  # this footage has one section, so there may be none to place
print(f"sound effects ok: 3 read, classified and measured on this Mac; {accents} accent clip(s) placed")
PY

# --- Venture Studio, read only ---------------------------------------------------------------------------
STUDIO="$PWD/Studio Data (test)"
rm -rf "$STUDIO"
python - "$STUDIO" <<'PY'
import hashlib, json, sqlite3, sys
from pathlib import Path
from roughcut import studio

folder = Path(sys.argv[1])
(folder / "backups").mkdir(parents=True)
db = folder / "backups" / "1700000000-1-1.sqlite"
con = sqlite3.connect(db)
for table, cols in studio.REQUIRED_COLUMNS.items():
    con.execute(f"CREATE TABLE {table} ({', '.join(sorted(cols))})")
con.execute("INSERT INTO libraries (id, path, name) VALUES (1, '/Volumes/Media', 'Studio Library')")
con.execute(
    "INSERT INTO assets (id, library_id, relative_path, name, display_name, extension, kind, category, provider, license, tags, notes, "
    "duration, favorite, missing, browse_visible, package_id, template_resource) VALUES "
    "(1, 1, 'Sounds/whoosh_soft_01.wav', 'whoosh_soft_01.wav', 'Whoosh', 'wav', 'audio', 'audio', 'Acme', 'Standard', '', '', 0.8, 0, 0, 1, NULL, 0)"
)
con.execute(f"PRAGMA user_version = {studio.SCHEMA_VERSION}")
con.commit()
con.close()
data = db.read_bytes()
manifest = {
    "id": db.stem, "name": "Catalog snapshot", "created_at": 1700000000, "schema_version": studio.SCHEMA_VERSION,
    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
}
(folder / "backups" / "1700000000-1-1.json").write_text(json.dumps(manifest))
PY
before=$(find "$STUDIO" -type f -exec shasum {} + | sort | shasum)
mtimes=$(find "$STUDIO" -print0 | xargs -0 stat -f '%N %m' | sort)
ROUGHCUT_STUDIO_DIR="$STUDIO" python -m roughcut studio status --offline --json > studio_status.json
python - <<'PY'
import json
r = json.load(open("studio_status.json"))
assert r.get("available") is True, r
assert r.get("items") == 1, r
print("studio status ok:", r.get("summary"))
PY
after=$(find "$STUDIO" -type f -exec shasum {} + | sort | shasum)
[ "$before" = "$after" ] || { echo "Venture Studio's folder changed while Roughcut read it"; exit 1; }
[ "$mtimes" = "$(find "$STUDIO" -print0 | xargs -0 stat -f '%N %m' | sort)" ] || { echo "Venture Studio's folder mtimes changed"; exit 1; }

# --- reference pictures, HEIC through sips ---------------------------------------------------------------
ffmpeg -loglevel error -y -f lavfi -i "color=c=teal:size=800x600" -frames:v 1 ref.jpg
sips -s format heic ref.jpg --out ref.heic > /dev/null
python - <<'PY'
from pathlib import Path
from roughcut import references

warnings = []
out = Path("refs_out")
names = references.ingest([Path("ref.jpg"), Path("ref.heic")], out, warnings)
assert len(names) == 2 and not warnings, (names, warnings)
for n in names:
    assert (out / n).read_bytes()[:3] == b"\xff\xd8\xff", f"{n} isn't a JPEG"
print("reference pictures ok (JPEG and HEIC):", names)
PY
echo "features smoke test passed"
