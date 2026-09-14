/**
 * Fires the GitHub Actions "FCC recording mailer" workflow via its
 * workflow_dispatch API on a Cloudflare Cron Trigger. This exists because
 * GitHub's own `schedule` cron trigger was not firing reliably for that
 * repo; workflow_dispatch has been 100% reliable when called manually, so
 * this Worker just calls that same endpoint on a schedule instead.
 */

const OWNER = "anonymousfliphones";
const REPO = "freeconferencecall-recording-mailer";
const WORKFLOW_FILE = "fcc-mailer.yml";

// Desired fire times in US Eastern local time (handles the EST/EDT switch
// automatically -- see the comment above the crons array in wrangler.toml
// for why this check exists alongside the cron schedule).
const EASTERN_TARGET_TIMES = ["13:30", "14:00", "14:30"];

function currentEasternHHMM() {
  // Intl's IANA tz database knows the US DST transition dates, so this
  // stays correct across the clock change with no manual updates.
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());
  const hour = parts.find((p) => p.type === "hour").value;
  const minute = parts.find((p) => p.type === "minute").value;
  return `${hour}:${minute}`;
}

async function dispatchWorkflow(env) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "fcc-mailer-trigger-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "master" }),
  });

  if (resp.status !== 204) {
    const text = await resp.text();
    throw new Error(`GitHub dispatch failed: ${resp.status} ${text}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    // The cron fires more often (in UTC) than we actually want, to cover
    // both possible UTC offsets for Eastern time; only dispatch on the
    // ticks that land on one of our real target times.
    const hhmm = currentEasternHHMM();
    if (!EASTERN_TARGET_TIMES.includes(hhmm)) return;
    ctx.waitUntil(dispatchWorkflow(env));
  },

  // Manual test endpoint: visit the deployed Worker's URL (with the right
  // token) to trigger it on demand without waiting for the cron. Requires
  // TRIGGER_TOKEN so the public URL can't be used to spam-trigger runs.
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const authHeader = request.headers.get("Authorization") || "";
    const bearerToken = authHeader.replace(/^Bearer\s+/i, "");
    const token = bearerToken || url.searchParams.get("token") || "";
    if (!env.TRIGGER_TOKEN || token !== env.TRIGGER_TOKEN) {
      return new Response("Unauthorized\n", { status: 401 });
    }

    try {
      await dispatchWorkflow(env);
      return new Response("Dispatched fcc-mailer.yml workflow_dispatch.\n");
    } catch (err) {
      return new Response(`Error: ${err.message}\n`, { status: 500 });
    }
  },
};
