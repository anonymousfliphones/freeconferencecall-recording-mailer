# FCC Mailer Trigger (Cloudflare Worker)

A tiny Cloudflare Worker whose only job is to fire this repo's own
[fcc-mailer.yml](../.github/workflows/fcc-mailer.yml) GitHub Actions
workflow on a reliable schedule. Lives at `worker/` in the
`fcc-recording-mailer` repo, deployed independently to Cloudflare (it does
not run as part of the GitHub Actions workflow itself).

## Why this exists

`fcc-recording-mailer`'s workflow originally used GitHub Actions' own
`schedule` (cron) trigger. That trigger never fired — not once — across
2+ hours, two different cron intervals, a CLI push, and a web-UI edit,
despite the workflow being fully enabled and correctly configured (ruled
out: workflow state, Actions permissions, billing/quota, account email
verification, GitHub status incidents). This looked like a platform-side
quirk specific to that repo, not a misconfiguration.

`workflow_dispatch` (the manual "Run workflow" trigger), by contrast, has
been 100% reliable every time it's been called via the API. So instead of
depending on GitHub's scheduler, this Worker calls that same reliable
`workflow_dispatch` endpoint on its own Cron Trigger — a completely
different, independent scheduling mechanism that isn't affected by
whatever's wrong with GitHub's.

This Worker does **not** do any of the actual work (login, download,
compress, email) — it only makes one `POST` request to GitHub's API. All of
that logic still runs entirely inside the GitHub Actions workflow.

## How it works

- The goal is 1:30pm, 2:00pm, and 2:30pm US Eastern time, every day.
  Cloudflare Cron Triggers only run in UTC and have no DST awareness, so
  `wrangler.toml` lists five UTC ticks — the union of both possible Eastern
  offsets (EDT: 17:30/18:00/18:30 UTC, EST: 18:30/19:00/19:30 UTC).
- `src/index.js`'s `scheduled` handler checks the actual current
  `America/New_York` wall-clock time (via `Intl.DateTimeFormat`, which
  knows the real DST transition dates) on every tick, and only dispatches
  the workflow on the ticks that are genuinely 1:30/2:00/2:30pm Eastern
  right now. The other two ticks (the "wrong offset" ones) are no-ops. This
  means the schedule automatically follows the spring/fall clock change —
  nothing here needs to be edited twice a year.
- The actual dispatch `POST`s to
  `https://api.github.com/repos/anonymousfliphones/fcc-recording-mailer/actions/workflows/fcc-mailer.yml/dispatches`
  using a `GITHUB_TOKEN` secret.
- It also exports a `fetch` handler (unaffected by the time gate above), so
  visiting the deployed Worker's URL triggers a dispatch immediately,
  regardless of time — useful for testing without waiting for the cron.
  This requires a `TRIGGER_TOKEN` secret (see below) — without it, anyone
  who finds the URL could spam-trigger your workflow.

## Setup / redeploy

Requires Node.js/npm (uses `npx wrangler`, no global install needed) and a
Cloudflare API token.

```bash
cd fcc-recording-mailer/worker

# Cloudflare API token: dash.cloudflare.com/profile/api-tokens -> Create
# Token -> "Edit Cloudflare Workers" template. Scope it to your one
# account; you don't need the "Workers Routes" (zone) permission for a
# cron-only Worker with no custom domain -- remove that row if present,
# or you'll be forced to also pick a zone.
export CLOUDFLARE_API_TOKEN=your-token-here   # PowerShell: $env:CLOUDFLARE_API_TOKEN="..."

npx wrangler deploy
```

Set the GitHub token secret (needs at minimum permission to dispatch
workflows on the target repo — the existing `gh` CLI token, if you have one
authenticated locally, already has sufficient scope):

```bash
# Windows/macOS/Linux, avoid `echo` here -- it appends a trailing newline
# that corrupts the piped secret. Use printf instead.
printf '%s' "$(gh auth token)" | npx wrangler secret put GITHUB_TOKEN
```

Also set a `TRIGGER_TOKEN` secret — a random string that guards the manual
`fetch` trigger below so the public Worker URL can't be used by anyone else
to spam-trigger your workflow:

```bash
printf '%s' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" | npx wrangler secret put TRIGGER_TOKEN
```

Test it immediately without waiting for the cron (replace `YOUR_TOKEN` with
the value you just set):

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://fcc-mailer-trigger.anonymousfliphones.workers.dev
# -> "Dispatched fcc-mailer.yml workflow_dispatch."
```

A request with no token, or the wrong one, gets `401 Unauthorized` and does
not trigger anything.

Then confirm on the GitHub side:

```bash
gh run list --repo anonymousfliphones/fcc-recording-mailer --limit 3
```

## Changing the target repo/workflow

Edit the `OWNER`, `REPO`, and `WORKFLOW_FILE` constants at the top of
`src/index.js`, then `npx wrangler deploy` again.

## Changing the schedule

Edit `EASTERN_TARGET_TIMES` in `src/index.js` (the actual Eastern-time fire
times) — and if you change how many times per day or shift them by more
than 30 minutes, also update the `crons` array in `wrangler.toml` so it
still covers the UTC tick(s) for both EDT and EST at each new target time.
Redeploy after either change. Keep in mind the downstream GitHub Actions
workflow has its own billing math (each run costs GitHub Actions minutes)
— see the main repo's README before making this more frequent.

## Caution: don't also re-enable GitHub's native `schedule` trigger

If you add a `schedule:` block back to `fcc-mailer.yml` while this Worker
is also active, you'll get two independent triggers firing close to each
other, which can race (this already caused one duplicate email during
initial testing — see the `concurrency` block in the workflow, which
guards against the *overlapping-run* half of that problem, but doesn't
prevent two separate triggers from firing in the first place).
