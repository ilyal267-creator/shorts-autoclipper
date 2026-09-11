"""§4.6 Render request — the instruction set turned into one ffmpeg invocation per clip.

Output is always 1080x1920. Subtitles are burned in from a generated .ass file so the
emphasis words and the safe zones survive into the pixels.
"""

from __future__ import annotations

from pathlib import Path

from . import media
from .clips import Clip, Line
from .config import Config

OUT_W, OUT_H = 1080, 1920
# §4.3 safe zones: platform chrome eats the bottom ~20% and top ~12% of frame.
BOTTOM_SAFE = int(OUT_H * 0.20)
TOP_SAFE = int(OUT_H * 0.12)


def _ass_color(hex_color: str, alpha: str = "00") -> str:
    """#RRGGBB -> ASS &HAABBGGRR."""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    r, g, b = value[0:2], value[2:4], value[4:6]
    return "&H%s%s%s%s" % (alpha, b.upper(), g.upper(), r.upper())


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "%d:%02d:%05.2f" % (int(hours), int(minutes), secs)


def _ass_text(line: Line, highlight: str) -> str:
    """Wrap the first appearance of each emphasis word in the highlight colour.

    Token by token, never by substring: emphasis can arrive as both "biggest" and "biggest,"
    and replacing text meant matching inside a tag the previous pass had just written —
    "{\\c...}{\\c...}biggest{\\r},{\\r}". Whole-word matching cannot nest.
    """
    body = line.text.replace("{", "(").replace("}", ")").replace("\n", " ")
    pending = {word.strip(".,!?…").lower() for word in line.emphasis if word.strip()}
    if not pending:
        return body

    out = []
    for token in body.split(" "):
        key = token.strip(".,!?…").lower()
        if key in pending:
            pending.discard(key)
            out.append(r"{\c%s}%s{\r}" % (highlight, token))
        else:
            out.append(token)
    return " ".join(out)


def build_ass(lines: list[Line], style: dict) -> str:
    primary = _ass_color(style.get("primary_color", "#FFFFFF"))
    outline_color = _ass_color(style.get("outline_color", "#000000"))
    highlight = _ass_color(style.get("highlight_color", "#FFE14D"))
    margin_v = BOTTOM_SAFE + 40 if style.get("position", "lower_middle") == "lower_middle" else TOP_SAFE + 40
    alignment = 2 if style.get("position", "lower_middle") == "lower_middle" else 8

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: %d" % OUT_W,
            "PlayResY: %d" % OUT_H,
            "WrapStyle: 2",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Caption,%s,%d,%s,%s,%s,&H80000000,-1,0,0,0,100,100,0,0,1,%d,%d,%d,80,80,%d,1"
            % (
                style.get("font", "Arial Black"),
                int(style.get("size", 84)),
                primary,
                primary,
                outline_color,
                int(style.get("outline", 5)),
                int(style.get("shadow", 2)),
                alignment,
                margin_v,
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    events = [
        "Dialogue: 0,%s,%s,Caption,,0,0,0,,%s"
        % (_ass_time(line.start), _ass_time(line.end), _ass_text(line, highlight))
        for line in lines
    ]
    return header + "\n" + "\n".join(events) + "\n"


def _crop_filter(reframe: dict, width: int, height: int) -> str:
    mode = reframe.get("mode", "center_crop")
    if mode == "fixed_crop" and all(k in reframe for k in ("x", "y", "w", "h")):
        return "crop=%d:%d:%d:%d" % (
            int(reframe["w"]),
            int(reframe["h"]),
            int(reframe["x"]),
            int(reframe["y"]),
        )
    # ponytail: active_speaker falls back to a centre crop — real speaker tracking needs a
    # face/VAD model per frame; add one behind this branch if two-speaker sources matter.
    crop_w = min(width, int(height * 9 / 16))
    crop_h = min(height, int(width * 16 / 9))
    return "crop=%d:%d:(iw-%d)/2:(ih-%d)/2" % (crop_w, crop_h, crop_w, crop_h)


# HLG/PQ -> SDR bt709: to linear light, into bt709 primaries, compress the highlights with a
# filmic curve, back to a bt709 transfer at 8 bits. Without it phone HDR renders flat and grey.
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


def _frame_filter(
    reframe: dict, width: int, height: int, subtitles: str, hdr: bool = False
) -> list[str]:
    """[vc] -> [vout]: get the concatenated clip into a 1080x1920 frame, then burn captions."""
    if hdr:
        return ["[vc]%s[vsdr]" % TONEMAP] + _framed(reframe, width, height, subtitles, "[vsdr]")
    return _framed(reframe, width, height, subtitles, "[vc]")


def _framed(reframe: dict, width: int, height: int, subtitles: str, source: str) -> list[str]:
    if reframe.get("mode") == "fit":
        # The whole frame, full width, over a blurred and dimmed fill of itself. For screens,
        # slides and gameplay, where any 9:16 slice throws away most of what matters. The
        # fill is blurred small and scaled back up: same look, a fraction of the work.
        return [
            "%ssplit=2[bg][fg]" % source,
            "[bg]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "scale=%d:%d,boxblur=12:2,scale=%d:%d,eq=brightness=-0.12[fill]"
            % (OUT_W, OUT_H, OUT_W, OUT_H, OUT_W // 4, OUT_H // 4, OUT_W, OUT_H),
            "[fg]scale=%d:-2[front]" % OUT_W,
            "[fill][front]overlay=(W-w)/2:(H-h)/2,setsar=1,subtitles='%s'[vout]" % subtitles,
        ]
    return [
        "%s%s,scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,subtitles='%s'[vout]"
        % (source, _crop_filter(reframe, width, height), OUT_W, OUT_H, OUT_W, OUT_H, subtitles)
    ]


def _escape_for_filter(path: Path) -> str:
    """ffmpeg filter args need the drive colon and backslashes escaped."""
    text = str(path.resolve()).replace("\\", "/")
    return text.replace(":", r"\:").replace("'", r"\'")


def audio_notes(clip: Clip) -> list[str]:
    """Pacing flags we accept but cannot honour from a single mixed track."""
    if clip.audio == "duck_under_speech":
        return [
            "%s asked for music ducked under speech — the source has one mixed track, so the "
            "audio was passed through untouched" % clip.clip_id
        ]
    if clip.audio == "trim_silence":
        return [
            "%s asked for silence trimming — handled by the planned micro-cuts only, to keep "
            "lip-sync intact" % clip.clip_id
        ]
    return []


def build_command(
    clip: Clip,
    source: str,
    ass_path: Path,
    out_path: Path,
    width: int,
    height: int,
    audio_track: int = 0,
    hdr: bool = False,
) -> list[str]:
    keeps = clip.keeps
    if not keeps:
        raise media.MediaError("%s has nothing left after its cuts" % clip.clip_id)

    parts, labels = [], []
    for i, (start, end) in enumerate(keeps):
        parts.append("[0:v:0]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[v%d]" % (start, end, i))
        # Pinned by index: a bare [0:a] on a multi-track capture picks one by luck.
        parts.append(
            "[0:a:%d]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d]" % (audio_track, start, end, i)
        )
        labels.append("[v%d][a%d]" % (i, i))
    parts.append("%sconcat=n=%d:v=1:a=1[vc][aout]" % ("".join(labels), len(keeps)))
    parts.extend(_frame_filter(clip.reframe, width, height, _escape_for_filter(ass_path), hdr))

    return [
        "ffmpeg", "-y", "-i", source,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        # say what the pixels are, so players do not guess; every render comes out SDR bt709
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]


def render(clip: Clip, cfg: Config, width: int, height: int, hdr: bool = False) -> str:
    """Render one clip and return its asset reference. Publishing waits on this returning."""
    media.require_ffmpeg()
    out_dir = Path(cfg.output_dir) / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    ass_path = out_dir / ("%s.ass" % clip.clip_id)
    ass_path.write_text(build_ass(clip.subtitles, cfg.subtitle_style), encoding="utf-8")

    out_path = out_dir / ("%s.mp4" % clip.clip_id)
    media.run(
        build_command(
            clip, cfg.source_video, ass_path, out_path, width, height, cfg.audio_track, hdr
        )
    )
    return str(out_path)
