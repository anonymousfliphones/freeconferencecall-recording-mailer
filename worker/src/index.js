/**
 * Fires the GitHub Actions "FCC recording mailer" workflow via its
 * workflow_dispatch API on a Cloudflare Cron Trigger. This exists because
 * GitHub's own `schedule` cron trigger was not firing reliably for that
 * repo; workflow_dispatch has been 100% reliable when called manually, so
 * this Worker just calls that same endpoint on a schedule instead.
 */

const OWNER = "anonymousfliphones";
const REPO = "fcc-recording-mailer";
const WORKFLOW_FILE = "fcc-mailer.yml";

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
    ctx.waitUntil(dispatchWorkflow(env));
  },

  // Manual test endpoint: visit the deployed Worker's URL to trigger it
  // on demand without waiting for the cron.
  async fetch(request, env, ctx) {
    try {
      await dispatchWorkflow(env);
      return new Response("Dispatched fcc-mailer.yml workflow_dispatch.\n");
    } catch (err) {
      return new Response(`Error: ${err.message}\n`, { status: 500 });
    }
  },
};
