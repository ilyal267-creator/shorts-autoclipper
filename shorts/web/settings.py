"""What a tester wants by default, and the run config built from it.

One place decides what a run looks like when nobody fills in a form: the one-command run, a
watched channel's nightly fetch, and the New run screen's starting state all read this. When
nothing has been saved, the documented defaults below are used and the run says which.
"""

from __future__ import annotations

import json

from ..config import PLATFORMS
from .runs import PLATFORM_NAMES
from . import db

DEFAULTS = {
    "clip_count": 3,
    "clip_length_range": [15, 45],
    "framing": "auto",  # the planner decides, unless told otherwise
    "platforms": list(PLATFORMS),
    "voiceover": False,
}
FRAMING = {"auto": None, "center_crop": "center_crop", "fit": "fit"}
FRAMING_LABELS = {"auto": "the planner decides", "center_crop": "a 9:16 crop", "fit": "the whole frame"}


def saved(db_path, user_id: int) -> dict:
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT json FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["json"]) if row else {}


def defaults_for(db_path, user_id: int) -> dict:
    """The tester's defaults, filled in from the documented ones."""
    return {**DEFAULTS, **saved(db_path, user_id)}


def save(db_path, user_id: int, wanted: dict) -> dict:
    """Keep only what is a setting, only when it makes sense, and only on top of the rest.

    Merged rather than replaced: saving one thing must not quietly wipe the others, and an
    empty platform list is a mistake, not a decision to post nowhere."""
    keep = dict(saved(db_path, user_id))
    if "clip_count" in wanted and 1 <= int(wanted["clip_count"]) <= 5:
        keep["clip_count"] = int(wanted["clip_count"])
    if wanted.get("framing") in FRAMING:
        keep["framing"] = wanted["framing"]
    platforms = [p for p in (wanted.get("platforms") or []) if p in PLATFORMS]
    if platforms:
        keep["platforms"] = platforms
    if "voiceover" in wanted:
        keep["voiceover"] = bool(wanted["voiceover"])
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO user_settings (user_id, json) VALUES (?, ?)
                   ON CONFLICT (user_id) DO UPDATE SET json = excluded.json,
                   updated_at = datetime('now')""",
                (user_id, json.dumps(keep)),
            )
    finally:
        conn.close()
    return keep


def described(db_path, user_id: int) -> str:
    """One line a run can print: what it is about to do, and where that came from."""
    settings = defaults_for(db_path, user_id)
    stored = saved(db_path, user_id)
    source = "your saved defaults" if stored else "the standard defaults (nothing saved yet)"
    return "%d clips, %s, %s%s - from %s" % (
        settings["clip_count"],
        FRAMING_LABELS.get(settings["framing"], settings["framing"]),
        ", ".join(PLATFORM_NAMES.get(p, p) for p in settings["platforms"]),
        ", with narration" if settings["voiceover"] else "",
        source,
    )


def run_config(db_path, user_id: int, source_video, output_dir, rights: dict | None = None) -> dict:
    """The raw run config these defaults produce. Everything that starts a run goes through here."""
    settings = defaults_for(db_path, user_id)
    return {
        "source_video": str(source_video),
        "connected_accounts": [
            {"platform": platform, "account_id": "pending", "handle": ""}
            for platform in settings["platforms"]
        ],
        "clip_count": settings["clip_count"],
        "clip_length_range": list(settings["clip_length_range"]),
        # Drafts first, always. Posting by itself is decided per clip afterwards, against the
        # channel's rights record and the rails in `posting`.
        "posting_mode": "draft_for_approval",
        "rights_confirmed": True,
        "rights": rights or {},
        "audio_track": 0,
        "reframe_mode": FRAMING[settings["framing"]],
        "voiceover": {"enabled": bool(settings["voiceover"]), "disclose": True},
        "output_dir": str(output_dir),
    }
