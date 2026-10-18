"""Generating a shot, and knowing what it costs before asking for it.

Clipping spends one model call for a whole run. Generating spends per second of video, every
time, so this module is built around two rules. A mock adapter is the default and never spends
anything - everything downstream can be built and tested against it. And nothing real is asked
for until it has been costed and the cost allowed.

The real provider is Higgsfield: submit, get a `status_url`, poll until `completed`, then pull
the file down immediately - their outputs are only hosted for about a week.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.higgsfield.ai"
MODEL = "/bytedance/seedance-2.5/reference-to-video"
SECRETS = Path(os.getenv("SHORTS_SECRETS_DIR", ".secrets")) / "higgsfield.json"
POLL_SECONDS = 5.0
POLL_LIMIT = 600.0  # ten minutes: past that, something is wrong rather than slow

# What a second of generated video costs, by resolution. Higgsfield publishes no price list in
# the docs, so these are estimates the operator can correct: they exist to make a run refuse an
# expensive mistake, not to bill anybody. SHORTS_HF_RATE_<resolution> overrides each.
RATES = {"480p": 0.02, "720p": 0.04, "1080p": 0.08}

# The cost ladder. Tier 0 generates nothing at all - stills and ffmpeg - and is what a draft
# should use while the idea is still being argued about.
TIERS = {
    0: {"resolution": None, "seconds": 0, "label": "no generation - stills and ffmpeg"},
    1: {"resolution": "480p", "seconds": 4, "label": "cheap drafts"},
    2: {"resolution": "1080p", "seconds": 6, "label": "finals"},
    3: {"resolution": "1080p", "seconds": 10, "label": "premium - always asks first"},
}


class GenerateError(Exception):
    pass


@dataclass
class Shot:
    """One generated clip, and what it cost to get."""

    path: Path
    seconds: float
    resolution: str
    cost: float
    mocked: bool = False
    prompt: str = ""
    request_id: str = ""
    notes: list[str] = field(default_factory=list)


def credentials() -> dict | None:
    """The key pair, from the environment or the gitignored file. None when there is none."""
    key_id = os.getenv("HIGGSFIELD_KEY_ID")
    key_secret = os.getenv("HIGGSFIELD_KEY_SECRET")
    if key_id and key_secret:
        return {"key_id": key_id, "key_secret": key_secret}
    path = Path(os.getenv("HIGGSFIELD_KEY_FILE", SECRETS))
    if path.exists():
        found = json.loads(path.read_text(encoding="utf-8"))
        if found.get("key_id") and found.get("key_secret") and "PASTE" not in found["key_id"]:
            return {"key_id": found["key_id"], "key_secret": found["key_secret"]}
    return None


def provider() -> str:
    """'mock' unless there is a key *and* somebody deliberately turned spending on."""
    if os.getenv("SHORTS_GENERATOR") == "mock":
        return "mock"
    if os.getenv("SHORTS_GENERATOR") == "real" and credentials():
        return "higgsfield"
    return "mock"


def rate_for(resolution: str) -> float:
    override = os.getenv("SHORTS_HF_RATE_%s" % resolution.upper())
    return float(override) if override else RATES.get(resolution, RATES["1080p"])


def estimate(seconds: float, resolution: str, shots: int = 1) -> float:
    """What a generation would cost, in dollars. An estimate, and labelled as one."""
    return round(rate_for(resolution) * max(seconds, 0) * max(shots, 0), 4)


def affordable(spent_today: float, wanted: float, ceiling: float | None = None) -> str | None:
    """Why this generation must not happen, or None when it may."""
    ceiling = float(os.getenv("SHORTS_DAILY_SPEND_LIMIT", 5.0)) if ceiling is None else ceiling
    if wanted <= 0:
        return None
    if spent_today + wanted > ceiling:
        return (
            "this would cost about $%.2f and $%.2f of today's $%.2f has gone already"
            % (wanted, spent_today, ceiling)
        )
    return None


# ----------------------------------------------------------------- the real thing


def _headers(creds: dict) -> dict:
    return {
        "Authorization": "Key %s:%s" % (creds["key_id"], creds["key_secret"]),
        "Content-Type": "application/json",
    }


def submit(prompt: str, seconds: int, resolution: str, references: dict, creds: dict) -> dict:
    """Ask for one shot. Returns the handle: request_id and status_url."""
    import requests

    body = {
        "prompt": prompt,
        "duration": int(seconds),
        "resolution": resolution,
        "aspect_ratio": "9:16",
        # our own narration goes over the top, and the captions are timed to that
        "generate_audio": False,
        **references,
    }
    response = requests.post(API + MODEL, headers=_headers(creds), json=body, timeout=60)
    if response.status_code == 401:
        raise GenerateError("Higgsfield refused the credentials")
    if response.status_code >= 400:
        raise GenerateError("Higgsfield said %d: %s" % (response.status_code, response.text[:300]))
    handle = response.json()
    if not handle.get("status_url"):
        raise GenerateError("Higgsfield returned no status_url to follow")
    return handle


def wait_for(handle: dict, creds: dict, poll_seconds: float = POLL_SECONDS, limit: float = POLL_LIMIT) -> str:
    """Poll until the shot is made, and hand back the URL of the finished video."""
    import requests

    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        response = requests.get(handle["status_url"], headers=_headers(creds), timeout=60)
        if response.status_code >= 400:
            raise GenerateError("status check said %d: %s" % (response.status_code, response.text[:200]))
        state = response.json()
        status = (state.get("status") or "").lower()
        if status in ("completed", "succeeded", "success"):
            url = (state.get("video") or {}).get("url") or state.get("url")
            if not url:
                raise GenerateError("the shot finished with no video on it")
            return url
        if status in ("failed", "error", "cancelled", "canceled"):
            raise GenerateError("the shot %s: %s" % (status, str(state.get("detail"))[:200]))
        if status == "nsfw":
            # a terminal state: retrying only spends again on the same refusal
            raise GenerateError("the shot was refused as nsfw - rewrite the prompt")
        time.sleep(poll_seconds)
    raise GenerateError("the shot was still not ready after %d minutes" % (limit / 60))


def download(url: str, into: Path) -> Path:
    """Pull the finished video down at once: their copy is only kept about a week."""
    import requests

    into.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as response:
        if response.status_code >= 400:
            raise GenerateError("could not download the shot (%d)" % response.status_code)
        with open(into, "wb") as out:
            for chunk in response.iter_content(chunk_size=1 << 20):
                out.write(chunk)
    return into


# ----------------------------------------------------------------- the mock


def mock_shot(prompt: str, seconds: int, resolution: str, into: Path) -> Path:
    """A real file of the right shape, made locally, so the rest of the pipeline is testable.

    Not a placeholder image: it is a video of the right length and aspect, which is what the
    assembly, the narration timing and the renderer all need to be exercised honestly.
    """
    from . import media

    media.require_ffmpeg()
    height = {"480p": 854, "720p": 1280, "1080p": 1920}.get(resolution, 1920)
    width = int(height * 9 / 16) // 2 * 2
    into.parent.mkdir(parents=True, exist_ok=True)
    label = (prompt or "generated shot").replace("'", "").replace(":", " ")[:40]
    media.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "gradients=size=%dx%d:rate=24:duration=%d" % (width, height, seconds),
        "-vf", "drawtext=text='%s':fontcolor=white:fontsize=%d:x=(w-text_w)/2:y=h*0.45:"
               "box=1:boxcolor=black@0.5:boxborderw=20" % (label, max(width // 22, 12)),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-t", str(seconds), str(into),
    ])
    return into


# ----------------------------------------------------------------- what callers use


def make_shot(prompt: str, into: Path, tier: int = 1, references: dict | None = None,
              spent_today: float = 0.0, ceiling: float | None = None) -> Shot:
    """One shot, mocked or real, costed first and never silently expensive."""
    settings = TIERS.get(tier) or TIERS[1]
    if settings["resolution"] is None:
        raise GenerateError("tier 0 generates nothing - assemble it from stills instead")
    seconds, resolution = settings["seconds"], settings["resolution"]
    cost = estimate(seconds, resolution)

    if provider() == "mock":
        path = mock_shot(prompt, seconds, resolution, into)
        return Shot(path, seconds, resolution, 0.0, mocked=True, prompt=prompt,
                    notes=["made by the mock generator: nothing was generated and nothing was spent"])

    refused = affordable(spent_today, cost, ceiling)
    if refused:
        raise GenerateError(refused)
    creds = credentials()
    if not creds:
        raise GenerateError("no Higgsfield credentials - put them in %s" % SECRETS)
    handle = submit(prompt, seconds, resolution, references or {}, creds)
    url = wait_for(handle, creds)
    download(url, into)
    return Shot(into, seconds, resolution, cost, mocked=False, prompt=prompt,
                request_id=handle.get("request_id", ""))
