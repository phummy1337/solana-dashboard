# stateofsol-cron

Fires the dashboard's data refresh on time.

## Why this exists

GitHub's `schedule` event is best-effort. Measured on this repo over six days
it delivered **2–3 of the 4 runs a day** it was asked for, and the last one
landed **2–3 hours late** every time. `repository_dispatch` is an on-demand
event and starts within seconds, so the schedule lives here and GitHub keeps a
single late cron (`37 20 * * *`) purely as a fallback for if this Worker or its
token ever stops.

## Setup (one-off)

1. **Create a fine-grained GitHub token.** Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token.
   - Repository access: **only** `phummy1337/solana-dashboard`
   - Permissions: **Contents → Read and write**. That is what the dispatch
     endpoint checks; despite triggering a workflow it is not gated on the
     Actions permission. If a dispatch comes back `403`, add **Actions → Read
     and write** as well and re-test — the manual endpoint below reports the
     status immediately, so this takes seconds to confirm either way.
   - Set an expiry you will actually notice; the fallback cron covers the gap
     if it lapses, but the dashboard drops to one refresh a day until renewed.

2. **Store it and deploy:**

   ```bash
   cd worker
   npx wrangler secret put GH_TOKEN      # paste the token when prompted
   npx wrangler deploy
   ```

3. **Set a throwaway key for the manual trigger** and deploy:

   ```bash
   npx wrangler secret put TEST_KEY     # any random string
   npx wrangler deploy
   ```

   This is deliberately *not* GH_TOKEN. The first draft reused the PAT as the
   URL key, which would have put a repo-write credential into shell history,
   proxy logs and Cloudflare's own request log.

4. **Check the wiring** without waiting for a cron:

   ```bash
   curl -X POST "https://stateofsol-cron.apxusd-supply-1337.workers.dev/?key=<TEST_KEY>"
   ```

   Expect `github: 204` — GitHub returns 204 No Content on a successful
   dispatch — and a run under Actions within seconds. Any other status is
   printed with GitHub's own error body, since a silent failure here looks
   exactly like GitHub being slow. Note secrets take a few seconds to
   propagate; a `not found` immediately after `secret put` usually just means
   retry.

## Verified

Deployed 2026-09-24. Manual dispatch returned `github: 204` and a
`repository_dispatch` run started within 20 seconds, against GitHub's own
scheduler taking 2-3 hours. A fine-grained PAT with **Contents: Read and
write** is confirmed sufficient — the Actions permission is not needed.

## Changing the schedule

Edit `crons` in `wrangler.toml` and redeploy. Leave the GitHub fallback alone —
it is deliberately at an odd minute, since the top of the hour is when GitHub's
queue is most congested.

## If refreshes stop

Check in this order:

- `npx wrangler tail` — every dispatch logs its HTTP status. A `401` means the
  token expired; `403` means its permissions were narrowed.
- Actions tab — if dispatches are arriving but builds fail, the problem is in
  `refresh_data.py`, not here.
- The site keeps serving its last good `data.json` either way; the build refuses
  to publish a file that lost series against the live one.
