"""ffmpeg/ffprobe plumbing shared by transcription and rendering."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field


class MediaError(Exception):
    pass


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg not found on PATH — install it (https://ffmpeg.org/download.html)")


@dataclass
class Probe:
    """Dimensions as the video is *shown*, not as the pixels are stored."""

    duration: float
    width: int
    height: int
    # Codec per audio stream, in ffmpeg's 0:a:N order. "none" means ffmpeg has no decoder
    # for it — an iPhone's spatial-audio "apac" track, for one.
    audio_codecs: list[str] = field(default_factory=lambda: ["aac"])

    @property
    def audio_tracks(self) -> int:
        """Streams that can actually be cut from."""
        return sum(1 for codec in self.audio_codecs if codec != "none")

    def can_decode(self, track: int) -> bool:
        return 0 <= track < len(self.audio_codecs) and self.audio_codecs[track] != "none"

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.aspect < 1.0


def displayed(width: int, height: int, rotation: float) -> tuple[int, int]:
    """A phone stores portrait video as landscape pixels plus a rotation flag. ffmpeg honours
    the flag when it decodes, so every size decision has to use the rotated shape too."""
    return (height, width) if round(abs(rotation)) % 180 == 90 else (width, height)


def probe(video: str) -> Probe:
    require_ffmpeg()
    if shutil.which("ffprobe"):
        return _probe_ffprobe(video)
    return _probe_ffmpeg(video)  # static builds often ship ffmpeg without ffprobe


def _probe_ffprobe(video: str) -> Probe:
    data = json.loads(
        run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", video])
    )
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise MediaError("no video stream in %s" % video)
    stream = video_streams[0]
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
    rotation = float((stream.get("tags") or {}).get("rotate") or 0)
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = float(side["rotation"])
    width, height = displayed(int(stream["width"]), int(stream["height"]), rotation)
    return Probe(
        duration=duration,
        width=width,
        height=height,
        audio_codecs=[
            s.get("codec_name") or "none" for s in streams if s.get("codec_type") == "audio"
        ],
    )


def _probe_ffmpeg(video: str) -> Probe:
    result = subprocess.run(["ffmpeg", "-i", video], capture_output=True, text=True)
    text = result.stderr  # `-i` with no output exits non-zero by design; the banner is the payload
    size = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    clock = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", text)
    if not size or not clock:
        raise MediaError("could not read %s: %s" % (video, text.strip()[-400:]))
    hours, minutes, seconds = clock.groups()
    turn = re.search(r"displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees", text)
    width, height = displayed(
        int(size.group(1)), int(size.group(2)), float(turn.group(1)) if turn else 0.0
    )
    return Probe(
        duration=int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        width=width,
        height=height,
        audio_codecs=re.findall(r"Stream #\d+:\d+[^\n]*?: Audio: (\w+)", text),
    )


def audio_extract_cmd(video: str, track: int, out_path: str) -> list[str]:
    """Pull one audio stream down to what whisper wants: 16kHz mono PCM."""
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video,
        "-map", "0:a:%d" % track,
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        out_path,
    ]


def extract_audio(video: str, track: int, out_path: str) -> str:
    require_ffmpeg()
    run(audio_extract_cmd(video, track, out_path))
    return out_path


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError("%s failed: %s" % (cmd[0], result.stderr.strip()[-800:]))
    return result.stdout
