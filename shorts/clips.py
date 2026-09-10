"""Deterministic clip maths: what survives the cuts, and where it lands on the new timeline.

Nothing here talks to a model or a network. §4.3's micro-cuts shift every subtitle after
them, so the re-timing in `retime` is what keeps captions in sync with the rendered clip.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .transcribe import Word

SENTENCE_END = ".?!…"


@dataclass
class Line:
    """One subtitle line on the *clip* timeline."""

    text: str
    start: float
    end: float
    emphasis: list[str] = field(default_factory=list)


@dataclass
class Clip:
    clip_id: str
    start: float
    end: float
    cuts: list[tuple[float, float]]
    reframe: dict
    audio: str
    selection_reason: str
    confidence: str
    copy: dict = field(default_factory=dict)
    subtitles: list[Line] = field(default_factory=list)
    asset_ref: str | None = None
    excluded_reason: str | None = None

    @property
    def keeps(self) -> list[tuple[float, float]]:
        return keep_ranges(self.start, self.end, self.cuts)

    @property
    def duration(self) -> float:
        return round(sum(e - s for s, e in self.keeps), 3)


def keep_ranges(start: float, end: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[start, end] minus every cut range. Overlapping and out-of-order cuts are handled."""
    if end <= start:
        return []
    clipped = []
    for c_start, c_end in cuts:
        c_start, c_end = max(min(c_start, c_end), start), min(max(c_start, c_end), end)
        if c_end > c_start:
            clipped.append((c_start, c_end))
    clipped.sort()

    merged: list[list[float]] = []
    for c_start, c_end in clipped:
        if merged and c_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], c_end)
        else:
            merged.append([c_start, c_end])

    keeps, cursor = [], start
    for c_start, c_end in merged:
        if c_start > cursor:
            keeps.append((round(cursor, 3), round(c_start, 3)))
        cursor = max(cursor, c_end)
    if end > cursor:
        keeps.append((round(cursor, 3), round(end, 3)))
    return keeps


def map_time(t: float, keeps: list[tuple[float, float]]) -> float | None:
    """Source time -> clip time. None if `t` fell inside a removed range."""
    offset = 0.0
    for k_start, k_end in keeps:
        if k_start - 1e-6 <= t <= k_end + 1e-6:
            return round(offset + (t - k_start), 3)
        offset += k_end - k_start
    return None


def retime(words: list[Word], keeps: list[tuple[float, float]]) -> list[Word]:
    """Words that survive the cuts, restamped onto the clip's own timeline (§4.4b)."""
    out = []
    for word in words:
        start = map_time(word.start, keeps)
        end = map_time(word.end, keeps)
        if start is None or end is None or end <= start:
            continue  # straddles a cut boundary — drop rather than stretch it
        out.append(Word(text=word.text, start=start, end=end))
    return out


MIN_DISPLAY = 0.5  # seconds a line must be on screen to be readable at all


def _ends_sentence(text: str) -> bool:
    """An ellipsis is the opposite of a full stop — the speaker is trailing off, not finishing."""
    stripped = text.rstrip()
    if stripped.endswith("...") or stripped.endswith("…"):
        return False
    return stripped.endswith(tuple(SENTENCE_END))


def merge_strays(
    lines: list[Line],
    max_words: int = 5,
    join_gap: float = 0.35,
    forward_gap: float = 2.0,
    clip_end: float | None = None,
) -> list[Line]:
    """Reunite a lone word with the phrase it belongs to.

    Chunking on a word count or a pause can leave one word by itself: "Look at me, look at" /
    "me.", or "What" waiting on the rest of its question. Long enough to read, still wrong to
    look at. Which way it joins follows the punctuation — a word that ends a sentence closes
    the line before it, one that does not opens the line after it.
    """
    index = 0
    while index < len(lines):
        line = lines[index]
        if len(line.text.split()) > 1:
            index += 1
            continue

        previous = lines[index - 1] if index else None
        following = lines[index + 1] if index + 1 < len(lines) else None
        room = max_words + 1  # §4.4b wants 3-6 words a line; one over the chunk size, no more

        if (
            previous is not None
            and not _ends_sentence(previous.text)
            and line.start - previous.end <= join_gap
            and len(previous.text.split()) + 1 <= room
        ):
            previous.text = "%s %s" % (previous.text, line.text)
            previous.end = max(previous.end, line.end)
            previous.emphasis += line.emphasis
            del lines[index]
            continue

        if (
            following is not None
            and not _ends_sentence(line.text)
            and following.start - line.end <= forward_gap
            and len(following.text.split()) + 1 <= room
        ):
            following.text = "%s %s" % (line.text, following.text)
            following.start = line.start
            following.emphasis = line.emphasis + following.emphasis
            del lines[index]
            continue

        index += 1  # genuinely standing alone — a one-word answer, or nothing to join

    # The out-point can cut a phrase mid-flow, leaving a word with nothing after it to join.
    # It reads as a typo however long it is on screen, so it goes. Only when it truly abuts the
    # out-point though: a lone word with clear air after it was spoken that way.
    if len(lines) > 1 and clip_end is not None:
        last = lines[-1]
        sliced = last.end >= clip_end - join_gap
        if sliced and len(last.text.split()) == 1 and not _ends_sentence(last.text):
            del lines[-1]
    return lines


def tidy_lines(
    lines: list[Line],
    clip_end: float | None = None,
    min_display: float = MIN_DISPLAY,
    max_gap: float = 0.6,
) -> list[Line]:
    """Give every line time to be read: stretch it, merge it, or — if the cut sliced through
    a phrase at the clip's edge — drop the leftover word rather than flash it for 0.1s."""
    lines = [Line(line.text, line.start, line.end, list(line.emphasis)) for line in lines]
    lines = merge_strays(lines, clip_end=clip_end)  # reunite fragments first; often fixes timing too
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.end - line.start >= min_display:
            index += 1
            continue

        # 1. stretch into whatever room follows, without running into the next line
        ceiling = lines[index + 1].start if index + 1 < len(lines) else clip_end
        wanted = line.start + min_display
        line.end = min(wanted, ceiling) if ceiling is not None else wanted
        if line.end - line.start >= min_display:
            index += 1
            continue

        # 2. still too brief — fold it into a neighbour it runs together with
        previous = lines[index - 1] if index else None
        following = lines[index + 1] if index + 1 < len(lines) else None
        if previous is not None and line.start - previous.end <= max_gap:
            previous.text = "%s %s" % (previous.text, line.text)
            previous.end = max(previous.end, line.end)
            previous.emphasis += line.emphasis
        elif following is not None and following.start - line.end <= max_gap:
            following.text = "%s %s" % (line.text, following.text)
            following.start = line.start
            following.emphasis = line.emphasis + following.emphasis
        # 3. otherwise it is an orphan against the clip boundary: drop it
        del lines[index]
    return lines


def subtitle_lines(
    words: list[Word],
    emphasis: list[str] | None = None,
    max_words: int = 5,
    max_gap: float = 0.6,
    clip_end: float | None = None,
) -> list[Line]:
    """3-6 word chunks broken on natural pauses and sentence ends, not dumped by sentence."""
    wanted = {w.strip(".,!?").lower() for w in (emphasis or []) if w.strip()}
    lines: list[Line] = []
    bucket: list[Word] = []

    def flush() -> None:
        if not bucket:
            return
        hits = [w.text for w in bucket if w.text.strip(".,!?").lower() in wanted]
        lines.append(
            Line(
                text=" ".join(w.text for w in bucket),
                start=bucket[0].start,
                end=bucket[-1].end,
                emphasis=hits,
            )
        )
        bucket.clear()

    for word in words:
        if bucket and word.start - bucket[-1].end > max_gap:
            flush()
        bucket.append(word)
        if len(bucket) >= max_words or word.text.rstrip().endswith(tuple(SENTENCE_END)):
            flush()
    flush()
    return tidy_lines(lines, clip_end=clip_end, max_gap=max_gap)


def validate(clip: Clip, length_range: tuple[int, int], others: list[Clip]) -> list[str]:
    """Problems that should keep a clip out of the batch (§4.2 length fit, distinctiveness)."""
    lo, hi = length_range
    problems = []
    if clip.end <= clip.start:
        problems.append("end is not after start")
    if clip.duration < lo:
        problems.append("runs %.1fs after cuts, under the %ds floor" % (clip.duration, lo))
    if clip.duration > hi:
        problems.append("runs %.1fs after cuts, over the %ds ceiling" % (clip.duration, hi))
    for other in others:
        if other.clip_id == clip.clip_id:
            continue
        overlap = min(clip.end, other.end) - max(clip.start, other.start)
        if overlap > 0.5 * min(clip.end - clip.start, other.end - other.start):
            problems.append("overlaps %s by %.1fs — not a distinct clip" % (other.clip_id, overlap))
    return problems


# §5 platform specs: (min hashtags, max hashtags, caption chars before it hurts, hard caption cap)
PLATFORM_NORMS = {
    "tiktok": (3, 5, 300, 2200),
    "instagram_reels": (3, 8, 2200, 2200),
    "youtube_shorts": (1, 3, 5000, 5000),
}


def platform_notes(platform: str, bundle: dict) -> list[str]:
    """Advisory drift from §5's table — flagged, never silently rewritten."""
    norms = PLATFORM_NORMS.get(platform)
    if not norms:
        return []
    low, high, soft_caption, hard_caption = norms
    caption = bundle.get("caption") or ""
    tags = bundle.get("hashtags") or []
    notes = []

    if not caption.strip():
        notes.append("%s caption is empty" % platform)
    if len(caption) > hard_caption:
        notes.append("%s caption is %d chars, over the %d limit" % (platform, len(caption), hard_caption))
    elif len(caption) > soft_caption:
        notes.append("%s caption is %d chars, longer than the platform's norm" % (platform, len(caption)))
    if not low <= len(tags) <= high:
        notes.append("%s has %d hashtags; §5 wants %d-%d" % (platform, len(tags), low, high))
    if platform == "youtube_shorts":
        title = bundle.get("title") or ""
        if not title.strip():
            notes.append("youtube_shorts has no title — the title field is its search surface")
        elif len(title) > 100:
            notes.append("youtube_shorts title is %d chars and will be truncated at 100" % len(title))
    elif bundle.get("title"):
        notes.append("%s has a title field, which that platform does not show" % platform)
    return notes


def duplicate_copy(copy: dict) -> list[str]:
    """§4.4: identical text across platforms is the anti-duplication signal we must not send."""
    problems = []
    for field_name in ("hook", "caption"):
        seen: dict[str, str] = {}
        for platform, bundle in copy.items():
            value = (bundle.get(field_name) or "").strip().lower()
            if not value:
                continue
            if value in seen:
                problems.append(
                    "%s is identical on %s and %s" % (field_name, seen[value], platform)
                )
            seen[value] = platform
    return problems
