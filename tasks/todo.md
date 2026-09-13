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
- [ ] Every rendered platform version plays in the browser and seeks
- [ ] Approve/undo and copy edits persist across reloads, and edited copy is what a later publish sends
- [ ] Compliance flags from the summary show on the card they belong to

**Verification:**
- [ ] `python tests/test_web.py`: approve, edit, video served with a 206 range response only to the owner
- [ ] Manual: review the real run from T5 end to end

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
- [ ] A tester connects their channel and publishes an approved draft; it appears as a private video on their channel, marked as synthetic media when narrated
- [ ] The token is unreadable in the database file without the encryption key
- [ ] A revoked or expired token shows "Reconnect" instead of a raw error

**Verification:**
- [ ] `python tests/test_web.py`: OAuth callback stored encrypted, publish uses that tester's token (Google stubbed)
- [ ] Manual: one live private upload to the test channel from the review screen

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
