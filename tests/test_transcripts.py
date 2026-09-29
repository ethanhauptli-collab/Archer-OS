import json

from roughcut.ffmpeg_ops import parse_silencedetect, sound_regions
from roughcut.segments import build_segments, drop_words_in_silence, segments_from_regions
from roughcut.transcribe import Word, _from_whisper_dict, find_sidecar, load_sidecar


def words(*spec):
    return [Word(s, e, t) for s, e, t in spec]


def test_segments_split_on_sentences_and_gaps():
    ws = words(
        (0.0, 0.3, "Hello"), (0.35, 0.6, "there."),
        (0.8, 1.0, "This"), (1.05, 1.3, "is"),
        (3.0, 3.2, "after"), (3.25, 3.5, "a"), (3.55, 3.9, "pause"),
    )
    segs = build_segments("V01", ws)
    assert [s.id for s in segs] == ["V01.S001", "V01.S002", "V01.S003"]
    assert segs[0].text == "Hello there."
    assert segs[1].text == "This is"
    assert segs[2].start == 3.0 and segs[2].end == 3.9


def test_filler_detection():
    assert Word(0, 1, "Um,").is_filler
    assert Word(0, 1, "uh").is_filler
    assert not Word(0, 1, "umbrella").is_filler
    assert not Word(0, 1, "like").is_filler


def test_hallucinated_words_in_silence_are_dropped():
    ws = words((0.0, 0.5, "real"), (2.1, 2.6, "Thanks"), (2.7, 2.9, "watching"))
    kept = drop_words_in_silence(ws, [(2.0, 5.0)])
    assert [w.text for w in kept] == ["real"]


def test_parse_silencedetect():
    log = """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 1.02 | silence_duration: 1.02
[silencedetect @ 0x1] silence_start: 3.5
[silencedetect @ 0x1] silence_end: 4.25 | silence_duration: 0.75
[silencedetect @ 0x1] silence_start: 9.1
"""
    assert parse_silencedetect(log, 10.0) == [(0.0, 1.02), (3.5, 4.25), (9.1, 10.0)]
    assert sound_regions([(0.0, 1.02), (3.5, 4.25), (9.1, 10.0)], 10.0) == [(1.02, 3.5), (4.25, 9.1)]


def test_segments_from_regions_chunks_long_regions():
    segs = segments_from_regions("V02", [(0.0, 30.0)], max_len=12.0)
    assert [(s.start, s.end) for s in segs] == [(0.0, 12.0), (12.0, 24.0), (24.0, 30.0)]


def test_srt_sidecar(tmp_path):
    media = tmp_path / "clip.mov"
    media.write_bytes(b"")
    (tmp_path / "clip.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHello world.\n\n2\n00:00:04,500 --> 00:00:06,000\n<i>Second</i> line here.\n"
    )
    side = find_sidecar(media)
    assert side == tmp_path / "clip.srt"
    t = load_sidecar(side)
    assert [w.text for w in t.words] == ["Hello", "world.", "Second", "line", "here."]
    assert t.words[0].start == 1.0 and t.words[1].end == 3.0
    assert t.words[2].start == 4.5


def test_vtt_sidecar(tmp_path):
    (tmp_path / "clip.vtt").write_text("WEBVTT\n\n00:01.000 --> 00:02.500\nShort cue.\n\n01:00:00.000 --> 01:00:01.000\nHour mark.\n")
    t = load_sidecar(tmp_path / "clip.vtt")
    assert t.words[0].start == 1.0
    assert t.words[-1].end == 3601.0


def test_whisper_json_sidecar(tmp_path):
    data = {
        "language": "en",
        "segments": [
            {"start": 0, "end": 2, "text": " Hi there.", "no_speech_prob": 0.01, "avg_logprob": -0.2,
             "words": [{"word": " Hi", "start": 0.1, "end": 0.4, "probability": 0.9}, {"word": " there.", "start": 0.5, "end": 0.9, "probability": 0.8}]},
            {"start": 5, "end": 7, "text": " Thanks for watching!", "no_speech_prob": 0.7, "avg_logprob": -1.3,
             "words": [{"word": " Thanks", "start": 5.0, "end": 5.5, "probability": 0.2}]},
        ],
    }
    (tmp_path / "clip.json").write_text(json.dumps(data))
    t = load_sidecar(tmp_path / "clip.json")
    assert [w.text for w in t.words] == ["Hi", "there."]
    assert t.language == "en"


def test_whisper_dict_without_word_timestamps_spreads_words():
    t = _from_whisper_dict({"segments": [{"start": 0.0, "end": 2.0, "text": "one two"}]}, source="x")
    assert [w.text for w in t.words] == ["one", "two"]
    assert t.words[0].start == 0.0 and abs(t.words[-1].end - 2.0) < 1e-6


def test_mlx_backend_glue(monkeypatch, tmp_path):
    """The Mac path, with mlx_whisper stubbed (it only runs on Apple Silicon)."""
    import sys
    import types

    from roughcut.transcribe import VERBATIM_PROMPT, Transcriber

    seen = {}

    def transcribe(path, **kwargs):
        seen.update(kwargs, path=path)
        return {
            "language": "en",
            "segments": [
                {"start": 0, "end": 1, "text": " Hi, um, there.", "no_speech_prob": 0.01, "avg_logprob": -0.1,
                 "words": [{"word": " Hi,", "start": 0.1, "end": 0.3, "probability": 0.9},
                           {"word": " um,", "start": 0.35, "end": 0.6, "probability": 0.7},
                           {"word": " there.", "start": 0.7, "end": 0.9, "probability": 0.9}]},
            ],
        }

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    t = Transcriber("mlx", language="en").transcribe(tmp_path / "a.wav")
    assert seen["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    # Verbatim mode conditions on previous text so the um-keeping style lasts past 30s.
    assert seen["word_timestamps"] is True and seen["condition_on_previous_text"] is True
    assert seen["initial_prompt"] == VERBATIM_PROMPT and seen["language"] == "en"
    assert [w.text for w in t.words] == ["Hi,", "um,", "there."]
    assert t.words[1].is_filler


def test_faster_whisper_backend_glue(monkeypatch, tmp_path):
    import sys
    import types

    from roughcut.transcribe import Transcriber

    seen = {}

    class FakeModel:
        def __init__(self, name, **kwargs):
            seen["model"] = name

        def transcribe(self, path, **kwargs):
            seen.update(kwargs)
            W = types.SimpleNamespace
            segs = [
                W(text=" Real words.", no_speech_prob=0.01, avg_logprob=-0.2,
                  words=[W(start=0.0, end=0.4, word=" Real", probability=0.9), W(start=0.5, end=0.9, word=" words.", probability=0.9)]),
                W(text=" Thanks for watching!", no_speech_prob=0.8, avg_logprob=-1.5,
                  words=[W(start=5.0, end=6.0, word=" Thanks", probability=0.1)]),
            ]
            return iter(segs), W(language="en")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    t = Transcriber("faster-whisper").transcribe(tmp_path / "a.wav")
    assert seen["model"] == "large-v3-turbo" and seen["vad_filter"] is True and seen["word_timestamps"] is True
    assert seen["hotwords"] == seen["initial_prompt"]  # re-sent every window
    assert [w.text for w in t.words] == ["Real", "words."]
    assert t.language == "en"
