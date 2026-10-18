"""Reading what makes a channel work, without taking anything that belongs to it.

The evidence is public metadata only: the titles it publishes, how often, and whatever the
operator can say about why it works. No video is downloaded, no transcript taken, no caption
copied. What comes back is a formula - topics, the shape of a hook, length, pace, who it is for
- which is the part nobody owns.

The formula explicitly excludes the things that *are* owned: the channel's name, its presenter,
its logo, its catchphrases. A profile that would let someone pass themselves off as that channel
is the failure case here, not the goal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import agent

PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics", "hook_shapes", "format", "pacing", "audience", "avoid", "ideas"],
    "properties": {
        "topics": {"type": "array", "items": {"type": "string"}, "description": "subject matter this channel covers"},
        "hook_shapes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "the shapes its openings take, as patterns rather than quotes",
        },
        "format": {"type": "string", "description": "what a video of this kind is made of, start to end"},
        "pacing": {"type": "string", "description": "length, cut rhythm, how fast it moves"},
        "audience": {"type": "string", "description": "who watches and what they came for"},
        "avoid": {
            "type": "array",
            "items": {"type": "string"},
            "description": "what would make an imitation obvious, cheap or legally someone else's",
        },
        "ideas": {
            "type": "array",
            "items": {"type": "string"},
            "description": "original video ideas in this vein, owing nothing to a specific video",
        },
    },
}


@dataclass
class Evidence:
    """Everything the profile is allowed to be built from."""

    channel: str
    titles: list[str]
    per_week: float
    span_days: int
    notes: str = ""


def gather(channel_title: str, videos, notes: str = "") -> Evidence:
    """Public titles and the upload rhythm. Nothing is fetched beyond the feed already read."""
    titles = [video.title for video in videos if video.title]
    stamps = []
    for video in videos:
        try:
            stamps.append(datetime.fromisoformat(video.published.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    span_days = 0
    if len(stamps) >= 2:
        span_days = max((max(stamps) - min(stamps)).days, 1)
    per_week = round(len(stamps) / span_days * 7, 1) if span_days else 0.0
    return Evidence(channel_title, titles, per_week, span_days, notes.strip())


def learn(evidence: Evidence, brand_voice: str = "") -> dict:
    """Turn the evidence into a formula. Returns the profile, with the model's own caveats."""
    if agent.provider() == "mock":
        return _mock_profile(evidence)
    rhythm = (
        "%s posts about %.1f videos a week (%d titles over %d days)."
        % (evidence.channel, evidence.per_week, len(evidence.titles), evidence.span_days)
        if evidence.per_week
        else "%s: %d recent titles, with no reliable rhythm in them." % (evidence.channel, len(evidence.titles))
    )
    prompt = (
        "Work out the formula behind a channel, so that *original* videos can be made in the "
        "same vein.\n\n"
        + rhythm
        + "\n\nIts recent titles:\n"
        + "\n".join("- %s" % title for title in evidence.titles[:30])
        + ("\n\nWhat the operator says works about it: %s" % evidence.notes if evidence.notes else "")
        + ("\n\nThe brand voice the new videos must keep: %s" % brand_voice if brand_voice else "")
        + "\n\nThe line that matters: a format, a topic list and a pace belong to nobody, and "
        "that is what you are extracting. The channel's name, its presenter, its catchphrases, "
        "its logo and its actual videos belong to it. So:\n"
        "- describe hook *shapes* as patterns, never as quotes from these titles\n"
        "- never put the channel's name, handle or presenter in anything you return\n"
        "- `ideas` must be new videos that could stand on their own, not versions of a title above\n"
        "- `avoid` should name what would make an imitation obvious, cheap, or someone else's "
        "property - including anything that would let a viewer mistake the result for them\n"
    )
    return agent._call(PROFILE_SCHEMA, prompt)


GENERIC = {
    "the", "and", "with", "show", "shows", "daily", "weekly", "tube", "official", "channel",
    "media", "studio", "studios", "network", "live", "clips", "shorts", "videos", "world",
    "nursery", "rhyme", "rhymes", "kids", "kid", "baby", "babies", "toddler", "family",
    "music", "song", "songs", "news", "game", "games", "gaming", "vlog", "vlogs", "cooking",
    "food", "fitness", "tech", "travel", "sports", "learning", "life", "talk", "podcast",
}


FORBIDDEN_FIELDS = ("topics", "hook_shapes", "format", "pacing", "audience", "ideas")


def borrowed_identity(profile: dict, channel_title: str, handles: list[str] | None = None) -> list[str]:
    """Anywhere the profile carries the channel's identity instead of its formula.

    Checked rather than trusted: the model is told not to, and this is what notices when it
    does anyway. The caller decides - in practice the run flags it and a person reads it.
    """
    names = [channel_title] + list(handles or [])
    words = {
        word.lower()
        for name in names
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", name or "")
        # A channel name is usually part identity, part category: "Cocomelon - Nursery Rhymes"
        # owns the first word and nobody owns the rest. Matching the category would flag every
        # honest profile, so the generic half is ignored.
        if word.lower() not in GENERIC
    }
    if not words:
        return []
    found = []
    for field in FORBIDDEN_FIELDS:
        value = profile.get(field)
        text = " ".join(value) if isinstance(value, list) else str(value or "")
        for word in sorted(words):
            if re.search(r"\b%s\b" % re.escape(word), text, flags=re.IGNORECASE):
                found.append("%s names the channel it was modelled on (%r)" % (field, word))
                break
    return found


def _mock_profile(evidence: Evidence) -> dict:
    return {
        "topics": ["[MOCK] whatever the titles suggest"],
        "hook_shapes": ["[MOCK] a question, then a reversal"],
        "format": "[MOCK] hook, three beats, payoff",
        "pacing": "[MOCK] 30 seconds, cut every two",
        "audience": "[MOCK] people who came for the topic, not the presenter",
        "avoid": ["[MOCK] no model ran; this profile is a placeholder and must not be used"],
        "ideas": ["[MOCK] an original idea in this vein"],
    }


def summarise(profile: dict) -> str:
    """One line for a card or a log."""
    topics = ", ".join(profile.get("topics", [])[:3])
    return "%s · %s" % (topics or "no topics", profile.get("pacing", "")[:60])


def as_json(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False)
