"""§4.1 Ingest & transcribe — word-level timestamps or nothing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import media


class TranscriptionError(Exception):
    pass


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    words: list[Word]
    language: str
    duration: float
    source: str  # "provided" | "faster-whisper"

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text for w in self.words_between(start, end))

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.start >= start - 1e-6 and w.end <= end + 1e-6]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def load_words(raw: dict) -> list[Word]:
    """Accept our own shape or a whisper-style {"segments": [{"words": [...]}]} dump."""
    if raw.get("words"):
        items = raw["words"]
    else:
        items = [w for seg in raw.get("segments", []) for w in seg.get("words", [])]
    if not items:
        raise TranscriptionError(
            "transcript has no word-level timestamps — sentence-level is not precise enough for cut points"
        )
    words = []
    for item in items:
        text = (item.get("text") or item.get("word") or "").strip()
        if not text:
            continue
        words.append(Word(text=text, start=float(item["start"]), end=float(item["end"])))
    return words


def transcribe(video: str, transcript_path: str | None = None, language: str | None = None) -> Transcript:
    if transcript_path:
        raw = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
        words = load_words(raw)
        return Transcript(
            words=words,
            language=language or raw.get("language") or "en",
            duration=float(raw.get("duration") or (words[-1].end if words else 0.0)),
            source="provided",
        )

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise TranscriptionError(
            "no transcript supplied and faster-whisper is not installed "
            "(pip install 'shorts-autoclipper[whisper]', or pass source_transcript)"
        ) from exc

    size = os.getenv("SHORTS_WHISPER_MODEL", "base")

    def run(device: str, compute: str):
        model = WhisperModel(size, device=device, compute_type=compute)
        segments, info = model.transcribe(video, word_timestamps=True, language=language)
        # `segments` is lazy — the transcription (and any CUDA failure) happens right here.
        words = [
            Word(text=w.word.strip(), start=w.start, end=w.end)
            for seg in segments
            for w in (seg.words or [])
            if w.word.strip()
        ]
        return words, info

    device = os.getenv("SHORTS_WHISPER_DEVICE", "auto")
    compute = os.getenv("SHORTS_WHISPER_COMPUTE", "int8")
    try:
        words, info = run(device, compute)
        used = device
    except (RuntimeError, ValueError) as exc:
        # A machine can advertise a GPU and still lack the CUDA runtime ctranslate2 wants.
        if device == "cpu":
            raise TranscriptionError("transcription failed: %s" % exc) from exc
        words, info = run("cpu", "int8")
        used = "cpu"

    if not words:
        raise TranscriptionError("transcription returned no words")
    return Transcript(
        words=words,
        language=language or info.language,
        duration=float(info.duration or media.probe(video).duration),
        source="faster-whisper (%s)" % used,
    )
