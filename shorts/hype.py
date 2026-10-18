"""Where the room got loud.

Selection reads the transcript, so it only ever sees what was *said*. A reaction — the laugh,
the shout, the moment the music kicks — is loud before it is articulate, and on a stream or a
podcast that is usually where the clip is. One ffmpeg pass gives a loudness reading per second;
combined with how fast the words are coming and what they are, it marks candidate moments for
the planner to look at. It never picks a clip: loud is a hint, not a judgement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import media

WINDOW_HZ = 16000  # one reading per second: resample to 16k, then one frame of 16k samples
QUIET_DB = -91.0  # ffmpeg reports digital silence as -inf
# Said at the moment something lands. Deliberately short: a longer list starts matching ordinary
# talk, and every extra word costs precision exactly where it matters.
REACTIONS = {
    "wow", "whoa", "woah", "haha", "hahaha", "lol", "omg", "insane", "crazy", "unbelievable",
    "ridiculous",
}
REACTION_PHRASES = (("oh", "my", "god"), ("oh", "my"), ("no", "way"), ("what", "the"), ("let's", "go"))
LOUD_DB = 4.0  # decibels above this recording's own middle before a second counts as loud
FAST_WPS = 3.2  # words a second: above this the speaker is running, not explaining
MIN_PEAK = 2.0  # seconds — shorter than this is a door slam, not a moment
MAX_PEAK = 25.0
JOIN_GAP = 2  # seconds: two loud stretches this close are one moment


@dataclass
class Peak:
    start: float
    end: float
    score: float
    why: str

    def line(self) -> str:
        return "(loud %.1f-%.1f: %s)" % (self.start, self.end, self.why)


def levels(video: str, track: int = 0) -> list[float]:
    """Loudness per second of `track`, in dBFS. Index 0 is the first second."""
    media.require_ffmpeg()
    report = media.run_logged([
        "ffmpeg", "-hide_banner", "-nostats", "-i", video,
        "-map", "0:a:%d" % track,
        # the resample belongs *inside* the chain: -ar is an output option, so on its own the
        # windows stay at the source rate and each reading covers a fraction of a second
        "-af", "aresample={hz},aformat=channel_layouts=mono,asetnsamples=n={hz},"
               "astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level".format(hz=WINDOW_HZ),
        "-f", "null", "-",
    ])
    return [
        QUIET_DB if value == "-inf" else float(value)
        for value in re.findall(r"lavfi\.astats\.Overall\.RMS_level=(-?\d+(?:\.\d+)?|-inf)", report)
    ]


def _per_second(words, seconds: int) -> tuple[list[float], list[float]]:
    """Words started in each second, and whether a reaction was one of them."""
    pace = [0.0] * seconds
    reacted = [0.0] * seconds
    cleaned = [(int(w.start), w.text.strip(".,!?…\"'").lower()) for w in words]
    for position, (index, word) in enumerate(cleaned):
        if not 0 <= index < seconds:
            continue
        pace[index] += 1
        ahead = [text for _, text in cleaned[position:position + 3]]
        if word in REACTIONS or any(list(phrase) == ahead[:len(phrase)] for phrase in REACTION_PHRASES):
            reacted[index] = 1.0
    return pace, reacted


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return QUIET_DB
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def find(levels_db: list[float], words=(), limit: int = 8) -> list[Peak]:
    """Score every second against this recording's own normal, then return the best runs."""
    if len(levels_db) < MIN_PEAK + 1:
        return []
    speaking = [db for db in levels_db if db > QUIET_DB] or list(levels_db)
    baseline = _median(speaking)
    spread = max(_median([abs(db - baseline) for db in speaking]), 1.0)

    seconds = len(levels_db)
    pace, reacted = _per_second(words, seconds)
    scores = []
    for index, db in enumerate(levels_db):
        score = max(db - baseline - LOUD_DB, 0.0) / spread
        if score > 0 and pace[index] >= FAST_WPS:
            score += 0.4
        score += reacted[index]
        scores.append(score)

    peaks = []
    for first, last in _runs(scores):
        start, end = float(first), float(last + 1)
        if not MIN_PEAK <= end - start <= MAX_PEAK:
            continue
        inside = range(first, last + 1)
        why = "+%.0fdB" % (max(levels_db[first:last + 1]) - baseline)
        if any(pace[i] >= FAST_WPS for i in inside):
            why += ", fast"
        if any(reacted[i] for i in inside):
            why += ", reaction"
        peaks.append(Peak(start, end, round(sum(scores[first:last + 1]), 2), why))
    peaks.sort(key=lambda peak: peak.score, reverse=True)
    return peaks[:limit]


def _runs(scores: list[float]) -> list[tuple[int, int]]:
    """Stretches of scoring seconds, with gaps up to JOIN_GAP treated as one stretch."""
    runs: list[tuple[int, int]] = []
    for index, score in enumerate(scores):
        if score <= 0:
            continue
        if runs and index - runs[-1][1] <= JOIN_GAP:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs


def of(video: str, track: int = 0, words=(), limit: int = 8) -> list[Peak]:
    """The whole thing: read the audio, score it, return the moments worth a look."""
    try:
        return find(levels(video, track), words, limit)
    except media.MediaError:
        return []  # a hint is never worth failing a run over


def lines(peaks: list[Peak]) -> str:
    """The marker lines the planner reads, in time order."""
    return "\n".join(peak.line() for peak in sorted(peaks, key=lambda peak: peak.start))
