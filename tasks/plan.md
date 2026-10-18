# Implementation Plan: Shorts Autoclipper web dashboard (tester build)

## Overview

Put the existing Python pipeline behind a hosted web app so a few invited testers can sign in,
upload a video, watch the run, review each platform's version and publish it to their own
TikTok, Instagram and YouTube accounts. The design is approved: Direction A (dark studio), six
screens in `design/*.dc.html` (sign-in, runs, new run, run progress, review clips, accounts &
voices). The CLI keeps working unchanged.

## What the code does today that has to change

| Today | Hosted build needs |
|---|---|
| Publishers read one global token from env vars / `.secrets/` (`publish.py`, `auth.py`) | Each tester's own tokens, passed in per call |
| YouTube sign-in is a loopback flow on the operator's PC (`auth.authorize_youtube`) | Web OAuth redirect to the site, for YouTube, TikTok and Instagram |
| Progress lives in `Run.log` and is written to `run.log` at the end | Stage events the progress screen can read while the run is going |
| Drafts end the run; publishing a draft later has no entry point | "Publish this clip to this platform" from the review screen, with the synthetic-voice flag kept |
| Schedule queue is `out/queue.json`, drained by cron | Per-tester scheduled posts, drained by the worker |
| Operator's own keys, no limits | Shared Anthropic/ElevenLabs keys with a per-tester run cap |

## Architecture decisions

- **One Python codebase.** The web app imports the pipeline directly. No second language, no
  API between them.
- **FastAPI + Jinja2, server-rendered pages, a little vanilla JS** (polling progress, approve
  buttons, upload progress). No SPA or JS build step. The design's inline styles become one
  shared stylesheet and a base layout. *Lazier alternative: Flask; same shape, and FastAPI's
  test client and streaming uploads are the reason to pick it.*
- **SQLite** (stdlib `sqlite3`) for users, sessions, connections, runs, clip versions and
  scheduled posts. A handful of testers doesn't need Postgres. `# ponytail:` single file,
  single writer; move to Postgres if it ever serves more than a few dozen people.
- **A worker process runs one job at a time**, polling SQLite for queued runs and due posts.
  Transcribing and rendering are CPU-bound, so running them in parallel on one server only makes
  everyone slower. No Redis or Celery.
- **Files on a disk volume**: `/data/runs/<run_id>/` holds the upload, renders and summary.
  Deleted after 14 days.
- **Platform tokens encrypted at rest** (Fernet, key from env). This is the one new
  security-driven dependency, and it isn't skippable.
- **Sign-in with Google, limited to invited emails.** It reuses the Google Cloud project that
  already exists. The email-link option in the design waits (see open questions).
- **Hosting mirrors the marketplace's deploy**: Docker Compose with Caddy for TLS, on one VPS
  (4 vCPU / 8 GB), on a subdomain. It reuses the Caddyfile and backup-script patterns from
  `ai-b2b-marketplace/deploy/`.

## Dependency graph

```
T1 pipeline: per-call credentials + stage events + publish-one-draft
 │
 ├── T2 app skeleton (layout, DB, sessions) ── T3 Google sign-in + invites
 │                                               │
 │                                               ├── T4 new run (upload, probe, settings)
 │                                               │     └── T5 worker + progress screen
 │                                               │           ├── T6 runs list
 │                                               │           └── T7 review clips
 │                                               │                 └── T8 connect YouTube + publish   (proves per-tester creds)
 │                                               │                       ├── T9  connect TikTok + publish     (needs your TikTok app)
 │                                               │                       ├── T10 connect Instagram + publish  (needs your Meta app)
 │                                               │                       └── T12 schedule mode
 │                                               └── T11 voices + caption style defaults
 │
 └── T13 container image ── T14 deploy + legal pages update ── T15 invite first tester
```

## Task list

Full task details (acceptance criteria, verification, files) are in [todo.md](todo.md).

### Phase 1: Foundation
- [x] T1: Pipeline takes credentials and emits stage events
- [x] T2: App skeleton with the Direction A layout
- [x] T3: Invited testers sign in with Google

### Checkpoint 1
- [ ] CLI tests and render e2e still pass, and web tests pass
- [ ] An invited Google account reaches an empty Runs page; an uninvited one is refused

### Phase 2: The run loop
- [x] T4: New run: upload, probe, settings
- [x] T5: Worker runs it; progress screen follows it
- [x] T6: Runs list
- [x] T7: Review clips: watch, approve, edit copy

### Checkpoint 2
- [ ] Locally, a real 2-minute video goes from upload to three reviewed drafts in the browser
- [ ] You review the screens against the design

### Phase 3: Accounts and publishing
- [x] T8: Connect YouTube and publish a draft privately
- [ ] T9: Connect TikTok and publish
- [ ] T10: Connect Instagram and publish
- [ ] T11: Voice and caption defaults
- [ ] T12: Schedule mode

### Checkpoint 3
- [ ] One private upload each to YouTube, TikTok and Instagram (test accounts), all from the review screen
- [ ] Scheduled post goes out at its slot

### Phase 4: Hosting and testers
- [ ] T13: Container image and compose file
- [ ] T14: Deploy to the subdomain and update Terms/Privacy for a hosted service
- [ ] T15: Invite the first tester

### Phase 5: channel automation (added 18 October 2026)
- [x] T16: Find the hyped moments from the audio, not just the words
- [x] T17: Rights record per channel
- [x] T18: Watch a channel and fetch what is new
- [x] T19: Everygen: one command, start to finish
- [ ] T20: Script editor per clip, with re-record
- [x] T21: Daily schedule, several channels and several accounts at once
- [x] T22: Auto-publishing with its rails

### Phase 6: generated videos (added 18 October 2026)
- [x] T23: Higgsfield provider: mock by default, real behind a key, costed and polled
- [x] T24: Style profile from a channel's public metadata
- [ ] T25: Idea, script and shot list from the profile
- [ ] T26: Shots to a finished 9:16 video: generate, assemble, narrate, caption
- [ ] T27: Generated runs in the dashboard
- [ ] T28: Daily generation on a cadence, under a spending ceiling
- [ ] T29: Guardrails: no identity imitation, AI disclosure, mock banner, cost ceiling

### Checkpoint 5: a day on its own
- [ ] A watched channel posts overnight with nobody awake, and the digest says what went out
- [ ] A channel with no rights record never auto-posts, whatever else is set

### Checkpoint 4: ready for testers
- [ ] One invited tester completes upload → review → private publish on the live site without help
- [ ] Backups restore; tokens are encrypted in the backup

T9 and T10 wait on the TikTok and Meta apps you set up. Everything else can go ahead without
them, so they can be moved to the end without blocking Phases 2 and 4.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| CPU transcription of long videos is slow (an 80-minute video could take 15+ min on 4 vCPU) | High | One job at a time, an honest ETA on the progress screen, a max video length for the test (e.g. 60 min), a smaller whisper model on the server |
| Multi-GB browser uploads fail midway | Med | Streamed upload with a progress bar, a 4 GB cap, a clear error. Resumable upload only if testers hit it |
| Every tester must be added by hand in each platform's developer console (Google test users, TikTok sandbox target users, Instagram testers), and Google test-mode tokens lapse after 7 days | High | T15 checklist; the Accounts screen shows "reconnect" when a token is refused; plan the Google verification/TikTok audit only if the test goes well |
| Testers spend your Anthropic and ElevenLabs credit | Med | Per-tester daily run cap and max clips per run, enforced server-side, with the cost of each run logged |
| Testers upload content they have no rights to; Instagram posts are public | Med | Rights checkbox enforced server-side, drafts as the default, Terms updated, publish is always a deliberate click |
| Stored platform tokens leak | High | Encrypted at rest, key only in the server env, HTTPS only, secure/HttpOnly cookies, every file and route checked against the signed-in tester |
| The privacy policy currently says "no hosted service" | High | T14 updates Terms/Privacy before any tester is invited |
| Sharing the marketplace server would slow the marketplace during renders | Med | Separate VPS (recommended) |

## Decisions (answered 2026-09-13)

1. Domain: `shorts.letovaa.com`
2. Server: a new VPS, separate from the marketplace
3. Sign-in: Google only for the test; email link later if needed
4. Limits per tester: 3 runs a day, source videos up to 60 minutes
5. API costs: Ilya's Anthropic and ElevenLabs keys cover all testers

---

# Addendum, 18 October 2026: channel automation

## What was asked

A one-command run ("everygen"), a scripting tool, and a daily automation that pulls video from a
named channel, cuts the most hyped moments and posts them — automatically, to several accounts.

## Decisions (answered 18 October 2026)

1. **Everygen** = one command, no settings screen: a URL in, posts out, using saved defaults.
2. **Source** = any public channel, gated per channel by a written-permission record.
3. **Daily mode** = post automatically, with the rails below.
4. **Scripting** = in this product, per clip, written from the real transcript.

## The rights problem, stated plainly

Downloading another channel's videos is against YouTube's Terms of Service, and reposting them is
copyright infringement unless the owner has given permission. Platforms take this seriously:
re-uploading other people's clips is what gets accounts banned and strikes issued, and it is the
account owner — you or a tester — who carries that, not the tool.

So the automation is built to be usable, but never to pretend permission exists:

- Every watched channel carries a rights record: **you own it** (its channel id matches a connected
  YouTube account), **you have written permission** (who gave it, when, where it is kept), or
  **neither**.
- **Auto-publishing is only available on channels with one of the first two.** A channel with
  neither still gets watched and clipped, but its clips stop in Review, where a person decides.
- The rights record is shown on the source, carried into each run's summary, and included in what
  the compliance stage checks.
- Anything compliance flags never auto-posts, whatever the rights record says.

Two practical limits worth knowing before this is built: downloading needs `yt-dlp`, and YouTube
blocks server IP addresses that pull a lot (it may need cookies from a signed-in browser); and the
"most hyped moments" detector hears the audio, it does not watch the picture.

## Architecture additions

- **`shorts/hype.py`** — one ffmpeg pass gives per-second loudness; combined with words per second
  and reaction words from the transcript it marks candidate peaks, which go into the planner's
  timeline as extra lines. The model still chooses; this tells it where the room got loud.
- **`shorts/sources.py`** — a channel's RSS feed (`/feeds/videos.xml`) says when something new is
  up; `yt-dlp` fetches it into the run folder. No API key, no scraping of pages.
- **`channel_sources`** table — url, resolved channel id, rights record, preset, platforms,
  per-platform auto-publish, cadence, last video seen. Several can be added at once, one URL per
  line, sharing a preset.
- **Several accounts per platform** — `connections` becomes one row per account with a label, so
  one clip can fan out to three TikToks with spacing between them.
- **Worker** gains two more things to pick up besides runs: due channel checks, and single-clip
  re-renders from the script editor.

## Risks this adds

| Risk | Impact | Mitigation |
|---|---|---|
| Reposting other people's video gets accounts struck or banned | High | Rights record per channel; auto-publish only with ownership or permission; everything else stops in Review |
| YouTube blocks the server for downloading | Med | Back off and pause the source, say so on its card; let the tester supply browser cookies |
| An unattended run posts something wrong to every account at once | High | Compliance flags never auto-post; per-account daily caps; a global pause; a digest of everything posted |
| "Hyped" is guessed from audio only | Med | Loud ≠ good: the model still picks, confidence is shown, and the first week runs in Review for a new source |
| Fetching long videos daily fills the disk | Med | Source file deleted after a run completes; 14-day cleanup already planned in T13 |

---

# Addendum, 18 October 2026: making videos instead of cutting them

## What was asked

Point the platform at a channel worth copying, and have it *generate* new videos in that vein
with Higgsfield, then publish them through the same automation.

## The line this is built on

Generating original videos in a channel's **style** is a different thing from reposting their
work, and it is the version worth building: a format, a topic list and a pacing are not owned by
anyone. What *is* owned, and what this will not touch:

- **their footage** - nothing is downloaded from the modelled channel, ever. The style profile is
  built from public metadata only: titles, descriptions, upload rhythm, and whatever the owner
  tells us about the niche.
- **their identity** - no channel name, logo, catchphrase-as-branding, cloned voice or likeness
  in anything generated. A video that could be mistaken for theirs is the failure case, not the
  goal, and the compliance stage checks for it.
- **their scripts** - ideas are generated from the formula, never rewritten from their captions.

Generated video also carries an AI disclosure on every platform that has one, which is both the
rule and, increasingly, the thing the platforms detect anyway.

## What the API actually offers (checked 18 October 2026)

- `POST https://api.higgsfield.ai/bytedance/seedance-2.5/reference-to-video`
- Auth: `Authorization: Key <HF_API_KEY_ID>:<HF_API_KEY_SECRET>`, server-side only
- `duration` 4-30s, `aspect_ratio` includes **9:16**, `resolution` up to **1080p**, optional
  `generate_audio` (off for us - the narration is ElevenLabs and the captions must match it)
- Needs at least one reference: `image_urls`, `video_urls` or `audio_urls`, each a **public URL**
- Returns `request_id` + `status_url`; poll until `completed`, result at `video.url`
- Outputs are hosted "at least seven days" - download into the run folder immediately
- **Open question for the build:** a reference is required, so a shot starts as an image. Either
  generate the frame first (SOUL V2) and feed its hosted URL in, or use a model that takes a bare
  prompt. The catalogue lists 67 video models; the build picks one and records why.

## How a generated run works

1. **Style profile** - a channel's public feed plus the owner's own words, read once by the model
   into a formula: topics, hook shapes, length, pace, what the audience is there for.
2. **Idea and script** - one original idea per run, written to that formula: hook, beats, payoff,
   call to action, and the shot list behind it.
3. **Shots** - each beat becomes a Higgsfield generation at the chosen tier. Mock by default.
4. **Assembly** - ffmpeg stitches the shots, ElevenLabs narrates, captions are timed to the
   narration, 1080x1920 out. The existing renderer does all of this already.
5. **Review and publish** - the same screens and the same rails: compliance, drafts first,
   per-account caps, the pause switch.

## Money, which is the real risk

Clipping costs a model call. Generating costs per second of video, every time, and a loop that
generates is a loop that spends. So, following the house rule:

- **A mock provider is the default**, returning bundled sample shots, with a banner on anything
  it produced. Dev never spends.
- **Every generation is costed before it is made.** A run that would exceed its ceiling stops and
  says what it would have cost.
- **A tier ladder**: 0 is free (ffmpeg over stills, no generation), 1 is cheap drafts (480p,
  short), 2 is the real thing (1080p), 3 is premium and always asks first.
- **A daily ceiling per tester**, enforced where the daily run cap already is.

## Tasks

- [x] T23: Higgsfield provider: mock by default, real behind a key, costed and polled
- [x] T24: Style profile from a channel's public metadata
- [ ] T25: Idea, script and shot list from the profile
- [ ] T26: Shots to a finished 9:16 video: generate, assemble, narrate, caption
- [ ] T27: Generated runs in the dashboard: start one, see the script, regenerate a shot
- [ ] T28: Daily generation on a cadence, under a spending ceiling
- [ ] T29: The guardrails: no identity imitation, AI disclosure, mock banner, cost ceiling

### Checkpoint 6
- [ ] One generated video, start to finish, for less than its ceiling, reviewed before it posts
- [ ] Nothing it produced names, shows or sounds like the channel it was modelled on
