"""A made-up Venture Studio data folder for the end-to-end run: two songs in a made-up library, and one catalog snapshot in the
layout Roughcut reads. It is built with the tests' own helper (tests/test_studio.py), so the catalog's shape is written once, in
the tests, and nothing here is Studio's.

    python .github/scripts/fake-studio.py FOLDER   # prints the data folder to hand the app as ROUGHCUT_STUDIO_DIR
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from test_studio import NOW, FakeStudio, _asset  # noqa: E402

root = Path(sys.argv[1]).resolve()
library = root / "Studio Library"
(library / "Music").mkdir(parents=True)
for name, hertz in (("Sunrise.m4a", 330), ("Calm Piano.m4a", 262)):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hertz}:duration=30:sample_rate=48000",
         "-c:a", "aac", str(library / "Music" / name)],
        check=True,
    )
fake = FakeStudio(root)
fake.libraries = [(str(library), "Studio Library")]
fake.snapshot(
    "1700000000-42-1",
    NOW - 3600,
    assets=[
        _asset(1, "Music/Sunrise.m4a", kind="audio", duration=30.0, favorite=1, tags="upbeat, warm", provider="Vendor A", license="Standard"),
        _asset(2, "Music/Calm Piano.m4a", kind="audio", duration=30.0, tags="calm", provider="Vendor A", license="Standard"),
    ],
)
print(fake.dir)
