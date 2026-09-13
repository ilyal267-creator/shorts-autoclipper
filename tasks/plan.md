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
- [ ] T7: Review clips: watch, approve, edit copy

### Checkpoint 2
- [ ] Locally, a real 2-minute video goes from upload to three reviewed drafts in the browser
- [ ] You review the screens against the design

### Phase 3: Accounts and publishing
- [ ] T8: Connect YouTube and publish a draft privately
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
