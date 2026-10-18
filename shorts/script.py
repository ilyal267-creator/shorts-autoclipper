"""From a formula to a video that does not exist yet: one idea, its script, its shots.

This is the part that decides what gets made, so it is also where the rules live. Every shot
prompt has to describe something original: no named characters, no real people, no brands or
logos, nothing that would let a viewer think they are watching somebody else's video. And since
the result is generated, it says so - the disclosure is part of the copy, not an afterthought.
"""

from __future__ import annotations

from . import agent

# A shot is generated in one call, so its length is what the generator allows (4-30s) and its
# cost is linear in that. Short shots, cut together, read better than one long drifting one.
SHOT_SECONDS = (4, 10)
MAX_SHOTS = 6

SCRIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "hook", "narration", "shots", "caption", "hashtags", "why"],
    "properties": {
        "title": {"type": "string", "description": "what this video is called, for the platform"},
        "hook": {"type": "string", "description": "the first line heard or read, under 12 words"},
        "narration": {
            "type": "string",
            "description": "the whole voiceover, written to be read aloud in the clip's length",
        },
        "shots": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "seconds", "beat"],
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "what the generator should make: subject, action, camera, light",
                    },
                    # bounds live in fit(), not here: the structured-output schema takes
                    # neither maxItems on an array nor minimum/maximum on an integer
                    "seconds": {
                        "type": "integer",
                        "description": "%d to %d seconds" % SHOT_SECONDS,
                    },
                    "beat": {"type": "string", "description": "what this shot does for the story"},
                },
            },
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string", "description": "why this idea fits the formula, in one line"},
    },
}

RULES = (
    "Rules, and they are not negotiable:\n"
    "- The idea must be original. Not a version of an existing video, and not a format so "
    "generic that a hundred identical ones already exist - platforms demote exactly that.\n"
    "- No named characters, real people, brands, logos or trademarks in any shot prompt. "
    "Describe an animal, a place and an action, not a likeness.\n"
    "- Nothing distressing: no animal in pain, danger or real distress, no cruelty played for "
    "laughs, no children.\n"
    "- Write the narration to be *spoken* in the time available, at about 2.4 words a second, "
    "and leave the last beat room to land.\n"
    "- Each shot prompt stands alone: the generator sees that sentence and nothing else, so say "
    "the subject, what it does, where the camera is and what the light is like.\n"
)


SERIES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["through_line", "videos"],
    "properties": {
        "through_line": {
            "type": "string",
            "description": "what makes these one series rather than unrelated videos",
        },
        "videos": {"type": "array", "minItems": 1, "items": SCRIPT_SCHEMA},
    },
}


def _brief(profile: dict, seconds: int, brand_voice: str) -> str:
    budget = int(seconds * 2.4)
    return (
        "Topics: %s\n" % ", ".join(profile.get("topics", [])[:12])
        + "Hook shapes that work here: %s\n" % " | ".join(profile.get("hook_shapes", [])[:6])
        + "Format: %s\n" % profile.get("format", "")
        + "Pace: %s\n" % profile.get("pacing", "")
        + "Audience: %s\n" % profile.get("audience", "")
        + ("Brand voice: %s\n" % brand_voice if brand_voice else "")
        + "\nThey will be generated, not filmed: every shot is made by a video model from the "
        "prompt you write. Each video runs about %ds, so roughly %d words of narration across "
        "2 to %d shots of %d-%d seconds.\n\n" % (seconds, budget, MAX_SHOTS, *SHOT_SECONDS)
    )


def _forbidden(profile: dict, avoid: list | None) -> str:
    items = list(profile.get("avoid") or []) + list(avoid or [])
    if not items:
        return ""
    return "\nThis formula's own list of what to avoid:\n" + "\n".join("- %s" % item for item in items) + "\n"


def write_series(profile: dict, count: int = 3, seconds: int = 24, brand_voice: str = "",
                 avoid: list | None = None) -> dict:
    """A run of videos that belong together without repeating each other.

    Written in one pass on purpose: asked one at a time, a model hands back the same video with
    the nouns changed, and that is precisely what the platforms bury. Seeing the whole series at
    once is what makes each one carry its own idea.
    """
    if agent.provider() == "mock":
        return {
            "through_line": "[MOCK] no model ran",
            "videos": [_mock_script(seconds) for _ in range(count)],
        }
    prompt = (
        "Write %d short-form videos as one series, from this formula.\n\n" % count
        + _brief(profile, seconds, brand_voice)
        + "A series means a through line a viewer would notice - a recurring setup, a running "
        "joke, a format they learn - while every video carries its own idea and its own payoff. "
        "Videos that differ only in their nouns are not a series.\n\n"
        + RULES
        + _forbidden(profile, avoid)
        + "\nThe caption is for the post, not the video. Five hashtags at most."
    )
    series = agent._call(SERIES_SCHEMA, prompt)
    series["videos"] = [fit(video, seconds) for video in series.get("videos", [])]
    return series


def series_cost(series: dict, resolution: str = "1080p") -> float:
    return round(sum(cost_of(video, resolution) for video in series.get("videos", [])), 4)


def write(profile: dict, seconds: int = 24, brand_voice: str = "", avoid: list[str] | None = None) -> dict:
    """One video, written to the formula. Returns the script with its shot list."""
    if agent.provider() == "mock":
        return _mock_script(seconds)
    budget = int(seconds * 2.4)
    forbidden = list(profile.get("avoid") or []) + list(avoid or [])
    prompt = (
        "Write one short-form video, start to finish, from this formula.\n\n"
        "Topics: %s\n" % ", ".join(profile.get("topics", [])[:12])
        + "Hook shapes that work here: %s\n" % " | ".join(profile.get("hook_shapes", [])[:6])
        + "Format: %s\n" % profile.get("format", "")
        + "Pace: %s\n" % profile.get("pacing", "")
        + "Audience: %s\n" % profile.get("audience", "")
        + ("Brand voice: %s\n" % brand_voice if brand_voice else "")
        + "\nIt will be generated, not filmed: every shot is made by a video model from the "
        "prompt you write. Target length %ds in total, so about %d words of narration across "
        "%d-%d shots of %d-%ds each.\n\n" % (seconds, budget, 2, MAX_SHOTS, *SHOT_SECONDS)
        + RULES
        + ("\nThis formula's own list of what to avoid:\n%s\n" % "\n".join("- %s" % item for item in forbidden)
           if forbidden else "")
        + "\nThe caption is for the post, not the video. Hashtags: five at most, no spaces inside them."
    )
    script = agent._call(SCRIPT_SCHEMA, prompt)
    return fit(script, seconds)


def fit(script: dict, seconds: int) -> dict:
    """Trim the shot list to the length asked for, so the cost is what was estimated."""
    kept, total = [], 0
    # MAX_SHOTS is enforced here rather than in the schema: the API's structured output
    # rejects maxItems, so the cap has to be applied to what comes back
    for shot in script.get("shots", [])[:MAX_SHOTS]:
        length = max(SHOT_SECONDS[0], min(int(shot.get("seconds", SHOT_SECONDS[0])), SHOT_SECONDS[1]))
        if total + length > seconds and kept:
            break
        kept.append({**shot, "seconds": length})
        total += length
    script["shots"] = kept
    script["seconds"] = total
    return script


def cost_of(script: dict, resolution: str = "1080p") -> float:
    from . import higgsfield

    return round(sum(higgsfield.estimate(shot["seconds"], resolution) for shot in script.get("shots", [])), 4)


def problems(script: dict, profile: dict | None = None) -> list[str]:
    """What a person should look at before this is generated or posted."""
    found = []
    if not script.get("shots"):
        found.append("the script has no shots to generate")
    words = len((script.get("narration") or "").split())
    seconds = script.get("seconds") or 0
    if seconds and words > seconds * 2.8:
        found.append(
            "the narration is %d words for %ds - it will be rushed or cut off" % (words, seconds)
        )
    for shot in script.get("shots", []):
        if len((shot.get("prompt") or "").split()) < 5:
            found.append("a shot prompt is too thin to generate from: %r" % shot.get("prompt"))
    if profile:
        from .style import borrowed_identity

        found.extend(borrowed_identity(
            {"topics": [script.get("title", "")], "ideas": [script.get("caption", "")],
             "format": script.get("hook", ""), "hook_shapes": [], "pacing": "", "audience": ""},
            profile.get("modelled_on", ""),
        ))
    return found


def _mock_script(seconds: int) -> dict:
    shots = [
        {"prompt": "[MOCK] a shot that was never generated", "seconds": 4, "beat": "hook"},
        {"prompt": "[MOCK] a second shot that was never generated", "seconds": 4, "beat": "payoff"},
    ]
    return fit({
        "title": "[MOCK] an idea no model wrote",
        "hook": "[MOCK] no model ran",
        "narration": "[MOCK] no model ran, so this script is a placeholder and must not be published.",
        "shots": shots,
        "caption": "[MOCK] placeholder caption",
        "hashtags": ["#mock"],
        "why": "[MOCK] placeholder",
    }, seconds)
