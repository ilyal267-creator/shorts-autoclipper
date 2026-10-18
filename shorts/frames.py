"""Stills from a video, for when there is nothing to read.

Selection normally reads the transcript. A screen recording, a gameplay capture or a montage
has no speech in it at all, and the transcript route has nothing to work with — so the frames
are sampled and shown to the model instead. Small and few on purpose: a 512px JPEG every few
seconds is enough to see what is happening, and a hundred of them would cost more than the
rest of the run put together.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from . import media

WIDTH = 512
MAX_FRAMES = 32
MIN_GAP = 1.5  # seconds: closer than this and consecutive frames show the same thing


class Frame:
    def __init__(self, at: float, jpeg: bytes):
        self.at = at
        self.jpeg = jpeg

    @property
    def base64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")


def every_for(duration: float, max_frames: int = MAX_FRAMES) -> float:
    """How often to take a frame so a video of any length comes back as a readable handful."""
    return max(MIN_GAP, duration / max(max_frames - 1, 1))


def sample(video: str, duration: float, max_frames: int = MAX_FRAMES) -> list[Frame]:
    """Frames spread across the whole video, oldest first, with the time each was taken."""
    media.require_ffmpeg()
    every = every_for(duration, max_frames)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        media.run_logged([
            "ffmpeg", "-hide_banner", "-nostats", "-y", "-i", video,
            "-vf", "fps=1/%.3f,scale=%d:-2" % (every, WIDTH),
            "-frames:v", str(max_frames), "-q:v", "6",
            str(out / "frame_%03d.jpg"),
        ])
        frames = []
        for index, path in enumerate(sorted(out.glob("frame_*.jpg"))):
            # ffmpeg's fps filter puts the first frame at the start of the first window
            frames.append(Frame(round(index * every, 1), path.read_bytes()))
        return frames
