"""Synthetic footage for end-to-end tests, generated with ffmpeg.

The "talking" clip has tone bursts where speech would be and a sidecar
transcript whose words line up with those bursts, so the whole pipeline runs
without a Whisper model or an API key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# (start, end, sentence) for the fake talking-head clip.
SPEECH = [
    (1.0, 3.0, "So today we're heading up the mountain."),
    (3.3, 5.0, "Um, wait, let me start over."),
    (6.5, 8.0, "Today we're heading up the mountain."),
    (8.2, 10.0, "It's going to be a long day."),
    (12.0, 15.0, "The kids have been asking about this trip for weeks."),
    (15.4, 18.0, "And honestly, uh, I needed it too."),
]
TALK_SECONDS = 20


def spread_words(start: float, end: float, sentence: str) -> list[dict]:
    tokens = sentence.split()
    step = (end - start) / len(tokens)
    return [
        {"start": round(start + i * step, 3), "end": round(start + (i + 1) * step - 0.02, 3), "text": tok, "prob": 0.95}
        for i, tok in enumerate(tokens)
    ]


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def footage(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("footage")
    talk = root / "talk.mov"
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SPEECH)
    ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=1920x1080:rate=30000/1001:duration={TALK_SECONDS}",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*300*t)*({gates})':s=48000:d={TALK_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-timecode", "01:00:00:00", "-metadata", "creation_time=2026-08-12T09:00:00Z",
        str(talk),
    )
    words = [w for s, e, text in SPEECH for w in spread_words(s, e, text)]
    (root / "talk.json").write_text(json.dumps({"words": words}))

    broll = root / "broll.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30000/1001:duration=8",
        "-f", "lavfi", "-i", "anoisesrc=a=0.08:d=8:r=48000",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        "-metadata", "creation_time=2026-08-12T09:05:00Z",
        str(broll),
    )
    (root / "broll.json").write_text(json.dumps({"words": []}))

    film = root / "broll_2398.mov"
    ffmpeg(
        "-f", "lavfi", "-i", "smptebars=size=1920x1080:rate=24000/1001:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-metadata", "creation_time=2026-08-12T09:10:00Z",
        str(film),
    )

    music_dir = root / "music"
    music_dir.mkdir()
    ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=220:duration=30:sample_rate=48000",
        "-c:a", "aac", "-metadata", "creation_time=2026-08-12T08:00:00Z",
        str(music_dir / "music.m4a"),
    )
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1600x900:duration=1", "-frames:v", "1", str(root / "photo.png"))
    # Things the scanner must ignore.
    (root / "._talk.mov").write_bytes(b"resource fork junk")
    (root / "notes.txt").write_text("not media")
    return root


CANNED_PLAN = {
    "title": "Mountain Day",
    "logline": "A family trip up the mountain.",
    "sections": [
        {"name": "Cold open", "purpose": "hook", "items": ["V01.S005"], "broll": []},
        {
            "name": "Heading out",
            "purpose": "setup",
            "items": ["V01.S003", "V01.S004", "I01@0-3", "V09.S001", "V01.S005"],
            "broll": [
                {"clip": "V02", "over": ["V01.S003", "V01.S004"], "source_in": -1, "reason": "cover the jump cut"},
                {"clip": "V01", "over": ["V01.S999"], "source_in": 0, "reason": "bad reference"},
            ],
        },
        {
            "name": "Wrap",
            "purpose": "close",
            "items": ["V01.S006", "V03@1-4"],
            "broll": [{"clip": "I01", "over": ["V01.S006"], "source_in": 0, "reason": "photo"}],
        },
    ],
    "alternates": [{"chosen": "V01.S003", "others": ["V01.S001"], "note": "second take is cleaner"}],
    "flags": [
        {"ref": "V01.S002", "note": "cut the restart"},
        {"ref": "V01.S006", "note": "check the audio here"},
    ],
    "music": [{"clip": "A01", "first_section": 0, "last_section": 2, "source_in": 2}],
    "cut_notes": "Dropped the first take and the restart.",
}


class FakeProvider:
    """Stands in for a model: returns canned JSON and records what it was sent."""

    name = "fake"
    model = "fake-model"
    supports_vision = True

    def __init__(self, plan: dict | None = None):
        from roughcut.providers import UsageLog

        self.plan = plan or CANNED_PLAN
        self.calls: list[dict] = []
        self.usage = UsageLog()

    def verify(self):
        return self.model

    def complete_json(self, system, parts, schema, *, schema_name, purpose, effort=None, max_tokens=64000):
        self.calls.append({"system": system, "parts": parts, "schema_name": schema_name, "purpose": purpose})
        if schema_name == "clip_log":
            ids = [p.text.split()[1] for p in parts if hasattr(p, "text") and p.text.startswith("Clip ")]
            return {
                "clips": [
                    {"id": i, "description": f"Test pattern {i}", "tags": ["test", "pattern"], "shot": "wide", "quality": "good"}
                    for i in ids
                ]
            }
        return self.plan


# ------------------------------------------------------------ in-memory clips

NTSC = __import__("fractions").Fraction(1001, 30000)


def make_clip(clip_id: str, *, kind: str = "video", seconds: float = 30.0, sentences=None, start=None, role=None, silences=None):
    """Build an analysed Clip without touching ffmpeg."""
    from fractions import Fraction

    from roughcut.analyze import Clip
    from roughcut.media import MediaInfo
    from roughcut.segments import build_segments
    from roughcut.transcribe import Word

    fd = NTSC if kind == "video" else None
    if kind == "video":
        duration = round(Fraction(seconds) / NTSC) * NTSC
    elif kind == "audio":
        duration = Fraction(round(seconds * 48000), 48000)
    else:
        duration = Fraction(0)
    media = MediaInfo(
        path=f"/footage/{clip_id}.{'mov' if kind == 'video' else 'wav' if kind == 'audio' else 'jpg'}",
        name=f"{clip_id}.{'mov' if kind == 'video' else 'wav' if kind == 'audio' else 'jpg'}",
        kind=kind,
        duration=duration,
        start=start if start is not None else Fraction(0),
        has_video=kind in ("video", "image"),
        has_audio=kind in ("video", "audio"),
        width=1920 if kind != "audio" else 0,
        height=1080 if kind != "audio" else 0,
        frame_duration=fd,
        rate="29.97" if fd else "",
        audio_channels=2 if kind != "image" else 0,
        audio_rate=48000 if kind != "image" else 0,
        fingerprint=clip_id.lower(),
    )
    clip = Clip(id=clip_id, media=media, silences=silences or [])
    if sentences:
        ws = []
        for s, e, text in sentences:
            ws += [Word(**w) for w in spread_words(s, e, text)]
        clip.segments = build_segments(clip_id, ws)
        clip.transcript_source = "test"
    clip.role = role or ("aroll" if sentences else {"video": "broll", "audio": "music", "image": "still"}[kind])
    return clip
