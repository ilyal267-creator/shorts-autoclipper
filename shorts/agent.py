"""The model-facing half: segment selection (§4.2/4.3), copywriting (§4.4), safety (§4.5).

The spec in docs/agent-system-prompt.md *is* the system prompt — single source of truth.
Every call comes back through a strict JSON schema, so a malformed plan fails loudly here
rather than three stages later at the publish call.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .config import PLATFORMS, Config
from .transcribe import Transcript

MODEL = "claude-opus-5"
# Ships inside the package: a non-editable install has no docs/ directory.
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system-prompt.md"

CONFIDENCE = ["low", "medium", "high"]
REFRAME_MODES = ["center_crop", "fixed_crop", "active_speaker"]
AUDIO_MODES = ["preserve", "duck_under_speech", "trim_silence"]


class AgentError(Exception):
    pass


def provider() -> str:
    """'claude' when the SDK *and* a credential are present, else 'mock'. Decided once per run.

    An installed SDK with no credential would otherwise fail on the first call, halfway into
    the run — better to fall back up front and flag it than to die at stage 2.
    """
    if os.getenv("SHORTS_PROVIDER") == "mock":
        return "mock"
    try:
        import anthropic
    except ImportError:
        return "mock"
    try:
        client = anthropic.Anthropic()
    except Exception:
        return "mock"
    if client.api_key or getattr(client, "auth_token", None):
        return "claude"
    # `ant auth login` stores a profile the SDK picks up without any env var.
    return "claude" if (Path.home() / ".config" / "anthropic").exists() else "mock"


def system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _call(schema: dict, user_prompt: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise AgentError(f"model declined the request: {getattr(detail, 'explanation', '')}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise AgentError("model returned no text block")
    return json.loads(text)


# --------------------------------------------------------------------------- transcript view


def timeline(transcript: Transcript, chunk_seconds: float = 6.0) -> str:
    """Timestamped lines — enough to pick boundaries from, far cheaper than word-level JSON."""
    lines: list[str] = []
    bucket: list[str] = []
    bucket_start = None
    for word in transcript.words:
        if bucket_start is None:
            bucket_start = word.start
        bucket.append(word.text)
        if word.end - bucket_start >= chunk_seconds:
            lines.append("[%.1f] %s" % (bucket_start, " ".join(bucket)))
            bucket, bucket_start = [], None
    if bucket and bucket_start is not None:
        lines.append("[%.1f] %s" % (bucket_start, " ".join(bucket)))
    return "\n".join(lines)


def snap(value: float, transcript: Transcript, edge: str) -> float:
    """Pull a model-chosen boundary onto the nearest word edge so a cut never clips a syllable."""
    edges = [w.start for w in transcript.words] if edge == "start" else [w.end for w in transcript.words]
    if not edges:
        return value
    return min(edges, key=lambda e: abs(e - value))


# --------------------------------------------------------------------------- §4.2 + §4.3

CLIP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clips", "excluded_segments", "notes"],
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "start",
                    "end",
                    "selection_reason",
                    "confidence",
                    "cuts",
                    "reframe",
                    "audio",
                ],
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "selection_reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": CONFIDENCE},
                    "cuts": {
                        "type": "array",
                        "description": "internal ranges to remove: dead air, filler, repetition",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end"],
                            "properties": {
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                            },
                        },
                    },
                    "reframe": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["mode"],
                        "properties": {
                            "mode": {"type": "string", "enum": REFRAME_MODES},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "w": {"type": "number"},
                            "h": {"type": "number"},
                        },
                    },
                    "audio": {"type": "string", "enum": AUDIO_MODES},
                },
            },
        },
        "excluded_segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end", "reason"],
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
        "notes": {"type": "string"},
    },
}


def plan_clips(cfg: Config, transcript: Transcript, width: int, height: int) -> dict:
    if provider() == "mock":
        return _mock_clips(cfg, transcript)
    lo, hi = cfg.clip_length_range
    keywords = ", ".join(cfg.niche_keywords) or "(none given)"
    prompt = (
        "Stage 4.2 and 4.3 for this run.\n\n"
        f"Source: {cfg.source_video} — {width}x{height}, {transcript.duration:.1f}s, "
        f"language {transcript.language}.\n"
        f"Target clip count: {cfg.clip_count}. Allowed duration: {lo}-{hi}s.\n"
        f"Brand voice: {cfg.brand_voice}\n"
        f"Niche keywords: {keywords}\n\n"
        "Return fewer clips than requested rather than padding with weak ones, and say so in "
        "notes. Every clip must stand alone, open on a hook in its first 1-2 seconds, and pay "
        "that hook off before it ends. Do not let two clips make the same point. Timestamps in "
        "seconds, one decimal.\n\n"
        "Transcript:\n" + timeline(transcript)
    )
    return _call(CLIP_SCHEMA, prompt)


def _mock_clips(cfg: Config, transcript: Transcript) -> dict:
    """Deterministic placeholder so the pipeline runs (and is testable) without an API key."""
    lo, hi = cfg.clip_length_range
    stride = transcript.duration / max(cfg.clip_count, 1)
    span = min(hi, max(lo, stride * 0.6))
    clips = []
    for i in range(cfg.clip_count):
        start = i * stride
        end = start + span
        if end > transcript.duration:
            break
        clips.append(
            {
                "start": round(snap(start, transcript, "start"), 1),
                "end": round(snap(end, transcript, "end"), 1),
                "selection_reason": "[MOCK] evenly spaced segment — no model ran",
                "confidence": "low",
                "cuts": [],
                "reframe": {"mode": "center_crop"},
                "audio": "preserve",
            }
        )
    return {
        "clips": clips,
        "excluded_segments": [],
        "notes": "[MOCK] no model ran; these segments are not editorially chosen",
    }


# --------------------------------------------------------------------------- §4.4


def _copy_schema() -> dict:
    platform_obj = {
        "type": "object",
        "additionalProperties": False,
        "required": ["hook", "title", "caption", "hashtags", "emphasis_words"],
        "properties": {
            "hook": {"type": "string", "description": "3-8 words, first on-screen text"},
            "title": {
                "type": "string",
                "description": "YouTube Shorts title, <=100 chars; empty string elsewhere",
            },
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "emphasis_words": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 words from the clip to highlight in the subtitles",
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(PLATFORMS),
        "properties": {p: platform_obj for p in PLATFORMS},
    }


def write_copy(cfg: Config, clip_text: str, clip_index: int, sibling_hooks: list[str]) -> dict:
    if provider() == "mock":
        return _mock_copy(cfg, clip_text, clip_index)
    used = ", ".join(sibling_hooks) or "(none yet)"
    prompt = (
        f"Stage 4.4 for clip {clip_index + 1}.\n\n"
        f"Brand voice: {cfg.brand_voice}\n"
        f"Niche keywords: {', '.join(cfg.niche_keywords) or '(none given)'}\n"
        f"Language: {cfg.language or 'match the transcript'}\n"
        f"Banned words: {', '.join(cfg.banned_words) or '(none)'}\n"
        f"Banned topics: {', '.join(cfg.banned_topics) or '(none)'}\n"
        f"Required disclaimers: {', '.join(cfg.required_disclaimers) or '(none)'}\n"
        f"Hooks already used by other clips in this batch, do not echo them: {used}\n\n"
        "Write an independent set per platform — TikTok, Instagram Reels, YouTube Shorts. No "
        "shared strings between them: different hook, different caption, different hashtags. "
        "Vary the CTA. Only YouTube Shorts gets a title; leave the others' title empty.\n\n"
        "Clip transcript:\n" + clip_text
    )
    return _call(_copy_schema(), prompt)


def _mock_copy(cfg: Config, clip_text: str, clip_index: int) -> dict:
    seed = hashlib.sha1(clip_text.encode("utf-8")).hexdigest()[:6]
    words = clip_text.split()
    gist = " ".join(words[:6]) or "clip"
    tags = (cfg.niche_keywords or ["shorts"])[:3]
    out = {}
    for n, platform in enumerate(PLATFORMS):
        out[platform] = {
            "hook": ("[MOCK] " + gist)[:60],
            "title": ("[MOCK] %s (%s)" % (gist, seed))[:100] if platform == "youtube_shorts" else "",
            "caption": "[MOCK-%s-%d-%s] no model ran — do not publish this text"
            % (platform, clip_index + 1, seed),
            "hashtags": ["#" + t.strip("#") for t in tags] + ["#%s%s" % (platform.split("_")[0], seed)],
            "emphasis_words": words[n : n + 1],
        }
    return out


# --------------------------------------------------------------------------- §4.5

SAFETY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "categories", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "exclude", "halt_run"]},
        "categories": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}


def safety_review(cfg: Config, clip_text: str, copy: dict) -> dict:
    """`halt_run` is reserved for the minor-safety case the spec says must stop everything."""
    if provider() == "mock":
        return {
            "verdict": "pass",
            "categories": [],
            "reason": "[MOCK] no model-side policy screen ran — human review required before publishing",
        }
    prompt = (
        "Stage 4.5 for one clip. Judge the transcript and the generated copy against platform "
        "community guidelines and this run's content policy.\n\n"
        f"Banned words: {', '.join(cfg.banned_words) or '(none)'}\n"
        f"Banned topics: {', '.join(cfg.banned_topics) or '(none)'}\n\n"
        'verdict "halt_run" only for content sexualizing minors. "exclude" for any other '
        "failure — medical or financial guarantees, misleading health claims, hate or "
        'harassment, undisclosed paid promotion, or a policy breach. "pass" otherwise.\n\n'
        "Clip transcript:\n" + clip_text + "\n\nGenerated copy:\n"
        + json.dumps(copy, ensure_ascii=False, indent=2)
    )
    return _call(SAFETY_SCHEMA, prompt)
