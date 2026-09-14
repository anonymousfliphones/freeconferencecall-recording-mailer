"""
FreeConferenceCall.com recording -> compressed MP3 -> email attachment.

Logs into FreeConferenceCall.com's internal web API (the same one their own
online.freeconferencecall.com dashboard uses) with a plain `requests`
session, finds recordings that haven't been processed yet, downloads the raw
audio, compresses it with ffmpeg into a small mono voice-quality MP3, emails
it as an attachment, and records the recording's ID so it is never re-sent.

IMPORTANT
-----------------------------------------------------------------------
This is FreeConferenceCall.com's *internal* API, not a documented/public
developer API with a stable contract. It was reverse-engineered on
2026-08-28 by inspecting network traffic from an authenticated browser
session on your own account. It can change without notice. If something
breaks, re-inspect the calls the site itself makes (browser DevTools ->
Network tab, filtered to XHR/Fetch) while logged into
https://www.freeconferencecall.com/profile/history and adjust the
constants/endpoints below.
-----------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

try:
    # Makes Python's SSL verification use the OS certificate store (Windows
    # Certificate Store / macOS Keychain) instead of only the certifi bundle.
    # Needed on machines where something (antivirus HTTPS scanning, a
    # corporate proxy, etc.) intercepts TLS with a locally-trusted root cert
    # that certifi doesn't know about.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fcc_recording_mailer")

BASE_URL = "https://www.freeconferencecall.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class Config:
    fcc_email: str
    fcc_password: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str

    mp3_bitrate: str
    mp3_sample_rate: int

    work_dir: Path
    tracking_file: Path
    max_recordings_per_run: int
    page_size: int

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "FCC_EMAIL": os.getenv("FCC_EMAIL"),
            "FCC_PASSWORD": os.getenv("FCC_PASSWORD"),
            "SMTP_HOST": os.getenv("SMTP_HOST"),
            "SMTP_USERNAME": os.getenv("SMTP_USERNAME"),
            "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD"),
            "EMAIL_FROM": os.getenv("EMAIL_FROM"),
            "EMAIL_TO": os.getenv("EMAIL_TO"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise SystemExit(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + "\nSee .env.example for the full list."
            )

        work_dir = Path(os.getenv("WORK_DIR", "./tmp")).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        email_to_override = os.getenv("EMAIL_TO_OVERRIDE")
        if email_to_override:
            log.warning(
                "EMAIL_TO_OVERRIDE is set — sending to %s instead of the "
                "configured EMAIL_TO. This is meant for manual test runs only.",
                email_to_override,
            )

        return cls(
            fcc_email=required["FCC_EMAIL"],
            fcc_password=required["FCC_PASSWORD"],
            smtp_host=required["SMTP_HOST"],
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=required["SMTP_USERNAME"],
            smtp_password=required["SMTP_PASSWORD"],
            email_from=required["EMAIL_FROM"],
            email_to=email_to_override or required["EMAIL_TO"],
            mp3_bitrate=os.getenv("MP3_BITRATE", "24k"),
            mp3_sample_rate=int(os.getenv("MP3_SAMPLE_RATE", "16000")),
            work_dir=work_dir,
            tracking_file=Path(os.getenv("TRACKING_FILE", "./downloaded_ids.json")).resolve(),
            max_recordings_per_run=int(os.getenv("MAX_RECORDINGS_PER_RUN", "5")),
            page_size=int(os.getenv("FCC_PAGE_SIZE", "100")),
        )


class LoginError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tracking (avoid re-downloading / re-emailing the same recording)
# --------------------------------------------------------------------------

def load_tracking(path: Path) -> dict[str, Any]:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tracking(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)  # atomic on both POSIX and Windows


# --------------------------------------------------------------------------
# FreeConferenceCall.com API client
# --------------------------------------------------------------------------

def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def login(session: requests.Session, cfg: Config) -> None:
    """Log in via FCC's classic Rails-style form POST (CSRF token + cookies)."""
    log.info("Fetching login page for CSRF token...")
    resp = session.get(f"{BASE_URL}/login", timeout=30)
    resp.raise_for_status()

    match = re.search(
        r'name=["\']authenticity_token["\']\s+value=["\']([^"\']+)["\']', resp.text
    )
    if not match:
        # Some renders put value before name.
        match = re.search(
            r'value=["\']([^"\']+)["\']\s+name=["\']authenticity_token["\']', resp.text
        )
    if not match:
        raise LoginError(
            "Could not find authenticity_token on the login page. "
            "FreeConferenceCall.com's login form markup may have changed — "
            "inspect https://www.freeconferencecall.com/login and update login()."
        )
    authenticity_token = match.group(1)

    log.info("Submitting login form...")
    resp = session.post(
        f"{BASE_URL}/login",
        data={
            "authenticity_token": authenticity_token,
            "redirect_uri": "",
            "email": cfg.fcc_email,
            "password": cfg.fcc_password,
            "rememberme": "true",
        },
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()

    # Verify by hitting an authenticated endpoint rather than trusting the
    # login response body (which may just be a redirect landing page).
    check = session.get(
        f"{BASE_URL}/v2/conferences",
        params={"limit": 1, "offset": 0, "order": "desc", "order_by": "start_time"},
        timeout=30,
    )
    if check.status_code != 200 or "conferences" not in check.text:
        raise LoginError(
            "Login POST completed but the account API is not returning data — "
            "credentials may be wrong, or FCC's login flow has changed."
        )
    log.info("Login successful.")


def list_recordings(session: requests.Session, cfg: Config) -> list[dict[str, Any]]:
    """Return all recorded conferences, oldest-processed-first is handled by caller."""
    recordings: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = session.get(
            f"{BASE_URL}/v2/conferences",
            params={
                "abandoned_calls": "false",
                "has_recordings": "true",
                "limit": cfg.page_size,
                "offset": offset,
                "order": "desc",
                "order_by": "start_time",
            },
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json().get("conferences", [])
        recordings.extend(page)
        if len(page) < cfg.page_size:
            break
        offset += cfg.page_size

    # Defensive filtering in case has_recordings=true ever loosens.
    recordings = [
        r for r in recordings
        if r.get("is_recorded") and r.get("audio") and not r.get("deleted")
    ]
    log.info("Found %d recorded conference(s) on the account.", len(recordings))
    return recordings


def resolve_download_url(session: requests.Session, recording: dict[str, Any]) -> str:
    """Look up the authoritative download URL for one recording."""
    subscription_id = recording["subscription_id"]
    conference_id = recording["id"]
    resp = session.get(
        f"{BASE_URL}/v2/subscriptions/{subscription_id}/conferences/{conference_id}/recordings",
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    for entry in data.get("audio", []):
        if entry.get("ready_to_download") and entry.get("download_path"):
            return entry["download_path"]

    # Fall back to the URL already present on the conference record.
    if recording.get("recording_url"):
        return recording["recording_url"] + ".mp3"

    raise RuntimeError(f"No downloadable audio found for conference {conference_id}")


def download_recording(session: requests.Session, url: str, dest_path: Path) -> None:
    with session.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    log.info("Downloaded raw audio to %s (%d bytes)", dest_path, dest_path.stat().st_size)


# --------------------------------------------------------------------------
# Audio compression (ffmpeg)
# --------------------------------------------------------------------------

def compress_audio(input_path: Path, output_path: Path, bitrate: str, sample_rate: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        str(output_path),
    ]
    log.info("Compressing audio: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}):\n{result.stderr}")
    size_mb = output_path.stat().st_size / (1024 * 1024)
    log.info("Compressed file size: %.2f MB", size_mb)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(
    cfg: Config, subject: str, body: str, attachment_path: Path, attachment_filename: str
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.email_from
    msg["To"] = cfg.email_to
    msg.set_content(body)

    with open(attachment_path, "rb") as f:
        data = f.read()
    msg.add_attachment(
        data, maintype="audio", subtype="mpeg", filename=attachment_filename
    )

    log.info("Sending email to %s via %s:%d", cfg.email_to, cfg.smtp_host, cfg.smtp_port)
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as server:
        server.starttls()
        server.login(cfg.smtp_username, cfg.smtp_password)
        server.send_message(msg)
    log.info("Email sent.")


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------

def run_once(cfg: Config, dry_run: bool) -> None:
    tracking = load_tracking(cfg.tracking_file)

    session = new_session()
    login(session, cfg)
    recordings = list_recordings(session, cfg)

    new_recordings = [r for r in recordings if str(r["conf_rec_id"]) not in tracking]
    # Oldest first, so a run that gets interrupted resumes in order.
    new_recordings.sort(key=lambda r: r.get("start_time", 0))
    log.info("%d new recording(s) found (of %d total).", len(new_recordings), len(recordings))

    if dry_run:
        for r in new_recordings:
            log.info(
                "[dry-run] would process: conf_rec_id=%s ref=%s start_time=%s duration=%ss size=%s bytes",
                r["conf_rec_id"], r.get("reference_number"), r.get("start_time"),
                r.get("recording_duration"), r.get("file_size"),
            )
        return

    new_recordings = new_recordings[: cfg.max_recordings_per_run]

    for rec in new_recordings:
        rec_id = str(rec["conf_rec_id"])
        call_date = datetime.fromtimestamp(rec.get("start_time", 0), tz=timezone.utc)
        date_str = call_date.strftime("%Y-%m-%d_%H-%M")
        label = f"Recording {date_str} UTC"
        attachment_filename = f"recording-{date_str}.mp3"
        raw_path = cfg.work_dir / f"{rec_id}_raw"
        mp3_path = cfg.work_dir / f"{rec_id}.mp3"
        try:
            log.info("Processing recording %s (%s)", rec_id, label)
            download_url = resolve_download_url(session, rec)
            download_recording(session, download_url, raw_path)
            compress_audio(raw_path, mp3_path, cfg.mp3_bitrate, cfg.mp3_sample_rate)
            send_email(
                cfg,
                subject=f"FreeConferenceCall recording: {label}",
                body=(
                    f"Attached: {label}\n"
                    f"Recording ID: {rec_id}\n"
                    f"Reference number: {rec.get('reference_number') or 'n/a'}\n"
                    f"Callers: {rec.get('callers')}"
                ),
                attachment_path=mp3_path,
                attachment_filename=attachment_filename,
            )
            tracking[rec_id] = {
                "label": label,
                "start_time": rec.get("start_time"),
                "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            save_tracking(cfg.tracking_file, tracking)
            log.info("Recording %s processed and marked as done.", rec_id)
        except Exception:
            log.exception("Failed to process recording %s; will retry next run.", rec_id)
        finally:
            for p in (raw_path, mp3_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    log.warning("Could not delete temp file %s", p)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop", action="store_true", help="Run continuously instead of once."
    )
    parser.add_argument(
        "--interval", type=int, default=900,
        help="Seconds between checks when --loop is used (default: 900 = 15 min).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List new recordings that would be processed, without downloading/emailing.",
    )
    args = parser.parse_args()

    cfg = Config.from_env()

    if args.loop:
        log.info("Starting loop mode, checking every %d seconds. Ctrl+C to stop.", args.interval)
        while True:
            try:
                run_once(cfg, dry_run=args.dry_run)
            except Exception:
                log.exception("Run failed; will retry after the interval.")
            time.sleep(args.interval)
    else:
        run_once(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
