"""Run configuration: the {{VARIABLES}} block of the spec, with §8 defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PLATFORMS = ("tiktok", "instagram_reels", "youtube_shorts")

DEFAULT_SUBTITLE_STYLE = {
    "font": "Arial Black",
    "size": 84,
    "primary_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline": 5,
    "shadow": 2,
    "highlight_color": "#FFE14D",
    "position": "lower_middle",
}

DEFAULT_SCHEDULE = {
    "per_platform_per_day": 1,
    "times": ["09:00", "13:00", "18:00"],
    "timezone": "UTC",
    "min_gap_minutes": 180,
}


class ConfigError(Exception):
    """A missing requirement the run cannot default its way past (§8)."""


@dataclass
class Config:
    source_video: str
    connected_accounts: list[dict]
    source_transcript: str | None = None
    brand_voice: str = "clear, energetic, conversational; no corporate jargon"
    niche_keywords: list[str] = field(default_factory=list)
    clip_count: int = 3
    clip_length_range: tuple[int, int] = (15, 45)
    posting_mode: str = "draft_for_approval"
    posting_schedule: dict = field(default_factory=lambda: dict(DEFAULT_SCHEDULE))
    subtitle_style: dict = field(default_factory=lambda: dict(DEFAULT_SUBTITLE_STYLE))
    content_policy: dict = field(default_factory=dict)
    language: str | None = None
    # §4.5 rights: passed into the run, never inferred.
    rights_confirmed: bool = False
    third_party_music: bool = False
    # Instagram's Graph API pulls the asset over HTTP, so it needs a public URL.
    public_asset_base_url: str | None = None
    output_dir: str = "out"
    flags: list[str] = field(default_factory=list)

    @property
    def banned_words(self) -> list[str]:
        return [w.lower() for w in self.content_policy.get("banned_words", [])]

    @property
    def banned_topics(self) -> list[str]:
        return list(self.content_policy.get("banned_topics", []))

    @property
    def required_disclaimers(self) -> list[str]:
        return list(self.content_policy.get("required_disclaimers", []))


def load(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    flags: list[str] = []

    def defaulted(key, default, note):
        value = raw.get(key)
        if value in (None, "", [], {}):
            flags.append(note)
            return default
        return value

    # Hard requirements — stop rather than guess (§8).
    for key in ("source_video", "connected_accounts"):
        if not raw.get(key):
            raise ConfigError(f"{key} is required; cannot proceed without it")

    accounts = []
    for acct in raw["connected_accounts"]:
        if acct.get("platform") not in PLATFORMS:
            raise ConfigError(
                f"unknown platform {acct.get('platform')!r}; expected one of {PLATFORMS}"
            )
        accounts.append(acct)

    length = defaulted("clip_length_range", [15, 45], "clip_length_range missing — defaulted to 15-45s")
    mode = defaulted(
        "posting_mode",
        "draft_for_approval",
        "posting_mode missing — defaulted to draft_for_approval (never auto-publish unasked)",
    )
    if mode not in ("auto_publish", "schedule", "draft_for_approval"):
        raise ConfigError(f"unknown posting_mode {mode!r}")

    cfg = Config(
        source_video=raw["source_video"],
        connected_accounts=accounts,
        source_transcript=raw.get("source_transcript"),
        brand_voice=defaulted(
            "brand_voice", Config.brand_voice, "brand_voice missing — used a neutral default voice"
        ),
        niche_keywords=raw.get("niche_keywords") or [],
        clip_count=int(defaulted("clip_count", 3, "clip_count missing — defaulted to 3")),
        clip_length_range=(int(length[0]), int(length[1])),
        posting_mode=mode,
        posting_schedule={**DEFAULT_SCHEDULE, **(raw.get("posting_schedule") or {})},
        subtitle_style={**DEFAULT_SUBTITLE_STYLE, **(raw.get("subtitle_style") or {})},
        content_policy=raw.get("content_policy") or {},
        language=raw.get("language"),
        rights_confirmed=bool(raw.get("rights_confirmed", False)),
        third_party_music=bool(raw.get("third_party_music", False)),
        public_asset_base_url=raw.get("public_asset_base_url") or os.getenv("PUBLIC_ASSET_BASE_URL"),
        output_dir=raw.get("output_dir") or "out",
    )
    if not raw.get("subtitle_style"):
        flags.append("subtitle_style missing — used the default caption style")
    if mode == "schedule" and not raw.get("posting_schedule"):
        flags.append("posting_schedule missing — defaulted to 09:00/13:00/18:00 UTC, 1/platform/day")
    if not cfg.rights_confirmed:
        flags.append(
            "rights_confirmed is false — nothing was published; confirm rights to the source and its music"
        )
    cfg.flags = flags
    return cfg
