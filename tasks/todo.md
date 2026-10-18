# Tasks: Shorts Autoclipper web dashboard

Plan and decisions: [plan.md](plan.md). Design: `design/*.dc.html` (Direction A).

Test commands used below:
- `python tests/test_pipeline.py`: pipeline logic (exists)
- `python tests/test_render_e2e.py`: real ffmpeg render (exists)
- `python tests/test_web.py`: web app through FastAPI's TestClient (added in T2)

---

## Phase 1: Foundation

### T1: Pipeline takes credentials and emits stage events

**Description:** Make the pipeline usable by more than one person, without touching the web
layer yet. Each publisher takes a `creds` dict instead of reading env vars and `.secrets/`. `Run`
accepts an `on_event(stage, message)` callback that fires at each stage (probe, transcribe, plan,
copy per clip, compliance, voiceover, render, deliver). A new `publish_draft(row, platform,
account, creds)` publishes one stored draft. Draft rows keep `synthetic_media`, which
`_text_fields` drops today, so a later publish still declares the AI voice. The CLI builds
`creds` from env and the token file, so its behaviour is unchanged.

**Acceptance criteria:**
- [x] With no env vars set, a publisher given `creds` uses exactly those tokens (checked with a stubbed `requests`)
- [x] A mock-provider run reports every stage, in order, to `on_event`
- [x] Publishing a stored narrated draft sends `containsSyntheticMedia` to YouTube

**Verification:**
- [x] `python tests/test_pipeline.py` passes (26 checks)
- [x] `python tests/test_render_e2e.py` passes, now also checking stage order
- [x] `python -m shorts run --config run.json` (mock provider) still writes the same summary shape

**Dependencies:** None

**Files likely touched:** `shorts/publish.py`, `shorts/run.py`, `shorts/auth.py`, `shorts/__main__.py`, `tests/test_pipeline.py`

**Estimated scope:** M

---

### T2: App skeleton with the Direction A layout

**Description:** A FastAPI app in `shorts/web/` with Jinja2 templates. It has a base layout (the
sidebar and top bar from the design, colours and type as one stylesheet), a SQLite schema
created on start (users, sessions, connections, runs, clip_versions, scheduled_posts), and a
`/health` route. For now it serves a placeholder Runs page to anyone. It starts with
`python -m shorts.web`.

**Acceptance criteria:**
- [x] `python -m shorts.web serve` serves the Runs page with the sidebar looking like `design/Runs.dc.html` (empty state)
- [x] The database is created and migrated on first start and survives a restart (each feature adds its own tables by migration)
- [x] Web dependencies go in a `[web]` extra, so the CLI install stays lean

**Verification:**
- [x] `python tests/test_web.py`: `/health` returns 200, the Runs page renders
- [x] Manual: checked in the browser against the design's sidebar and header

**Dependencies:** None (can run parallel to T1)

**Files likely touched:** `shorts/web/__init__.py`, `shorts/web/app.py`, `shorts/web/db.py`, `shorts/web/templates/base.html`, `shorts/web/static/app.css`, `pyproject.toml`, `tests/test_web.py`

**Estimated scope:** M

---

### T3: Invited testers sign in with Google

**Description:** The sign-in screen from `design/SignIn.dc.html`, with Google OAuth (web client,
server-side code exchange). Only emails in the `users` table get a session. Adds
`python -m shorts.web invite <email>` and `revoke <email>`. Sessions use HttpOnly, Secure,
SameSite=Lax cookies. Every page except sign-in and health requires a session. Sign out works.

**Acceptance criteria:**
- [x] An invited email lands on Runs after Google sign-in; an uninvited email sees "no invite" and gets no session
- [x] Revoking an email ends that tester's sessions on their next request
- [x] OAuth `state` is checked; a forged callback is rejected

**Verification:**
- [x] `python tests/test_web.py`: invite, session guard, revoke and state mismatch (Google token endpoint stubbed)
- [ ] Manual: sign in locally with your Google account on `http://localhost` (waiting on the Web OAuth client)

**Dependencies:** T2

**Files likely touched:** `shorts/web/auth.py`, `shorts/web/app.py`, `shorts/web/templates/signin.html`, `shorts/web/__main__.py`, `tests/test_web.py`

**Estimated scope:** M

### Checkpoint 1
- [ ] `test_pipeline`, `test_render_e2e` and `test_web` all pass
- [ ] Invited account signs in; uninvited account refused
- [ ] Review with Ilya before Phase 2

---

## Phase 2: The run loop

### T4: New run: upload, probe, settings

**Description:** The New run screen (`design/NewRun.dc.html`). The browser streams the upload
with a progress bar to `/data/runs/<id>/`. The server probes the file with the existing
`media.probe` and shows duration, resolution, HDR and an audio-track picker when there is more
than one track. Settings: clip count, framing, platforms, voiceover with a voice per platform,
posting mode. Start run validates on the server and stores a queued run whose config is built by
`config.from_dict`.

**Acceptance criteria:**
- [x] A 2-track file shows the track picker, and the chosen track is saved into the run's config
- [x] Start run is refused server-side without the rights box, over the size cap (4 GB), over the tester's daily run cap, or for an unreadable file
- [x] The saved config loads with `config.from_dict` without flags beyond the pipeline's normal defaults

**Verification:**
- [x] `python tests/test_web.py`: upload a generated clip, probe values shown, refusal cases
- [x] Manual: a generated 2-track 1080p file uploaded, track 2 chosen in the browser, run queued with those settings (the iPhone `.mov` is no longer on disk; rotation/HDR probing is covered by test_pipeline)

**Dependencies:** T1, T3

**Files likely touched:** `shorts/web/runs.py`, `shorts/web/templates/new_run.html`, `shorts/web/static/upload.js`, `shorts/web/db.py`, `tests/test_web.py`

**Estimated scope:** M

---

### T5: Worker runs it; progress screen follows it

**Description:** `python -m shorts.web worker` takes the oldest queued run, executes it with
`run.execute`, and stores each `on_event` in the run's event log plus the final summary. The
progress screen (`design/Progress.dc.html`) polls a JSON endpoint every 2 s: stage checklist, a
"clips found" list once selection is done, and the log tail. Cancel stops the run between
stages. A worker restart marks an interrupted run failed instead of leaving it "running" forever.

**Acceptance criteria:**
- [x] A queued mock-provider run moves through every stage on the screen without a reload and ends in "Needs review"
- [x] Cancel during transcription ends the run as cancelled within one stage
- [x] Killing the worker mid-run leaves the run marked failed, with the reason, after the next worker start

**Verification:**
- [x] `python tests/test_web.py`: worker loop on a generated clip with the mock provider, events stored in order
- [x] Manual: a 105 s talking video (Windows speech over a test picture) with Claude: 2 clips picked and rendered, screen followed it live; a no-speech file failed with its reason shown

**Dependencies:** T1, T4

**Files likely touched:** `shorts/web/worker.py`, `shorts/web/runs.py`, `shorts/web/templates/progress.html`, `shorts/web/static/progress.js`, `tests/test_web.py`

**Estimated scope:** M

---

### T6: Runs list

**Description:** The Runs screen (`design/Runs.dc.html`) lists the signed-in tester's runs
only, newest first, with source, start time, clip count, platforms, mode and status. A running
row shows its stage and progress. Each row links to progress or review, depending on its state.

**Acceptance criteria:**
- [x] Tester A never sees, opens or downloads anything from tester B's runs, including by guessing IDs
- [x] Every status renders: needs review, running (with its stage), no clips, failed, cancelled, not started (scheduled/done arrive with publishing)

**Verification:**
- [x] `python tests/test_web.py`: two testers, cross-access returns 404 for pages (files are served from T7 and get the same check)
- [x] Manual: compared with the design, with three real runs

**Dependencies:** T5

**Files likely touched:** `shorts/web/runs.py`, `shorts/web/templates/runs.html`, `tests/test_web.py`

**Estimated scope:** S

---

### T7: Review clips: watch, approve, edit copy

**Description:** The review screen (`design/Main.dc.html`). There is a tab per clip and a card
per platform: a video player (range requests, owner-checked), voice name, hook, caption,
hashtags, compliance flags, and Approve/Undo. Edit copy changes the title, caption and hashtags
only. The spoken narration and burned captions stay as rendered, and the edit box says so.
Approval state lives in `clip_versions`. Publish buttons appear only once that platform is
connected (T8–T10).

**Acceptance criteria:**
- [x] Every rendered platform version plays in the browser and seeks
- [x] Approve/undo and copy edits persist across reloads (T8 publishes the edited copy)
- [x] Compliance flags from the summary show on the card they belong to

**Verification:**
- [x] `python tests/test_web.py`: approve, edit, video served with a 206 range response only to the owner
- [x] Manual: reviewed the real run from T5 in the browser; both videos load at 1080×1920, approve/undo work

**Dependencies:** T5

**Files likely touched:** `shorts/web/review.py`, `shorts/web/templates/review.html`, `shorts/web/static/review.js`, `shorts/web/db.py`, `tests/test_web.py`

**Estimated scope:** M

### Checkpoint 2
- [ ] All tests pass
- [ ] Locally: a real video goes upload → progress → three reviewed drafts, entirely in the browser
- [ ] Ilya reviews the screens against the design

---

## Phase 3: Accounts and publishing

### T8: Connect YouTube and publish a draft privately

**Description:** The Accounts screen (`design/Accounts.dc.html`), YouTube card only. It uses a
web OAuth client with a redirect to `/connect/youtube/callback`. The refresh token is stored
encrypted in `connections` for that tester, and Disconnect revokes it. On the review screen,
Publish sends an approved draft with `publish_draft` and that tester's creds, privately. The
post ID and link are stored and shown. A refused token turns the card to "Reconnect".

**Acceptance criteria:**
- [x] A tester connects their channel and publishes an approved draft, privately, with the copy as they edited it (live upload still to do - see below)
- [x] The token is unreadable in the database file without the encryption key
- [x] A revoked or expired token shows "Reconnect" instead of a raw error

**Verification:**
- [x] `python tests/test_web.py`: callback stored sealed, forged callback and unticked scope refused, publish uses that tester's token, reconnect path (12 checks)
- [ ] Manual: one live private upload - waiting on the consent screen getting the two YouTube scopes and the new redirect URI

**Dependencies:** T1, T7

**Files likely touched:** `shorts/web/connections.py`, `shorts/web/crypto.py`, `shorts/web/templates/accounts.html`, `shorts/web/review.py`, `tests/test_web.py`

**Estimated scope:** M

---

### T9: Connect TikTok and publish

**Description:** TikTok Login Kit web flow (PKCE) with `user.info.basic` and `video.publish`.
The 24-hour access token is refreshed from the stored refresh token before each post, and the
creator info query is checked before posting. Publishing is privacy SELF_ONLY until the app
passes TikTok's audit.

**Acceptance criteria:**
- [ ] A sandbox target user connects, and the handle shows on the card
- [ ] An approved draft posts to their account as private, and its status is fetched and stored
- [ ] An access token older than 24 hours is refreshed automatically before posting

**Verification:**
- [ ] `python tests/test_web.py`: refresh-before-post with the TikTok endpoints stubbed
- [ ] Manual: one live private post to the TikTok test account

**Dependencies:** T8; your TikTok app (client key/secret, sandbox, redirect URI on the domain)

**Files likely touched:** `shorts/web/connections.py`, `shorts/publish.py`, `shorts/web/templates/accounts.html`, `tests/test_web.py`

**Estimated scope:** M

---

### T10: Connect Instagram and publish

**Description:** Instagram API with Instagram Login (`instagram_business_basic` and
`instagram_business_content_publish`). A long-lived token is stored and refreshed before its
60 days run out. Publishing switches from `video_url` to Instagram's resumable direct upload, so
no public file URL is needed. The card warns that Reels post publicly.

**Acceptance criteria:**
- [ ] A tester's professional account connects, and the username shows on the card
- [ ] An approved draft uploads directly from the server and is published as a Reel; `public_asset_base_url` is no longer required
- [ ] Publish asks for an explicit confirm, because the post will be public

**Verification:**
- [ ] `python tests/test_pipeline.py`: resumable upload request shape (stubbed)
- [ ] Manual: one live Reel on the Instagram test account

**Dependencies:** T8; your Meta app (token flow, tester role)

**Files likely touched:** `shorts/publish.py`, `shorts/web/connections.py`, `shorts/web/templates/accounts.html`, `shorts/web/templates/review.html`, `tests/test_pipeline.py`

**Estimated scope:** M

---

### T11: Voice and caption defaults

**Description:** The Accounts screen's voices and caption style. Each tester sets a default voice
per platform, chosen from the account's ElevenLabs voices, with preview audio from the voices
API's `preview_url` so a preview costs no credit. They also set the level of the original sound
under the voice, and caption font, highlight colour and position. New run is prefilled with
them.

**Acceptance criteria:**
- [ ] Changing a default voice changes the prefilled voice on New run, and the run uses it
- [ ] The caption preview updates as the style changes, and a render uses the saved style
- [ ] No two platforms can be saved with the same voice (matching `pick_voices`)

**Verification:**
- [ ] `python tests/test_web.py`: settings saved and applied to the built config
- [ ] Manual: preview each voice; render one clip with a non-default highlight colour

**Dependencies:** T4

**Files likely touched:** `shorts/web/settings.py`, `shorts/voice.py`, `shorts/web/templates/accounts.html`, `shorts/web/runs.py`, `tests/test_web.py`

**Estimated scope:** M

---

### T12: Schedule mode

**Description:** Posting mode Schedule books slots with the existing `schedule.slots` into
`scheduled_posts` (per tester, per account) instead of `queue.json`. The worker publishes due
posts between runs with that tester's creds. Runs shows "N posted · N queued", and review shows
each post's time with Cancel.

**Acceptance criteria:**
- [ ] Slots never collide with that tester's existing bookings on the same account
- [ ] A due post is published within a minute of its time by the worker
- [ ] Cancelling a queued post means it never publishes

**Verification:**
- [ ] `python tests/test_web.py`: fake clock, due post published once, cancelled one skipped
- [ ] Manual: schedule a YouTube draft 5 minutes ahead; it appears private on the channel

**Dependencies:** T8

**Files likely touched:** `shorts/web/worker.py`, `shorts/web/db.py`, `shorts/web/review.py`, `shorts/web/templates/runs.html`, `tests/test_web.py`

**Estimated scope:** M

### Checkpoint 3
- [ ] All tests pass
- [ ] Private posts on YouTube, TikTok and Instagram test accounts, all from the review screen
- [ ] A scheduled post goes out on time
- [ ] Review with Ilya

---

## Phase 4: Hosting and testers

### T13: Container image and compose file

**Description:** A Dockerfile with Python, ffmpeg (with zscale for HDR), the web and whisper
extras, and the whisper model downloaded at build. A compose file runs `web`, `worker` and
`caddy`, with a `/data` volume for SQLite and runs, and a daily clean-up of runs older than 14
days. Settings come from env: keys, encryption key, OAuth clients, domain, caps.

**Acceptance criteria:**
- [ ] `docker compose up` on a clean machine serves the app and runs a real video end to end
- [ ] The HDR iPhone file renders with tone mapping inside the container (zscale present)
- [ ] Stopping and starting the stack keeps testers, tokens and runs

**Verification:**
- [ ] `docker compose run --rm web python tests/test_render_e2e.py` passes inside the image
- [ ] Manual: full flow against the local compose stack

**Dependencies:** Checkpoint 3 (T9/T10 may still be pending)

**Files likely touched:** `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, `.env.example`, `README.md`

**Estimated scope:** M

---

### T14: Deploy to the subdomain and update Terms/Privacy for a hosted service

**Description:** Provision the VPS, point the subdomain, and bring the stack up with Caddy TLS.
Register the production OAuth redirect URIs with Google, TikTok and Meta. Set up a nightly
backup of the SQLite file (tokens stay encrypted) with a tested restore. Rewrite the Privacy
Policy and Terms: today they say the app runs on the operator's computer, and they have to
describe the hosted service, where files are stored, retention (14 days) and deletion requests.

**Acceptance criteria:**
- [ ] `https://<domain>/health` returns 200 with a valid certificate
- [ ] Sign-in and each platform connect work against the production redirect URIs
- [ ] A backup restored onto a fresh volume brings back users and connections
- [ ] The published Privacy and Terms pages describe the hosted service accurately

**Verification:**
- [ ] Manual: full flow on the live site with your account (upload → review → private publish)
- [ ] Manual: restore drill on a scratch directory

**Dependencies:** T13; open questions 1–2 answered (domain, server)

**Files likely touched:** `deploy/backup.sh`, `docs/privacy.html`, `docs/terms.html`, `README.md`

**Estimated scope:** M

---

### T15: Invite the first tester

**Description:** A one-page tester guide at `/guide`: what to upload, what drafts mean, and that
Instagram posts are public. It also covers an owner checklist for each new tester: invite
command, add them as a Google test user, TikTok sandbox target user and Instagram tester. Then
invite one real tester and watch their first run's log for problems.

**Acceptance criteria:**
- [ ] The tester completes sign-in → upload → review → private publish without help
- [ ] Anything they got stuck on is either fixed or written into the guide

**Verification:**
- [ ] Manual: the tester's run log and summary are clean, with no unhandled errors

**Dependencies:** T14

**Files likely touched:** `shorts/web/templates/guide.html`, `README.md`

**Estimated scope:** S

### Checkpoint 4: ready for testers
- [ ] All acceptance criteria above met
- [ ] Open questions resolved
- [ ] Ilya approves inviting the rest

---

## Phase 5: channel automation (added 18 October 2026)

The rights rules these tasks enforce are in [plan.md](plan.md), addendum of 18 October 2026.

### T16: Find the hyped moments from the audio, not just the words

**Description:** `shorts/hype.py` takes one ffmpeg pass over the chosen audio track and returns
per-second loudness, then scores windows by loudness above the recording's own baseline, words per
second, and reaction words from the transcript. The peaks go into the planner's timeline as extra
lines, so the model sees where the room got loud while it still chooses on meaning.

**Acceptance criteria:**
- [x] A generated file with one loud section has its peak found within a second of it
- [x] A flat recording produces no peaks rather than noise
- [x] Peaks appear in the timeline the planner is given, in time order among the speech

**Verification:**
- [x] `python tests/test_pipeline.py`: loud-section file, flat file, the scoring maths (28 checks)
- [x] Manual: the engineering-talk video returns no peaks - it is machine-read at a constant level, so there is nothing to find. Still to try on a real human recording

**Dependencies:** None · **Files:** `shorts/hype.py`, `shorts/agent.py`, `shorts/run.py`, `tests/test_pipeline.py` · **Scope:** M

---

### T17: Rights record per channel

**Description:** Before a channel can be watched it carries a rights record: owned (its channel id
matches a connected YouTube account), permitted (who gave permission, when, where it is kept), or
neither. Stored with the source, shown on its card, copied into every run that comes from it, and
read by the compliance stage. Auto-publishing is refused without one — by the server, not the form.

**Acceptance criteria:**
- [x] A source with no rights record cannot be set to auto-publish, through the form or by posting straight to the endpoint
- [x] Ownership is checked against the connected YouTube account, never typed in
- [x] Each run's summary carries the rights record it ran under, and compliance names the permission or says there is none

**Verification:**
- [x] `python tests/test_web.py`: all three records, bulk add, the refusal path, the gate, cross-tester access, the credit line (15 checks)
- [x] Manual: the Channels page renders with its permission form; adding real channels is yours to do, since it records a claim in your name

**Dependencies:** T8 (YouTube connection, for ownership) · **Files:** `shorts/web/sources.py`, `shorts/web/db.py`, `shorts/compliance.py`, `tests/test_web.py` · **Scope:** M

---

### T18: Watch a channel and fetch what's new

**Description:** `shorts/sources.py` resolves a channel URL or @handle to its id, reads its RSS
feed for new uploads, and fetches one with `yt-dlp` into the run folder, behind a `[channels]`
extra. It handles the ugly parts out loud: too long, a live stream still going, members-only, and
YouTube refusing the server — back off, pause that source, say so on its card.

**Acceptance criteria:**
- [x] A channel URL, an @handle and a bare channel id all resolve to the same source
- [x] A new upload is noticed once and never started twice
- [x] A refused download raises pause-this-source with the reason (the worker side lands in T21)

**Verification:**
- [x] `python tests/test_pipeline.py`: feed parsing, dedupe, refusals, bot-check, download stubbed
- [x] Manual (feeds): @TED and @veritasium resolve to the right channel id live, 15 uploads each, dedupe correct. Downloading a video is still not done - that needs a channel you own or have permission for

**Dependencies:** T17 · **Files:** `shorts/sources.py`, `shorts/web/sources.py`, `pyproject.toml`, `tests/test_web.py` · **Scope:** M

---

### T19: Everygen: one command, start to finish

**Description:** One shortcut that takes a video URL, a channel URL or a file and does the rest from
saved defaults, with no settings screen: `python -m shorts everygen <url>`, and a single field at
the top of Runs. The defaults live per tester (clips, framing, platforms, voices, caption style,
what happens when clips are ready) and are the same ones the daily automation uses.

**Acceptance criteria:**
- [ ] One command with only a URL produces drafts using the saved defaults
- [ ] With nothing saved it uses the documented defaults and says which it used
- [ ] The same defaults drive a manual run and the daily automation

**Verification:**
- [ ] `python tests/test_web.py`: defaults applied; the config matches the equivalent manual run
- [ ] Manual: `everygen` on one video, start to finish

**Dependencies:** T18 · **Files:** `shorts/web/sources.py`, `shorts/web/settings.py`, `shorts/__main__.py`, `tests/test_web.py` · **Scope:** M

---

### T20: Script editor per clip, with re-record

**Description:** On the review card the narration script and hook become editable, with a rewrite
button asking Claude for two or three alternatives written from that clip's own transcript. Saving
queues a re-record of that platform's version only: the voice reads the new script, captions are
re-timed to it, that one file is re-rendered, and the card swaps the video when it's done.

**Acceptance criteria:**
- [ ] An edited script re-records and re-renders only that platform's version
- [ ] The captions on screen match the new script
- [ ] A script too long for the clip is flagged before it is recorded, not after

**Verification:**
- [ ] `python tests/test_web.py`: job queued, worker re-renders, file replaced (ElevenLabs stubbed)
- [ ] Manual: rewrite one hook and listen to the result

**Dependencies:** T7, T11 · **Files:** `shorts/web/review.py`, `shorts/web/worker.py`, `shorts/agent.py`, `shorts/web/templates/review.html`, `tests/test_web.py` · **Scope:** M

---

### T21: Daily schedule, several channels and several accounts at once

**Description:** Sources get a cadence (daily, or every N hours) and the worker checks the due ones
between runs. Several channels can be added at once, one URL per line, sharing a preset. On the
posting side, `connections` allows more than one account per platform, each labelled, so one clip
fans out to several accounts with spacing between them. A sources page lists them with last check,
last video, next check and a pause switch.

**Acceptance criteria:**
- [ ] Ten channels pasted at once become ten sources sharing one preset
- [ ] A daily source is checked once a day; a paused one is not checked at all
- [ ] One clip posts to three accounts on the same platform, spaced, each logged separately

**Verification:**
- [ ] `python tests/test_web.py`: fake clock over three days, bulk add, fan-out with a stubbed publisher
- [ ] Manual: two channels and two accounts for a day

**Dependencies:** T18, T8-T10 · **Files:** `shorts/web/sources.py`, `shorts/web/worker.py`, `shorts/web/connections.py`, `shorts/web/db.py`, `tests/test_web.py` · **Scope:** L - split if it grows

---

### T22: Auto-publishing with its rails

**Description:** A source with a rights record can post by itself, per platform. The rails: a clip
with any compliance flag goes to Review instead; per-account daily caps; a global pause that stops
everything mid-flight; and a digest showing what went out, with a link to each post and an undo
where the platform allows one.

**Acceptance criteria:**
- [ ] A flagged clip never auto-posts, whatever the source says
- [ ] The global pause stops posting within a minute, and nothing is lost when it resumes
- [ ] The digest shows every post of the last week with its link and its source

**Verification:**
- [ ] `python tests/test_web.py`: flagged clip held back, caps, pause, digest contents
- [ ] Manual: one overnight run from your own channel to the test accounts

**Dependencies:** T21 · **Files:** `shorts/web/worker.py`, `shorts/web/sources.py`, `shorts/web/templates/digest.html`, `tests/test_web.py` · **Scope:** L - split if it grows

### Checkpoint 5: a day on its own
- [ ] A watched channel posts overnight with nobody awake, and the digest says what went out
- [ ] A channel with no rights record never auto-posts, whatever else is set
- [ ] Review with Ilya
