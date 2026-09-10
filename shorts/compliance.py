"""§4.5 Compliance & safety. Deterministic policy checks plus the model's verdict.

A failed clip is excluded whole — never published with the risky part muted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config


class HaltRun(Exception):
    """Raised for the one category the spec says must stop everything, not just skip a clip."""


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _contains(haystack: str, needle: str) -> bool:
    return re.search(r"\b%s\b" % re.escape(needle), haystack, flags=re.IGNORECASE) is not None


def check(cfg: Config, clip_text: str, copy: dict, model_verdict: dict) -> Verdict:
    reasons: list[str] = []
    flags: list[str] = []

    if model_verdict.get("verdict") == "halt_run":
        raise HaltRun(model_verdict.get("reason") or "safety review halted the run")
    if model_verdict.get("verdict") == "exclude":
        reasons.append(
            "safety review: %s (%s)"
            % (model_verdict.get("reason", "policy failure"), ", ".join(model_verdict.get("categories", [])))
        )
    if model_verdict.get("reason", "").startswith("[MOCK]"):
        flags.append("no model-side policy screen ran — only the banned-word list was applied")

    copy_text = " ".join(
        str(value)
        for bundle in copy.values()
        for key, value in bundle.items()
        if key != "hashtags"
    )
    hashtag_text = " ".join(
        tag for bundle in copy.values() for tag in bundle.get("hashtags", [])
    )
    haystack = " ".join([clip_text, copy_text, hashtag_text])

    for word in cfg.banned_words:
        if _contains(haystack, word):
            reasons.append("content policy: banned word %r appears in the clip or its copy" % word)

    for disclaimer in cfg.required_disclaimers:
        missing = [p for p, b in copy.items() if disclaimer.lower() not in (b.get("caption") or "").lower()]
        if missing:
            reasons.append(
                "content policy: required disclaimer %r missing from %s"
                % (disclaimer, ", ".join(sorted(missing)))
            )

    # §4.5 rights — asserted by the caller, never inferred from the video.
    if not cfg.rights_confirmed:
        reasons.append("rights to the source video were not confirmed for this run")
    if cfg.third_party_music:
        flags.append(
            "source contains third-party music not covered by a platform sound library — "
            "cleared for render but not swapped; a human must clear it before publishing"
        )

    return Verdict(ok=not reasons, reasons=reasons, flags=flags)
