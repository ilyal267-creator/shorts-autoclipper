"""Voiceover: a narration script read by an ElevenLabs voice, with word timings for captions.

Captions have to follow what is *heard*, and once narration goes over a clip that is the
voice, not the original speech. ElevenLabs' with-timestamps endpoint returns per-character
timings; when a model does not supply them, the rendered narration is transcribed locally
instead — whisper on a clean studio voice is close to exact.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from .transcribe import Word

API = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_v3"  # the most human-sounding ElevenLabs model at the time of writing
WORDS_PER_SECOND = 2.4  # ~145 wpm: natural narration pace, with room to breathe


class VoiceError(Exception):
    pass


def _key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set — add it to .env")
    return key


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise VoiceError("requests is not installed — pip install requests") from exc
    return requests


def word_budget(seconds: float) -> int:
    """How many words of narration fit a clip without rushing or running over."""
    return max(4, int(seconds * WORDS_PER_SECOND))


def words_from_alignment(alignment: dict) -> list[Word]:
    """Per-character timings -> per-word timings, split where the characters are whitespace."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words: list[Word] = []
    text, first, last = "", None, None
    for char, start, end in zip(chars, starts, ends):
        if char.isspace():
            if text:
                words.append(Word(text=text, start=first, end=last))
            text, first, last = "", None, None
            continue
        if first is None:
            first = float(start)
        text += char
        last = float(end)
    if text:
        words.append(Word(text=text, start=first, end=last))
    return words


def list_voices() -> list[dict]:
    requests = _requests()
    response = requests.get(API + "/v1/voices", headers={"xi-api-key": _key()}, timeout=30)
    if response.status_code >= 400:
        raise VoiceError("voices lookup failed (%d): %s" % (response.status_code, response.text[:300]))
    return [
        {
            "voice_id": v["voice_id"],
            "name": v.get("name", ""),
            "category": v.get("category", ""),
            "labels": v.get("labels") or {},
        }
        for v in response.json().get("voices", [])
    ]


# ElevenLabs' built-in voices, open to every account, each matched to its platform's register
# (spec §5): TikTok casual and fast, Reels a touch more polished, Shorts more informative.
DEFAULT_VOICES = {
    "tiktok": "TX3LPaxmHKxFdv7VOQHJ",  # Liam — energetic, young, made for social
    "instagram_reels": "Xb7hH8MSUJpSbSDYk0k2",  # Alice — clear, engaging
    "youtube_shorts": "nPczCjzI2devNBz1zQrb",  # Brian — deep, resonant
}


def pick_voices(platforms: list[str], configured: dict) -> dict[str, str]:
    """The configured voice per platform, then the platform's default, then any unused voice
    on the account — never the same voice on two platforms."""
    chosen = {p: configured[p] for p in platforms if configured.get(p)}
    for p in platforms:
        default = DEFAULT_VOICES.get(p)
        if p not in chosen and default and default not in chosen.values():
            chosen[p] = default
    missing = [p for p in platforms if p not in chosen]
    if missing:
        pool = [v["voice_id"] for v in list_voices() if v["voice_id"] not in chosen.values()]
        if len(pool) < len(missing):
            raise VoiceError("the account has too few voices to give each platform its own")
        chosen.update(zip(missing, pool))
    return chosen


def synthesize(text: str, voice_id: str, out_path: Path, model: str = DEFAULT_MODEL) -> list[Word]:
    """Speak `text` into out_path (mp3) and return word timings on the narration's own clock."""
    requests = _requests()
    response = requests.post(
        API + "/v1/text-to-speech/%s/with-timestamps" % voice_id,
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": _key(), "Content-Type": "application/json"},
        json={"text": text, "model_id": model},
        timeout=180,
    )
    if response.status_code >= 400:
        raise VoiceError("speech generation failed (%d): %s" % (response.status_code, response.text[:300]))
    body = response.json()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(body["audio_base64"]))

    words = words_from_alignment(body.get("alignment") or {})
    if words:
        return words
    # No alignment from this model: time the words by listening to what was generated.
    from .transcribe import transcribe

    return script_timed(text, transcribe(str(out_path)).words)


def script_timed(script: str, heard: list[Word]) -> list[Word]:
    """Caption text from the script, timing from what was heard.

    Whisper is good at *when* and unreliable at *what*: an accented voice reading English
    came back transcribed in Cyrillic. The script is the truth for the words on screen; the
    recording only decides where they sit. Same count: pair them up. Otherwise spread the
    script across the heard span, each word weighted by its length.
    """
    words = script.split()
    if not words:
        return []
    if len(heard) == len(words):
        return [Word(text=w, start=h.start, end=h.end) for w, h in zip(words, heard)]
    start = heard[0].start if heard else 0.0
    end = heard[-1].end if heard else len(words) / WORDS_PER_SECOND
    total = sum(len(w) + 1 for w in words)
    out, clock = [], start
    for w in words:
        span = (end - start) * (len(w) + 1) / total
        out.append(Word(text=w, start=round(clock, 3), end=round(clock + span * 0.9, 3)))
        clock += span
    return out
