# Address capture: deployment runbook

How to take the address-capture system (the tokenised page at
sentrada.io/for/<token>, its API, and the runner's tease commands) from
built-and-tested to live. Written for the sender, who is not a coder, working
with a Claude session. Any Claude session with this repo can follow it: the
sender does the account clicks (only they can), the session runs every command
and verifies every step.

Status when this was written: everything is merged to `main` and passes
`capture-probe` (22 assertions) plus a simulated-phone walkthrough, but only
against local mocks. Nothing below has run on real Vercel/Upstash/Resend yet.
The first session to complete this runbook should note the date and any
surprises at the bottom.

## Where the site is now (read this first)

sentrada.io CURRENTLY serves from **GitHub Pages**, deploying the static sales
site from the `claude/build-sentrada-sales-page-tMMpY` branch. The repo
`joetrajectory/sentrada-one-pager` is **public**. GitHub Pages cannot run the
`/api` serverless functions, so the site moves to **Vercel**, which serves the
same static files plus the functions with no build step, from `main`.

Two facts drive the order below:
- The public repo holds recipient PII (see "The public-repo data question"),
  so this deployment ALSO flips the repo private. On a free GitHub plan,
  making a repo private disables its GitHub Pages site.
- Therefore **sentrada.io goes down the moment the repo is flipped private,
  and stays down until the Namecheap DNS switch to Vercel has propagated.**
  That is a deliberate, expected window (typically minutes, up to about an
  hour for DNS), not a fault. Do the whole sitting in one go; do not stop
  half-way and leave the site dark longer than necessary.

## Order of operations for the deployment sitting

Do these in exactly this order. Details for each are in the blocks below.

1. **Block A** — allow network access in the Claude environment (no site
   impact; can be done in advance).
2. **Flip the repo PRIVATE** (Block P). ← sentrada.io goes DOWN here.
3. **Default branch -> `main`** (GitHub settings).
4. **Vercel**: import, deploy, attach Upstash storage, set a FRESH
   `RUNNER_SECRET`, redeploy (Block B).
5. **Dress rehearsal** on the `.vercel.app` URL while the domain is dark
   (Block C). The public site is already down; this proves the new one works
   before pointing the domain at it.
6. **Email** notification (Block D, optional).
7. **DNS switch** at Namecheap to Vercel (Block E). ← sentrada.io comes back
   UP once this propagates, now serving from `main` via Vercel.
8. **Verify** sentrada.io is back and serving the new deployment (final
   validation), and that GitHub Pages is off (the private flip already
   disabled it; confirm).

Throughout the window between steps 2 and 7, sentrada.io returns an error /
404. That is expected. Nobody needs to panic.

---

## Block A — Environment network access (sender, 2 minutes)

The Claude environment's network policy must allow the runner and the session
to reach the deployment. In the Claude Code environment settings for this
project, allow: `sentrada.io`, `vercel.com`, `api.vercel.com`, `*.vercel.app`,
`api.resend.com` (or set full network access).

Verify (session): `curl -s -o /dev/null -w "%{http_code}" https://vercel.com`
returns an HTTP code, not 000. Until this passes, tease/capture/address/
delivered cannot work from sessions, and nothing else in this runbook can be
verified.

## Block P — Flip the repo private (sender, 1 minute) — SITE GOES DOWN HERE

GitHub: repo Settings > General > Danger Zone > Change repository visibility >
Make private. This immediately (a) stops the public exposure of the committed
recipient PII and (b) on a free plan, disables the GitHub Pages site, so
sentrada.io starts returning an error. That downtime is expected and lasts
until Block E's DNS switch propagates. Vercel and this Claude session both
keep access to the repo once private (their GitHub App installations carry
over); if Vercel's import later cannot see the repo, grant its GitHub App
access to the now-private repo in GitHub > Settings > Applications.

## Block A — Environment network access (sender, 2 minutes)

The Claude environment's network policy must allow the runner and the session
to reach the deployment. In the Claude Code environment settings for this
project, allow: `sentrada.io`, `vercel.com`, `api.vercel.com`, `*.vercel.app`,
`api.resend.com` (or set full network access).

Verify (session): `curl -s -o /dev/null -w "%{http_code}" https://vercel.com`
returns an HTTP code, not 000. Until this passes, tease/capture/address/
delivered cannot work from sessions, and nothing else in this runbook can be
verified.

## Block B — Vercel project (sender, ~10 minutes, all clicking)

(By now the repo is private, per Block P, and sentrada.io is down. That is
expected; the site returns at Block E.)

1. GitHub: repo Settings > General > Default branch > change to `main` (if not
   already done as step 3 of the sitting).
2. vercel.com > Continue with GitHub (the `joetrajectory` account).
3. Add New > Project > import `sentrada-one-pager` > Deploy. Change no
   settings (framework "Other", no build command). If the production branch
   does not show `main`, set it: Project Settings > Git > Production Branch.
   (If the import cannot see the now-private repo, grant the Vercel GitHub App
   access to it under GitHub > Settings > Applications.)
4. Storage tab > Create Database > Upstash for Redis (free tier, EU region) >
   connect it to the project. This injects `KV_REST_API_URL` and
   `KV_REST_API_TOKEN` automatically. (If the integration injects
   `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` instead, that also
   works; the functions read both names.)
5. Generate a FRESH `RUNNER_SECRET` now, at deployment time. Do not reuse any
   value that has appeared in a chat conversation, document or message; treat
   any secret that was ever pasted into a conversation as burned. The session
   generates it:

       python3 -c "import secrets; print(secrets.token_urlsafe(32))"

   The sender puts it in Project Settings > Environment Variables as
   `RUNNER_SECRET` (all environments is fine), and nowhere else except
   `.env` locally / the session env as `SENTRADA_RUNNER_SECRET` (step B7).
   `.env` is gitignored; the secret must never be committed.
6. Deployments tab > latest deployment > ... menu > Redeploy (env vars only
   apply to deployments made after they are set).
7. Session side: put the same value in `.env` as
   `SENTRADA_RUNNER_SECRET=<value>` for this session, and note that fresh
   containers need it again (or add it to the environment's env vars in the
   same settings screen as Block A, which persists across sessions).
8. Sender tells the session the project URL (like
   `sentrada-one-pager-xxxx.vercel.app`).

## Block C — Dress rehearsal on the .vercel.app URL (session, 10 minutes)

Run before the DNS change, while sentrada.io is dark. Proving the new
deployment on its `.vercel.app` URL first means the domain is only pointed at
Vercel once the flow is known to work. The session drives; the sender
optionally opens one link on their phone.

    export SENTRADA_CAPTURE_API=https://<project>.vercel.app
    python runner/sentrada_runner.py printed --piece test-pilot --name "Testa Fakerson"
    python runner/sentrada_runner.py tease --piece test-pilot --channel linkedin --variant mystery

Expected: a one-off link prints. Then verify, in order:

1. Open the link (session via headless browser, sender via phone): greeting
   "Testa.", the one-copy line, My office / Somewhere else, address fields,
   privacy line, Send it. Submit a made-up address.
2. Confirmation state appears; revisiting the link shows the confirmation,
   not the form.
3. `python runner/sentrada_runner.py capture` shows `test-pilot
   address-received` and a share rate of 1/1.
4. `python runner/sentrada_runner.py address --piece test-pilot` prints the
   made-up address to the terminal.
5. `python runner/sentrada_runner.py delivered --piece test-pilot` reports the
   store record deleted; re-running the `address` command now fails with a
   404. That failure is the deletion promise, observed.
6. `https://<project>.vercel.app/for/AAAAAAAAAAAAAAAAAAAAAA` shows "This
   one's expired." with the write-to-me email.
7. `curl -sI https://<project>.vercel.app/for/x | grep -i x-robots-tag`
   shows `noindex, nofollow`.
8. Remove the `test-pilot` entry from `runner/capture.json` (delete the JSON
   key) before the first real tease, so the pilot's share-rate stats start
   clean.

If any step fails, stop and fix before Block D/E; `capture-probe` still
passing locally means the divergence is real-infrastructure behaviour, which
is exactly what this block exists to catch.

Platform checks a cold-eyes review flagged as real divergence risks between
the local mock and live Vercel/Upstash. Run through these once during Block C:

- **The token link (highest risk).** `curl -sI https://<project>.vercel.app/for/AAAAAAAAAAAAAAAAAAAAAA`
  must return `200`, not `308` or `404`. A 308 or 404 means the `/for/:token`
  rewrite is fighting `cleanUrls` and every real tease link would fail. The
  fix (rewrite destination `/for`, no `.html`) is already in vercel.json and
  `capture-probe` now guards it, but confirm it on the real platform. If it
  regresses, the symptom is recipients seeing "This one's expired."
- **Deployment Protection must be OFF for the production domain.** Vercel >
  Project Settings > Deployment Protection. If a password/SSO wall or Attack
  Challenge covers production, recipients hit an auth page instead of the
  form, and the runner's Python client gets 401/403 HTML (which now surfaces
  as a clear "non-JSON reply" error rather than a crash). Verify with one real
  `capture` call.
- **Confirm the four store env var names exist** after attaching Upstash:
  `KV_REST_API_URL`, `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL` /
  `_TOKEN`). If a custom env prefix was chosen in the connect dialog, neither
  name exists and every request 500s with "Redis store is not configured".
- **Node version >= 18** (Project Settings > Node.js Version). New projects
  default to 22.x; anything >= 18 has the `fetch`/`crypto` the functions use.
- **One sanity curl:** `curl https://<project>.vercel.app/api/token?t=x` must
  return JSON (`{"state":"expired"}`), not JavaScript source.
- **After the first `tease`,** open the Upstash data browser and confirm the
  `tok:<token>` key shows a TTL (about 90 days). That confirms the atomic
  `SET ... EX` wire format landed as intended on real Upstash.

## Block D — Email notification (sender, 5 minutes; optional but recommended)

Without this, submissions are still safe: `capture` syncs them from the
store. This adds the same-day email ping.

1. Sign up at resend.com. Either sign up with the exact inbox that should
   receive the pings (the free `onboarding@resend.dev` sender reliably
   delivers only to the Resend account's own address), or, better, verify the
   `sentrada.io` domain: Resend > Domains > Add domain > add the three or
   four DNS records it lists at Namecheap (Advanced DNS) > wait for verified.
2. Resend > API Keys > Create > copy the key.
3. Vercel env vars: add `RESEND_API_KEY` (the key) and `NOTIFY_EMAIL` (the
   inbox). If the domain was verified, also `NOTIFY_FROM` like
   `Sentrada <notify@sentrada.io>`. Redeploy (B6).
4. Verify: repeat a Block C tease+submit; the email must arrive the same day
   and contain the piece id and NO part of the address. If it does not
   arrive, check Resend's dashboard logs; the capture poll remains the
   fallback either way.

## Block E — Move sentrada.io to Vercel (sender, 5 minutes) — SITE COMES BACK UP

Only after Block C is green. This is the step that ends the downtime: once
DNS propagates, sentrada.io serves the new Vercel deployment from `main`.

1. Vercel: Project Settings > Domains > add `sentrada.io` and
   `www.sentrada.io`. It shows configuration warnings until DNS moves.
2. Namecheap: Domain List > sentrada.io > Manage > Advanced DNS. WRITE DOWN
   the existing records first (for rollback). Delete only the GitHub Pages
   records: A records pointing at 185.199.108.153 / .109. / .110. / .111.
   addresses, and any CNAME pointing at `joetrajectory.github.io`. Add:
   - A record, host `@`, value `76.76.21.21`
   - CNAME record, host `www`, value `cname.vercel-dns.com`
   Leave everything else (the nobodyresponds.com redirect lives on that
   domain, not this one; leave any TXT/MX records alone).
3. Wait until Vercel's Domains page shows the domain valid with a
   certificate (minutes to an hour). Check https://sentrada.io loads and
   https://sentrada.io/for/x shows the expired state.
4. GitHub Pages: making the repo private (Block P) already took the Pages site
   down. Confirm it is off under repo Settings > Pages (Source should be None
   or unavailable on the private repo). The CNAME file in the repo is now
   inert and can be deleted in a later cleanup.
5. Rollback if the DNS switch itself goes wrong (not the private flip):
   restore the Namecheap records written down in step 2. Note the old GitHub
   Pages site will only return if the repo is made public again, which
   re-exposes the PII, so prefer fixing forward on Vercel over rolling back.

## Final validation (session + sender, 10 minutes) — site is back up

The build brief's definition of done, run once against the live domain (now
served from `main` via Vercel):
repeat Block C steps 1 to 8 with `SENTRADA_CAPTURE_API=https://sentrada.io`
(which is also the committed config default, so the export is optional), plus
Block D step 4 for the email. Confirm sentrada.io itself loads the sales site
(not a 404) and `sentrada.io/for/x` shows the expired state. The sender walks
a token link on their actual phone. When all pass, the flow is live and the
downtime window is closed; start the pilot (two or three remote-likely names,
LinkedIn DMs, mystery variant first).

## Day-to-day reference

Lives in CLAUDE.md ("Address capture" section) and runner/README.md ("Address
capture (the tease flow)"). Regression test after ANY edit to api/ or the
capture commands: `python runner/sentrada_runner.py capture-probe`.

## Troubleshooting

- 401 from tease/capture/address/delivered: `SENTRADA_RUNNER_SECRET` (env or
  .env) does not match Vercel's `RUNNER_SECRET`, or the redeploy after
  setting it was skipped.
- "capture API unreachable": Block A network policy, or the URL in config
  `capture_api` / `SENTRADA_CAPTURE_API`.
- Page shows "That didn't load.": the /api/token function failed; check the
  function logs in Vercel (Deployments > deployment > Functions). Usually
  missing store env vars (redeploy after attaching Upstash).
- /for/<token> gives 404 or drops to the homepage: the vercel.json rewrite;
  check it deployed (vercel.json is committed) and consult the session.
- Email never arrives: Resend logs first; unverified sender to a
  non-account inbox is the usual cause (Block D step 1). Submissions are
  still captured regardless; `capture` shows them.
- To rotate the runner secret: set a new value in Vercel, redeploy, update
  .env / environment settings. Tokens and data are unaffected.

## Residual risks (a cold-eyes review accepted these as known, not fixed)

- **The link is a bearer capability.** Anyone who holds a live `/for/<token>`
  link can submit any address, and the first submission is final (the record
  locks to "submitted"). So: never post a tease link anywhere semi-public;
  keep it to a direct DM or email to the target. If a bad-faith or mistaken
  submission ever needs undoing, there is currently no reset command; ask a
  session to purge and re-tease (`delivered` then `tease --again` after
  re-marking printed), or add a `reset` action.
- **`address` prints to the terminal by design.** If you run it inside a
  logged or cloud session, the address lands in that transcript, which the
  `delivered` purge cannot reach. Run `address` in a local, unlogged terminal
  when possible; the command prints this warning each time.
- **Committed ledgers in a PUBLIC repo.** See the separate note below; this is
  a project-level decision, not a capture bug. (This deployment fixes it: the
  repo is flipped private in Block P.)

Fixed since the review: the token page no longer loads Google Fonts. Fraunces
is self-hosted from `/assets/fraunces.woff2` and the body uses a system font
stack, so the page makes zero third-party requests (asserted by the
walkthrough).

## The public-repo data question (decide before the pilot)

`runner/outcomes.json` is committed and world-readable, and it contains real
recipient names, companies and verbatim private replies. `runner/capture.json`
(once teasing starts) will hold recipient names and slugs of the form
`firstname-lastname-company`. The repo `joetrajectory/sentrada-one-pager` is
public. These files are committed on purpose (containers are ephemeral and the
ledgers must survive), but a public repo is the wrong place for third-party
PII. Options, roughly in order of effort: make the repo private (simplest,
fixes it wholesale); or gitignore the ledgers and persist them privately
(a private gist, a separate private repo, or Notion) while keeping opaque
piece codes instead of name-company slugs. Either way the existing history
needs scrubbing (`git filter-repo`), because the data is already public.
This is the single highest-impact item the review surfaced and it is the
sender's call.

## Deployment log

- (fill in: date, who, project URL, surprises found)
