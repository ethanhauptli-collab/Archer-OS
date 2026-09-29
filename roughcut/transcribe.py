"""Speech-to-text with word timestamps.

Backends:
  mlx             mlx-whisper on Apple Silicon (fastest on a Mac)
  faster-whisper  CTranslate2 Whisper (CPU/CUDA, cross-platform)
  none            no transcription; cuts fall back to the silence map

A sidecar transcript next to a clip (IMG_1234.srt / .vtt / .json) always wins
over running a model, so transcripts from Descript, Premiere, FCP captions,
etc. can be dropped in.
"""

from __future__ import annotations

import json
import platform
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "hmm", "mm", "mmm"}

# Priming Whisper with disfluent text makes it transcribe "um"/"uh" instead of
# silently cleaning them up, which is what lets us cut them.
VERBATIM_PROMPT = "Um, so, uh, we went to the, uh, the park. Hmm, okay. Like, you know, it was good."

_HALLUCINATIONS = (
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "subtitles by",
    "transcribed by",
    "amara.org",
)

DEFAULT_MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster-whisper": "large-v3-turbo",
}


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0

    @property
    def bare(self) -> str:
        return re.sub(r"[^\w'-]", "", self.text).lower()

    @property
    def is_filler(self) -> bool:
        return self.bare in FILLERS


@dataclass
class Transcript:
    words: list[Word] = field(default_factory=list)
    language: str = ""
    source: str = ""  # backend name or sidecar path

    def to_json(self) -> dict:
        return {"language": self.language, "source": self.source, "words": [asdict(w) for w in self.words]}

    @classmethod
    def from_json(cls, d: dict) -> "Transcript":
        return cls(
            words=[Word(**w) for w in d.get("words", [])],
            language=d.get("language", ""),
            source=d.get("source", ""),
        )

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)


def resolve_backend(name: str) -> str:
    if name != "auto":
        return name
    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # noqa: F401

            return "mlx"
        except ImportError:
            pass
    try:
        import faster_whisper  # noqa: F401

        return "faster-whisper"
    except ImportError:
        pass
    raise TranscriptionError(
        "No transcription backend installed. On an Apple Silicon Mac: "
        "`pip install -e '.[mac]'` (mlx-whisper). Elsewhere: `pip install -e '.[whisper]'`. "
        "Or run with --transcriber none to cut on silence only."
    )


class Transcriber:
    def __init__(self, backend: str, model: str | None = None, language: str | None = None, verbatim: bool = True):
        self.backend = backend
        self.model = model or DEFAULT_MODELS.get(backend, "")
        self.language = language
        self.verbatim = verbatim
        self._fw_model = None

    @property
    def cache_key(self) -> str:
        return f"{self.backend}-{self.model}-{self.language or 'auto'}-{'v' if self.verbatim else 'c'}".replace("/", "_")

    def transcribe(self, path: Path) -> Transcript:
        if self.backend == "mlx":
            return self._mlx(path)
        if self.backend == "faster-whisper":
            return self._faster_whisper(path)
        raise TranscriptionError(f"unknown transcriber {self.backend!r}")

    def _mlx(self, path: Path) -> Transcript:
        try:
            import mlx_whisper
        except ImportError as e:
            raise TranscriptionError("mlx-whisper is not installed: pip install -e '.[mac]'") from e
        kwargs = dict(
            path_or_hf_repo=self.model,
            word_timestamps=True,
            condition_on_previous_text=False,
            # Skip long silent stretches where Whisper tends to invent text.
            hallucination_silence_threshold=2.0,
        )
        if self.language:
            kwargs["language"] = self.language
        if self.verbatim:
            kwargs["initial_prompt"] = VERBATIM_PROMPT
        result = mlx_whisper.transcribe(str(path), **kwargs)
        return _from_whisper_dict(result, source=f"mlx:{self.model}")

    def _faster_whisper(self, path: Path) -> Transcript:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise TranscriptionError("faster-whisper is not installed: pip install -e '.[whisper]'") from e
        if self._fw_model is None:
            self._fw_model = WhisperModel(self.model, device="auto", compute_type="auto")
        segments, info = self._fw_model.transcribe(
            str(path),
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
            language=self.language,
            initial_prompt=VERBATIM_PROMPT if self.verbatim else None,
        )
        words: list[Word] = []
        for seg in segments:
            if _looks_hallucinated(seg.text, getattr(seg, "no_speech_prob", 0.0), getattr(seg, "avg_logprob", 0.0)):
                continue
            for w in seg.words or []:
                if w.end > w.start:
                    words.append(Word(float(w.start), float(w.end), w.word.strip(), float(w.probability)))
        return Transcript(words=words, language=getattr(info, "language", "") or "", source=f"faster-whisper:{self.model}")


def _looks_hallucinated(text: str, no_speech_prob: float, avg_logprob: float) -> bool:
    lowered = text.strip().lower()
    if no_speech_prob > 0.6 and avg_logprob < -1.0:
        return True
    if any(h in lowered for h in _HALLUCINATIONS) and (no_speech_prob > 0.3 or avg_logprob < -0.7):
        return True
    return False


def _from_whisper_dict(result: dict, source: str) -> Transcript:
    words: list[Word] = []
    for seg in result.get("segments", []):
        if _looks_hallucinated(seg.get("text", ""), seg.get("no_speech_prob", 0.0), seg.get("avg_logprob", 0.0)):
            continue
        seg_words = seg.get("words")
        if seg_words:
            for w in seg_words:
                start, end = float(w.get("start", 0)), float(w.get("end", 0))
                if end > start:
                    words.append(Word(start, end, str(w.get("word", "")).strip(), float(w.get("probability", 1.0))))
        elif seg.get("text"):
            words.extend(_spread_words(seg["text"], float(seg["start"]), float(seg["end"])))
    return Transcript(words=words, language=result.get("language", "") or "", source=source)


# ---------------------------------------------------------------- sidecars


def find_sidecar(media: Path, extra_dirs: list[Path] | None = None) -> Path | None:
    dirs = [media.parent] + list(extra_dirs or [])
    for d in dirs:
        for ext in (".json", ".srt", ".vtt"):
            for name in (media.stem + ext, media.name + ext):
                candidate = d / name
                if candidate.is_file():
                    return candidate
    return None


def load_sidecar(path: Path) -> Transcript:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    ext = path.suffix.lower()
    if ext == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "words" in data and "segments" not in data:
            words = [
                Word(float(w["start"]), float(w["end"]), str(w.get("text", w.get("word", ""))).strip(), float(w.get("prob", w.get("probability", 1.0))))
                for w in data["words"]
            ]
            return Transcript(words=words, language=data.get("language", ""), source=str(path))
        if isinstance(data, dict) and "segments" in data:
            t = _from_whisper_dict(data, source=str(path))
            return t
        raise TranscriptionError(f"{path.name}: unrecognized JSON transcript shape")
    cues = _parse_cues(text)
    words: list[Word] = []
    for start, end, cue_text in cues:
        words.extend(_spread_words(cue_text, start, end))
    return Transcript(words=words, source=str(path))


_CUE_TIME = re.compile(
    r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _cue_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    hours = int(h[:-1]) if h else 0
    return hours * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _parse_cues(text: str) -> list[tuple[float, float, str]]:
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            m = _CUE_TIME.search(line)
            if m:
                g = m.groups()
                start = _cue_seconds(g[0], g[1], g[2], g[3])
                end = _cue_seconds(g[4], g[5], g[6], g[7])
                body = " ".join(lines[i + 1 :])
                body = re.sub(r"<[^>]+>", "", body).strip()
                if body and end > start:
                    cues.append((start, end, body))
                break
    return cues


def _spread_words(text: str, start: float, end: float) -> list[Word]:
    """Approximate word timings inside a cue, proportional to word length."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(t) + 1 for t in tokens]
    total = sum(weights)
    span = end - start
    words, cursor = [], start
    for tok, wgt in zip(tokens, weights):
        dur = span * wgt / total
        words.append(Word(round(cursor, 3), round(cursor + dur, 3), tok, 0.5))
        cursor += dur
    return words
