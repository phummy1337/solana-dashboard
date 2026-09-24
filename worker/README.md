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

3. **Check the wiring** without waiting for a cron:

   ```bash
   curl -X POST "https://stateofsol-cron.<your-subdomain>.workers.dev/?key=<GH_TOKEN>"
   ```

   Expect `dispatched`, then a run appearing under Actions within seconds. The
   endpoint is gated on the same secret rather than left open, because it
   starts a build.

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
