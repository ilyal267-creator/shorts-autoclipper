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
| §4.6 render | ffmpeg — trim, concat around micro-cuts, 9:16 crop or `fit`, burned .ass subtitles |
| §4.7 publish | TikTok Content Posting API, Instagram Graph API, YouTube Data API v3 |

The model plans against a timeline built from word-level timings, with every cuttable silence
named by its range. A boundary within half a second of a word edge is nudged onto it so a cut
never clips a syllable; one placed deliberately in silence — holding on the action past the last
word — is kept as set. Every subtitle is then re-timed onto the cut timeline (`shorts/clips.py`).

### Framing

The planner picks a crop per clip, and can choose `fit` — the whole 16:9 frame at full width
over a blurred fill of itself, with captions on the fill below the picture. That is the right
call for screen recordings, slides and gameplay, where any 9:16 slice throws away most of the
frame. Since the planner reads the transcript and never sees the picture, `reframe_mode` in the
config forces one framing on every clip; set it to `fit` for that kind of source.

### Voiceover

With `voiceover.enabled`, Claude writes a narration script per clip *and per platform*, and an
ElevenLabs voice reads it: a different voice on TikTok, Reels and Shorts, so each post sounds
native and none is a copy of another. Scripts are held to the clip's length (about 2.4 words a
second) so the voice finishes before the picture does. Each platform gets its own render, and
the captions follow the narration, not the original speech: word timings come from ElevenLabs'
alignment, or from whisper on the generated audio with the script supplying the words.

The clip's own sound stays underneath: at -32 dB when someone is speaking in it, so two voices
never compete, and -14 dB when it is music or ambience. `original_audio_db` overrides both.
YouTube uploads declare the synthetic voice (`containsSyntheticMedia`) unless `disclose` is false.

```bash
python -m shorts voices   # your account's voices, to override the defaults per platform
```

Left unset, TikTok gets **Liam** (energetic, young), Reels **Alice** (clear, a touch more
polished) and Shorts **Brian** (deep, resonant) — ElevenLabs built-ins every account has, each
matched to its platform's tone. Set `voiceover.voices.<platform>` to any voice id to replace one.

Needs `ELEVENLABS_API_KEY` in `.env`. The default model is `eleven_v3`, ElevenLabs' most
natural-sounding voice model at the time of writing. Narration currently needs a source with
speech, because clips are chosen from the transcript; footage with no talking needs the model
to see the frames, which is the next piece of work.

### Captions

Burned-in lines are 3–6 words, broken on pauses and sentence ends. Every line is on screen for at
least half a second; a word left on its own rejoins the phrase it belongs to, following the
punctuation; and no line ever spans a micro-cut, since the cut is what made those words
neighbours. Emphasis words are highlighted once per line, by whole word.

## Setup

```bash
pip install -e '.[whisper]'   # drop [whisper] if you always supply your own transcript
cp .env.example .env          # fill in what you have; the CLI reads it, real env vars win
python -m shorts doctor       # ffmpeg, model provider, API reachability, platform tokens
```

Requires **ffmpeg** on PATH (ffprobe optional) and Python 3.10+. The first transcription downloads
the whisper model; a GPU is used when its CUDA runtime is present, CPU otherwise.

### Publishing to YouTube

Create a Google Cloud project with the **YouTube Data API v3** enabled, an OAuth consent screen
carrying the `youtube.upload` scope, and a **Desktop app** OAuth client. Save its JSON as
`.secrets/youtube_client.json` (gitignored), then sign in once:

```bash
python -m shorts auth youtube   # browser opens; pick the account that owns the channel
```

The refresh token lands in `.secrets/youtube_token.json` and every later run mints its own access
token from it. While the consent screen is in *Testing*, only listed test users can sign in and the
token lapses after 7 days; publishing the app removes both limits.

If `doctor` reports the API unreachable over TLS, something is intercepting HTTPS (a corporate
proxy, some antivirus). Point `SSL_CERT_FILE` at the CA bundle it uses for the model calls, and
`REQUESTS_CA_BUNDLE` for the publishers.

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

- **Selection reads the transcript, not the picture.** On talk — lectures, podcasts, talking
  heads — the hook and payoff are in the words and the model finds them well. On gameplay or
  anything whose payoff is visual it can only infer from gaps in the speech, and says so in its
  notes; expect fewer clips and lower confidence, and look before publishing.
- **Captions are only as good as the transcript.** Line rules fix fragments; they cannot fix a
  word whisper misheard, which is common on noisy voice comms. Draft mode exists for this.
- **Active-speaker reframing falls back to a centre crop.** The plan accepts the mode; honouring
  it needs per-frame face tracking (`shorts/render.py`).
- **`duck_under_speech` is a no-op** — one mixed audio track can't be separated into stems. The
  run flags it rather than pretending.
- **YouTube uploads are private until your Cloud project passes Google's YouTube API audit.**
  Sign-in, refresh and upload all work (verified with a live private upload); public posting
  needs the audit, and the default quota covers roughly six uploads a day.
- **Instagram needs a public URL** for the rendered file (`public_asset_base_url`) — the Graph API
  pulls the video rather than accepting an upload.
- **Silent or non-speech audio invents words.** Whisper will produce "You You You" from a
  muted track and can grind for minutes on one. Voice-activity filtering is on by default;
  `SHORTS_WHISPER_VAD=0` turns it off. Measured lossless on real narration (91/91 words).
- **Multi-track sources need `audio_track`.** Game captures often carry separate game, mic and
  desktop streams; the run cuts from track 0 unless you say otherwise, and flags the choice
  when there is more than one track so it is never silently lucky.
- **TikTok defaults to `SELF_ONLY`** because unaudited apps can't post publicly.
