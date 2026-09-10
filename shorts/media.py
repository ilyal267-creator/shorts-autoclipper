"""ffmpeg/ffprobe plumbing shared by transcription and rendering."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


class MediaError(Exception):
    pass


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg not found on PATH — install it (https://ffmpeg.org/download.html)")


@dataclass
class Probe:
    duration: float
    width: int
    height: int

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.aspect < 1.0


def probe(video: str) -> Probe:
    require_ffmpeg()
    if shutil.which("ffprobe"):
        return _probe_ffprobe(video)
    return _probe_ffmpeg(video)  # static builds often ship ffmpeg without ffprobe


def _probe_ffprobe(video: str) -> Probe:
    data = json.loads(
        run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-select_streams", "v:0", "-show_streams", video,
            ]
        )
    )
    streams = data.get("streams") or []
    if not streams:
        raise MediaError("no video stream in %s" % video)
    stream = streams[0]
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
    return Probe(duration=duration, width=int(stream["width"]), height=int(stream["height"]))


def _probe_ffmpeg(video: str) -> Probe:
    result = subprocess.run(["ffmpeg", "-i", video], capture_output=True, text=True)
    text = result.stderr  # `-i` with no output exits non-zero by design; the banner is the payload
    size = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    clock = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", text)
    if not size or not clock:
        raise MediaError("could not read %s: %s" % (video, text.strip()[-400:]))
    hours, minutes, seconds = clock.groups()
    return Probe(
        duration=int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        width=int(size.group(1)),
        height=int(size.group(2)),
    )


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError("%s failed: %s" % (cmd[0], result.stderr.strip()[-800:]))
    return result.stdout
