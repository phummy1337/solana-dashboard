/**
 * Cron trigger for the stateofsol.com data refresh.
 *
 * GitHub's own `schedule` event is best-effort. Measured on this repo over six
 * days it delivered 2-3 of the 4 runs it was asked for, with the last landing
 * 2-3 hours late. `repository_dispatch` is an on-demand event and starts within
 * seconds, so the schedule lives here instead and GitHub keeps one late daily
 * cron purely as a fallback.
 *
 * Secrets (wrangler secret put ...):
 *   GH_TOKEN  fine-grained PAT, repo phummy1337/solana-dashboard,
 *             Contents: Read and write (what /dispatches checks). Nothing else.
 *   TEST_KEY  a throwaway string gating the manual trigger below. Separate from
 *             GH_TOKEN on purpose: the first draft reused the PAT as the URL
 *             key, which would have put a repo-write credential into shell
 *             history, proxy logs and Cloudflare's request log.
 */

const REPO = "phummy1337/solana-dashboard";
const EVENT_TYPE = "refresh";

async function fire(env, reason) {
  const res = await fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub rejects API requests without one.
      "User-Agent": "stateofsol-cron-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      event_type: EVENT_TYPE,
      client_payload: { reason, firedAt: new Date().toISOString() },
    }),
  });
  // 204 No Content is success here; anything else is worth seeing in the logs,
  // because a silent failure would look exactly like GitHub being late again.
  const body = res.ok ? "" : await res.text();
  console.log(`dispatch ${reason}: ${res.status}${body ? ` ${body.slice(0, 200)}` : ""}`);
  return res;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(fire(env, `cron ${event.cron}`));
  },

  // Manual trigger, so the wiring can be tested without waiting for a cron:
  //   curl -X POST "https://<worker>/?key=<TEST_KEY>"
  // Returns GitHub's own status and body, which is the whole point — a silent
  // failure here is indistinguishable from GitHub being slow.
  async fetch(req, env) {
    const url = new URL(req.url);
    if (!env.TEST_KEY || url.searchParams.get("key") !== env.TEST_KEY) {
      return new Response("not found", { status: 404 });
    }
    const res = await fire(env, "manual");
    const detail = res.ok ? "" : ` ${(await res.text()).slice(0, 300)}`;
    return new Response(`github: ${res.status}${detail}\n`, {
      status: res.ok ? 200 : 502,
    });
  },
};
