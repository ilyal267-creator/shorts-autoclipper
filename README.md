# Shorts Auto-Clipper & Publisher

One long video in → several platform-native vertical shorts out, published or queued to
TikTok, Instagram Reels and YouTube Shorts.

The agent spec in [shorts/system-prompt.md](shorts/system-prompt.md) is loaded verbatim
as the model's system prompt — it is the source of truth, not a copy of one.

## What runs where

| Stage | Who does it |
| --- | --- |
| §4.1 transcribe | `faster-whisper` locally, or a transcript you pass in |
| §4.2/4.3 segment selection + edit plan | `claude-opus-5`, one strict-schema call |
| §4.4 hook / captions / copy / hashtags | `claude-opus-5`, one call per clip, all three platforms in one schema |
| §4.5 compliance | model verdict **plus** a deterministic banned-word / disclaimer / rights check |
| §4.6 render | ffmpeg — trim, concat around micro-cuts, 9:16 crop, burned .ass subtitles |
| §4.7 publish | TikTok Content Posting API, Instagram Graph API, YouTube Data API v3 |

Every boundary the model picks is snapped to a word edge, and every subtitle is re-timed onto
the cut timeline (`shorts/clips.py`) — the model never gets to be off by a syllable.

## Setup

```bash
pip install -e .
cp .env.example .env      # fill in what you have; the CLI reads it, real env vars win
python -m shorts doctor   # checks ffmpeg + credentials
```

Requires **ffmpeg** on PATH (ffprobe optional) and Python 3.10+.

## Run

```bash
cp run.example.json run.json   # edit it
python -m shorts run --config run.json
```

Defaults are the safe ones from §8: no `posting_mode` means `draft_for_approval`, and nothing
publishes until `rights_confirmed` is `true` in the config. Output lands in `out/`:
`summary.json` (the §7 structure), `run.log`, and `assets/clip_N.mp4`.

```bash
python -m shorts run --config run.json --mode schedule   # queue instead of post
python -m shorts drain --config run.json                 # post whatever is now due (cron this)
SHORTS_DRY_RUN=1 python -m shorts run --config run.json --mode auto_publish
```

## Without an API key

The run still completes end to end using a deterministic `[MOCK]` provider — segments are
evenly spaced and every line of copy is prefixed `[MOCK]`. The summary's `generated_by` says
`mock` and a standing flag says the output must not be published. It exists so the pipeline is
testable offline, not so it can post.

## Tests

```bash
python tests/test_pipeline.py     # logic, stdlib only
python tests/test_render_e2e.py   # generates a source video and renders it; needs ffmpeg

# render into a directory it won't clean up, so you can watch the result
SHORTS_E2E_OUT=./render-check python tests/test_render_e2e.py
```

The first covers the parts that fail silently: cut-range maths, subtitle re-timing across cuts, subtitle
chunking, schedule slots, config defaults, the compliance gates, the cross-platform duplicate
detector, and the shape of the ffmpeg filtergraph. The second runs the whole pipeline against a
generated source and checks the rendered mp4 is 1080x1920, the right length, and carries burned
subtitles. Both run in CI on every push, and the render job uploads what it made as a
**rendered-clips** artifact — the mp4s, their subtitle files, the log and the summary — so you can
watch the output of a run instead of taking the assertions' word for it.

## Known limits

- **Active-speaker reframing falls back to a centre crop.** The plan accepts the mode; honouring
  it needs per-frame face tracking (`shorts/render.py`).
- **`duck_under_speech` is a no-op** — one mixed audio track can't be separated into stems. The
  run flags it rather than pretending.
- **YouTube takes a bare OAuth access token.** No refresh-token dance; wire your own token source
  in front of `YOUTUBE_ACCESS_TOKEN` for unattended runs.
- **Instagram needs a public URL** for the rendered file (`public_asset_base_url`) — the Graph API
  pulls the video rather than accepting an upload.
- **TikTok defaults to `SELF_ONLY`** because unaudited apps can't post publicly.
