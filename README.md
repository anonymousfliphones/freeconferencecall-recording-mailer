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
6. **Email** — sends the MP3 as an attachment via `smtplib`/`email` (Gmail SMTP + App Password by default).
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
- An SMTP account to send from. Default here is Gmail: enable 2-Step
  Verification on the Google account, then generate a 16-character
  [App Password](https://myaccount.google.com/apppasswords) — this is
  *not* your normal Gmail password, and lets the script send from your real
  address to any recipient. A transactional provider (SendGrid, Mailgun,
  Resend, etc.) also works via SMTP, but most require verifying a domain you
  own before you can send to recipients other than your own account email.

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

### Cron (Linux/macOS)

```cron
*/15 * * * * cd /path/to/fcc-recording-mailer && venv/bin/python fcc_recording_mailer.py >> run.log 2>&1
```

### Windows Task Scheduler

Create a Basic Task that runs on a recurring trigger (e.g. every 15 minutes)
with:

- Program/script: `C:\path\to\fcc-recording-mailer\venv\Scripts\python.exe`
- Arguments: `fcc_recording_mailer.py`
- Start in: `C:\path\to\fcc-recording-mailer`

Or from an elevated PowerShell prompt:

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\fcc-recording-mailer\venv\Scripts\python.exe" -Argument "fcc_recording_mailer.py" -WorkingDirectory "C:\path\to\fcc-recording-mailer"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName "FCC Recording Mailer" -Action $action -Trigger $trigger
```

### GitHub Actions

See `.github/workflows/fcc-mailer.yml` — runs every 30 minutes on a schedule.
Add `FCC_EMAIL`, `FCC_PASSWORD`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` as repository secrets. The workflow
commits `downloaded_ids.json` back to the repo after each run so state
persists between runs — reasonable for a low-frequency personal job, but swap
it for a cache/artifact/external store if you'd rather not commit state to
git, or if multiple runs could race.

### Built-in loop mode

`python fcc_recording_mailer.py --loop --interval 900` runs forever in the
foreground, useful under `pm2`, `systemd`, `nssm` (Windows service wrapper),
or a plain `screen`/`tmux` session.

## Configuration reference (`.env`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FCC_EMAIL` | yes | — | FreeConferenceCall.com login email |
| `FCC_PASSWORD` | yes | — | FreeConferenceCall.com login password |
| `SMTP_HOST` | yes | — | SMTP server hostname |
| `SMTP_PORT` | no | `587` | SMTP port (STARTTLS) |
| `SMTP_USERNAME` | yes | — | SMTP auth username |
| `SMTP_PASSWORD` | yes | — | SMTP auth password / app password |
| `EMAIL_FROM` | yes | — | From address |
| `EMAIL_TO` | yes | — | Recipient address |
| `MP3_BITRATE` | no | `24k` | ffmpeg audio bitrate |
| `MP3_SAMPLE_RATE` | no | `16000` | ffmpeg sample rate (Hz) |
| `WORK_DIR` | no | `./tmp` | Scratch dir for raw/compressed files (cleaned up after each recording) |
| `TRACKING_FILE` | no | `./downloaded_ids.json` | Where processed recording IDs are recorded |
| `MAX_RECORDINGS_PER_RUN` | no | `5` | Cap per run, oldest-unprocessed-first |
| `FCC_PAGE_SIZE` | no | `100` | Conferences fetched per API page |

## Security notes

- Credentials are read only from environment variables / `.env` — never
  hardcode them.
- `.gitignore` excludes `.env`, `downloaded_ids.json`, and temp/debug files.
- Use a Gmail **App Password**, not your primary account password, for SMTP.
- The GitHub Actions example stores secrets in encrypted repo secrets, not in
  the workflow file.
