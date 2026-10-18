"""Watching a channel: what is new, and getting one video down.

New uploads come from the channel's own RSS feed — no API key, no scraping of search pages, and
it is the feed YouTube publishes for exactly this. Fetching the video itself is `yt-dlp`.

Two things this module will not pretend about. Downloading a channel you do not own is against
YouTube's terms, so whether a source may be used at all is decided by its rights record (kept
with the source, checked before any of this runs), not here. And YouTube refuses servers that
pull a lot: that refusal comes back as a `SourceError` asking for the source to be paused, with
cookies from a signed-in browser as the way through.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=%s"
WATCH = "https://www.youtube.com/watch?v=%s"
CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
# A channel page mentions plenty of other channels - recommendations, the video owner, a
# featured list - and the first UC... on it is routinely somebody else's (checked against a
# real page: @TED's first match is a different channel entirely). Only these say whose page
# this is, and if none of them is there we stop rather than guess.
OWN_ID = (
    re.compile(r'"externalId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'<link[^>]+rel=.canonical.[^>]+/channel/(UC[0-9A-Za-z_-]{22})'),
    re.compile(r'<link[^>]+/channel/(UC[0-9A-Za-z_-]{22})[^>]+rel=.canonical.'),
    re.compile(r'og:url.[^>]+/channel/(UC[0-9A-Za-z_-]{22})'),
)
ATOM = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
# 1080p at most: the render is 1080x1920, so a 4K source costs disk and decode for nothing.
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
# What YouTube says when it has decided the server is a robot, or has simply had enough.
REFUSALS = ("not a bot", "429", "too many requests", "blocked", "captcha", "cookies")


class SourceError(Exception):
    """`pause` means the source should stop being checked until a person looks at it."""

    def __init__(self, message: str, pause: bool = False):
        super().__init__(message)
        self.pause = pause


@dataclass
class Video:
    video_id: str
    title: str
    published: str

    @property
    def url(self) -> str:
        return WATCH % self.video_id


@dataclass
class Channel:
    channel_id: str
    title: str

    @property
    def url(self) -> str:
        return "https://www.youtube.com/channel/%s" % self.channel_id


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise SourceError("requests is not installed - pip install requests") from exc
    return requests


def _get(url: str) -> str:
    requests = _requests()
    try:
        response = requests.get(url, timeout=30, headers={"User-Agent": "shorts-autoclipper/0.1"})
    except Exception as exc:  # network, TLS, DNS: all the same to the caller
        raise SourceError("could not reach YouTube: %s" % exc) from exc
    if response.status_code == 404:
        raise SourceError("no channel there")
    if response.status_code == 429:
        raise SourceError("YouTube is rate-limiting this server", pause=True)
    if response.status_code >= 400:
        raise SourceError("YouTube answered %d" % response.status_code)
    return response.text


def resolve(url_or_handle: str) -> Channel:
    """A channel URL, an @handle or a bare id, all down to one channel."""
    text = url_or_handle.strip()
    if not text:
        raise SourceError("give a channel URL or @handle")
    direct = CHANNEL_ID.search(text)
    if direct and ("/channel/" in text or text == direct.group()):
        return Channel(direct.group(), title_of(direct.group()))

    page = text if text.startswith("http") else "https://www.youtube.com/" + text.lstrip("/")
    if not re.match(r"https?://(www\.)?(m\.)?youtube\.com/", page):
        raise SourceError("that is not a YouTube channel address")
    html = _get(page)
    for pattern in OWN_ID:
        found = pattern.search(html)
        if found:
            return Channel(found.group(1), title_of(found.group(1)))
    raise SourceError("could not work out which channel that page belongs to")


def title_of(channel_id: str) -> str:
    try:
        root = ET.fromstring(_get(FEED % channel_id))
    except ET.ParseError as exc:
        raise SourceError("that channel has no usable feed") from exc
    name = root.find("atom:title", ATOM)
    return (name.text or channel_id) if name is not None else channel_id


def feed(channel_id: str) -> list[Video]:
    """The channel's recent uploads, newest first. The feed holds about 15."""
    try:
        root = ET.fromstring(_get(FEED % channel_id))
    except ET.ParseError as exc:
        raise SourceError("that channel's feed could not be read") from exc
    return parse_feed(root)


def parse_feed(root) -> list[Video]:
    videos = []
    for entry in root.findall("atom:entry", ATOM):
        video_id = entry.find("yt:videoId", ATOM)
        title = entry.find("atom:title", ATOM)
        published = entry.find("atom:published", ATOM)
        if video_id is None or not video_id.text:
            continue
        videos.append(Video(
            video_id.text,
            (title.text if title is not None else "") or "",
            (published.text if published is not None else "") or "",
        ))
    return videos


def new_since(videos: list[Video], last_video_id: str | None) -> list[Video]:
    """Everything uploaded after the one we last took, oldest first.

    A video already seen is never handed back, so a source cannot clip the same upload twice —
    and a first check hands back only the newest, rather than a channel's whole back catalogue.
    """
    if last_video_id is None:
        return videos[:1]
    fresh = []
    for video in videos:
        if video.video_id == last_video_id:
            break
        fresh.append(video)
    return list(reversed(fresh))


def _ydl(extra: dict | None = None):
    try:
        import yt_dlp
    except ImportError as exc:
        raise SourceError(
            "yt-dlp is not installed - pip install -e '.[channels]' to watch channels"
        ) from exc
    options = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    cookies = os.getenv("SHORTS_YTDLP_COOKIES")
    if cookies:
        options["cookiefile"] = cookies
    options.update(extra or {})
    return yt_dlp.YoutubeDL(options)


def _raise_for(exc: Exception) -> None:
    message = str(exc)
    lowered = message.lower()
    if any(mark in lowered for mark in REFUSALS):
        raise SourceError(
            "YouTube refused this server. It usually wants cookies from a signed-in browser: "
            "point SHORTS_YTDLP_COOKIES at a cookies.txt file. (%s)" % message[:200],
            pause=True,
        )
    raise SourceError(message[:300])


def details(url: str) -> dict:
    """What the video is, without downloading it."""
    try:
        with _ydl() as ydl:
            return ydl.extract_info(url, download=False) or {}
    except SourceError:
        raise
    except Exception as exc:
        _raise_for(exc)
        return {}


def check(info: dict, max_minutes: float) -> None:
    """Refuse the ones that cannot or should not be clipped, each with its own reason."""
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise SourceError("that one is still live - it can be clipped once it ends")
    availability = info.get("availability")
    if availability in ("subscriber_only", "premium_only", "needs_auth", "private"):
        raise SourceError("that video is not public (%s)" % availability)
    duration = float(info.get("duration") or 0)
    if duration and duration > max_minutes * 60:
        raise SourceError(
            "that video is %.0f minutes, over the %.0f-minute limit" % (duration / 60, max_minutes)
        )
    if not duration:
        raise SourceError("could not tell how long that video is")


def fetch(url: str, into: Path, max_minutes: float = 60.0) -> Path:
    """Download one video into `into`, as source.<ext>. Checks before it spends the bandwidth."""
    info = details(url)
    check(info, max_minutes)
    into.mkdir(parents=True, exist_ok=True)
    options = {"format": FORMAT, "outtmpl": str(into / "source.%(ext)s"), "merge_output_format": "mp4"}
    try:
        with _ydl(options) as ydl:
            ydl.download([url])
    except SourceError:
        raise
    except Exception as exc:
        _raise_for(exc)
    saved = sorted(into.glob("source.*"))
    if not saved:
        raise SourceError("the download finished with nothing to show for it")
    return saved[0]
