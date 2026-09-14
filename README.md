# FCC Recording Mailer

Logs into FreeConferenceCall.com, finds recordings you haven't processed yet,
downloads them, compresses to a small mono voice-quality MP3, and emails the
MP3 as a direct attachment. Keeps `downloaded_ids.json` so nothing is ever
downloaded or emailed twice.

## How it works

This talks directly to FreeConferenceCall.com's internal web API (the same
JSON API their own `online.freeconferencecall.com` / history dashboard uses)
with a plain `requests` session — no browser automation needed at runtime.

1. **Login** — `POST /login` with your credentials plus the page's CSRF
   `authenticity_token`, the same form-post flow the site's own login page
   uses. The resulting session cookie authenticates every later call.
2. **List recordings** — `GET /v2/conferences?has_recordings=true&...`,
   paginated. Each entry includes a stable recording ID (`conf_rec_id`),
   timestamps, duration, and file size.
3. **Resolve download URL** — `GET /v2/subscriptions/{id}/conferences/{id}/recordings`
   returns the actual downloadable audio file URL.
4. **Download** — streamed straight to disk with the same session.
5. **Compress** — shells out to `ffmpeg`: mono, 16 kHz, 24 kbps MP3 (tunable),
   which keeps a typical hour-long call well under 15 MB.
6. **Email** — sends the MP3 as an attachment via `smtplib`/`email` through
   Gmail's SMTP server with an App Password (any SMTP server works — the
   code is provider-agnostic — but see [Prerequisites](#prerequisites) for
   why Gmail is the recommended choice). `EMAIL_TO` accepts multiple
   comma-separated addresses. The attachment filename and subject are named
   by the call's own date/time (UTC) — e.g. `recording-2026-08-28_15-45.mp3`
   — not the FCC reference number.
7. **Track** — only after a successful send, the recording's ID is written to
   `downloaded_ids.json` and the local raw/MP3 files are deleted.

### About this API

This is **not** a documented, published developer API — FreeConferenceCall.com
doesn't offer one. These are the same internal endpoints their own dashboard
calls, found on 2026-08-28 by inspecting network traffic from a real,
authenticated browser session on this account. That means:

- It can change or break without notice.
- Endpoints/params live as constants near the top of
  `fcc_recording_mailer.py` (`BASE_URL`, and the URLs used in `login()`,
  `list_recordings()`, `resolve_download_url()`) — if something breaks, open
  DevTools → Network tab (filter to Fetch/XHR) on
  `https://www.freeconferencecall.com/profile/history` while logged in, and
  update the matching call.
- Automating your own account this way may or may not be covered by FCC's
  Terms of Service — this script only accesses data belonging to the account
  whose credentials you provide; review FCC's ToS if you plan to run it
  unattended or at scale.

## Prerequisites

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/download.html) installed and on your `PATH`
  - Windows: `winget install ffmpeg` (or download a static build and add its
    `bin` folder to `PATH`)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt-get install ffmpeg`
- A FreeConferenceCall.com account with recordings enabled
- A Gmail account to send from. Enable 2-Step Verification on it, then
  generate a 16-character [App Password](https://myaccount.google.com/apppasswords)
  (*not* your normal Gmail password). Set `SMTP_HOST=smtp.gmail.com`,
  `SMTP_PORT=587`, and `SMTP_USERNAME`/`EMAIL_FROM` to the Gmail address.
  Limits are 500 emails/day and 25 MB per attachment, which this job never
  approaches.

  **Why Gmail rather than a transactional email service** (Mailjet, Brevo,
  SendGrid, etc.): the code is plain `smtplib` and will talk to any SMTP
  server, but those services are built for domain-verified businesses
  sending receipts and alerts. A personal automation that sends multi-MB
  audio attachments from a free-mail address doesn't fit their abuse
  heuristics, and free-tier accounts doing this tend to get suspended —
  sometimes repeatedly, since a replacement account looks like ban evasion.
  Gmail sending its own mail through its own servers has none of that: no
  compliance review, no sender/domain verification, no relay in the middle.
  If you do use a third-party service, `EMAIL_FROM` must be on a domain you
  control and have verified with them (SPF/DKIM), or it reads as spoofing.

  **Deliverability:** a brand-new sending account has no reputation, so the
  first emails may land in recipients' spam. Since the recipient list is
  fixed and small, fix it at the receiving end: in each recipient's Gmail,
  add a filter (Settings → Filters → From: *your sending address* → "Never
  send it to Spam"), add the sender as a contact, and mark any early ones
  "Not spam". After that it's permanent.

## Setup

```bash
cd fcc-recording-mailer
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env   # Windows: copy .env.example .env
# then edit .env with your real credentials
```

`.env` is loaded automatically (via `python-dotenv`) — never commit it.

## Usage

```bash
# One-off run
python fcc_recording_mailer.py

# See what would be processed without downloading/emailing anything
python fcc_recording_mailer.py --dry-run

# Run forever, checking every 15 minutes
python fcc_recording_mailer.py --loop --interval 900
```

## If login or the API calls fail

If FreeConferenceCall.com changes its login form or internal API, you'll see
a `LoginError` or an HTTP error. To fix:

1. Log into `https://www.freeconferencecall.com` in a normal browser.
2. Open DevTools → Network tab, filter to Fetch/XHR.
3. Visit `https://www.freeconferencecall.com/profile/history` and open a
   recording; watch for calls to `/v2/conferences`,
   `/v2/subscriptions/.../conferences/.../recordings`, and the `/login` form
   POST.
4. Update the corresponding URL/params/field names in `login()`,
   `list_recordings()`, or `resolve_download_url()` in
   `fcc_recording_mailer.py`.

## Scheduling

### Current live setup: GitHub Actions, triggered by a Cloudflare Worker

The workflow at `.github/workflows/fcc-mailer.yml` runs the script in the
cloud (Ubuntu runner, real `ffmpeg` via `apt-get`) — no computer or browser
needs to be on. It only has a `workflow_dispatch` trigger, no native
`schedule` trigger, because of the finding below.

**Why not GitHub's own `schedule` (cron) trigger?** We tried it first — set
to hourly, then every 5 minutes — and it never fired a single time across
2+ hours, a CLI push, and a web-UI edit, despite the workflow being fully
enabled and correctly configured. This looked like a platform-side quirk
specific to this repo rather than a config mistake (ruled out: workflow
state, Actions permissions, billing/quota, account email verification, and
GitHub status incidents). `workflow_dispatch`, by contrast, has been 100%
reliable every time it's been called.

So scheduling is now handled by a **separate Cloudflare Worker**
(`fcc-mailer-trigger`, in this repo's `worker/` directory)
with its own Cron Trigger that fires hourly and calls GitHub's
`workflow_dispatch` API — the same reliable trigger, just invoked
externally instead of by GitHub's own (unreliable, for this repo) scheduler.
See [`worker/README.md`](worker/README.md) for setup/redeploy instructions.

A `concurrency` guard (`group: fcc-mailer`) is set on the workflow so two
overlapping runs can't race each other — this is what caused a real
duplicate email on 2026-08-28, when a manual test run landed ~24 seconds
from the Worker's first automatic firing and both processed the same
recording before either had saved the updated tracking file.

Required repository secrets: `FCC_EMAIL`, `FCC_PASSWORD`, `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`. The
workflow commits `downloaded_ids.json` back to the repo after each run so
tracking state persists between runs (it is **not** gitignored — that's
intentional; it's the state store).

**Manual test runs** — trigger the workflow by hand (GitHub UI: Actions →
FCC recording mailer → Run workflow, or `gh workflow run fcc-mailer.yml`)
with the optional `test_email_override` input set to your own address. This
sends to that address only instead of the real `EMAIL_TO` recipients — use
it to verify SMTP changes (new provider, new credentials, etc.) without
spamming live recipients. Note the Cloudflare Worker's own trigger (the
`fetch`/`scheduled` handlers) always dispatches with no override, so hitting
the Worker's URL directly exercises the real production path.

If you need to force a specific recording to be reprocessed for testing
(e.g. to confirm a fix actually delivers), remove its entry from
`downloaded_ids.json`, commit, push, then trigger a run — the recording
will be re-downloaded, re-compressed, and re-sent as if new.

### Other ways to run it

These all work too, if you'd rather not use GitHub Actions + Cloudflare:

**Cron (Linux/macOS)**

```cron
*/15 * * * * cd /path/to/fcc-recording-mailer && venv/bin/python fcc_recording_mailer.py >> run.log 2>&1
```

**Windows Task Scheduler** — Basic Task on a recurring trigger:

- Program/script: `C:\path\to\fcc-recording-mailer\venv\Scripts\python.exe`
- Arguments: `fcc_recording_mailer.py`
- Start in: `C:\path\to\fcc-recording-mailer`

Or from an elevated PowerShell prompt:

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\fcc-recording-mailer\venv\Scripts\python.exe" -Argument "fcc_recording_mailer.py" -WorkingDirectory "C:\path\to\fcc-recording-mailer"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName "FCC Recording Mailer" -Action $action -Trigger $trigger
```

**Built-in loop mode** — `python fcc_recording_mailer.py --loop --interval 900`
runs forever in the foreground, useful under `pm2`, `systemd`, `nssm`
(Windows service wrapper), or a plain `screen`/`tmux` session.

## Configuration reference (`.env`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FCC_EMAIL` | yes | — | FreeConferenceCall.com login email |
| `FCC_PASSWORD` | yes | — | FreeConferenceCall.com login password |
| `SMTP_HOST` | yes | — | SMTP server hostname |
| `SMTP_PORT` | no | `587` | SMTP port (STARTTLS) |
| `SMTP_USERNAME` | yes | — | SMTP auth username |
| `SMTP_PASSWORD` | yes | — | SMTP auth password / app password |
| `EMAIL_FROM` | yes | — | From address — the Gmail address you're sending as (same as `SMTP_USERNAME`) |
| `EMAIL_TO` | yes | — | Recipient address(es) — comma-separated for multiple |
| `EMAIL_TO_OVERRIDE` | no | — | If set, overrides `EMAIL_TO` for this run only — for manual test runs (see [Scheduling](#scheduling)) |
| `MP3_BITRATE` | no | `24k` | ffmpeg audio bitrate |
| `MP3_SAMPLE_RATE` | no | `16000` | ffmpeg sample rate (Hz) |
| `WORK_DIR` | no | `./tmp` | Scratch dir for raw/compressed files (cleaned up after each recording) |
| `TRACKING_FILE` | no | `./downloaded_ids.json` | Where processed recording IDs are recorded |
| `MAX_RECORDINGS_PER_RUN` | no | `5` | Cap per run, oldest-unprocessed-first |
| `FCC_PAGE_SIZE` | no | `100` | Conferences fetched per API page |

## Security notes

- Credentials are read only from environment variables / `.env` — never
  hardcode them.
- `.gitignore` excludes `.env` and temp/debug files. `downloaded_ids.json`
  is deliberately **not** gitignored when running via GitHub Actions — the
  workflow commits it back to the repo as its persistent state store.
- Use a Gmail **App Password**, not your primary account password, for SMTP.
  Use a dedicated Gmail account for sending rather than your main one, so
  the App Password only ever grants access to a mailbox that holds nothing
  else.
- The GitHub Actions example stores secrets in encrypted repo secrets, not in
  the workflow file.
- The Cloudflare Worker's `GITHUB_TOKEN` secret only needs permission to
  dispatch this one workflow — don't use a broad personal access token for
  it if you set this up fresh.

## License

FCC Recording Mailer is free to use, copy, modify, and share for
**noncommercial purposes** — personal use, families, schools, synagogues,
charities, and the like — under the **GPL v3 with a Noncommercial
Restriction**. If you distribute a modified version, you must publish its
source under the same terms.

Using or distributing it commercially (for example, bundling it with a paid
service or selling a product built on it) requires permission — open an
issue on this repo to ask.

See [`LICENSE`](LICENSE) (GPL v3) and
[`LICENSE-ADDITIONAL-TERMS`](LICENSE-ADDITIONAL-TERMS) (the noncommercial
restriction). Because of that restriction this is *source-available*, not
OSI open source.

Required notice: Copyright (c) 2026 anonymousfliphones
(https://github.com/anonymousfliphones/fcc-recording-mailer)
