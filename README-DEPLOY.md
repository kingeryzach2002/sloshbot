# Deploy Runbook (Owner Guide)

This is a plain-language guide to running Sloshbot on a host, no engineering
background required.

## The golden rule

**The database lives on a separate "volume" (a persistent disk), not inside
the app.** Every time you deploy new code, the host throws away the old
container and builds a fresh one — but the volume is untouched. That's what
keeps your events, feedback, and settings safe across deploys. Never delete
or resize the volume without a backup.

## Environment variables

Set these in your hosting provider's dashboard (usually under
Settings -> Environment Variables). They are read when the container starts.

| Variable | What it does |
|---|---|
| `SLOSHBOT_SECRET_KEY` | A random secret that signs each visitor's anonymous identity cookie, so no one can impersonate another visitor. Set once, never share it. |
| `SLOSHBOT_ADMIN_TOKEN` | A shared secret that lets your own computer's daily refresh job push fresh events to this host. See "The daily refresh now runs on your own computer" below. |
| `PORT` | Which network port the app listens on. Most hosts set this automatically — you usually don't need to touch it. |

`ANTHROPIC_API_KEY` is **no longer needed on the host** — event scoring now
happens on your own computer, not here. See below.

## Optional: offsite backup (Litestream)

By default, your data is only as safe as the volume it lives on. You can
turn on continuous, automatic offsite backup to a cheap cloud storage
bucket (we recommend Cloudflare R2 — no egress fees). This is optional and
**does nothing until you configure it** — the app runs exactly the same
either way.

**To turn it on:**

1. Create a bucket in Cloudflare R2 (or any S3-compatible storage provider).
2. In that provider's dashboard, create an API token/key pair scoped to
   that bucket (an "access key ID" and "secret access key").
3. In your hosting dashboard, add these four environment variables:

   | Variable | What to put there |
   |---|---|
   | `LITESTREAM_BUCKET` | The bucket name you created. |
   | `LITESTREAM_ENDPOINT` | The bucket's API endpoint URL (your storage provider shows this next to the bucket). |
   | `LITESTREAM_ACCESS_KEY_ID` | The access key ID from step 2. |
   | `LITESTREAM_SECRET_ACCESS_KEY` | The secret access key from step 2. Treat this like a password. |

4. Redeploy. From then on, every change to your database is streamed to
   the bucket within seconds, in the background — you won't notice it
   running.

**Undoing it:** delete those four variables and redeploy. Backups stop;
nothing else changes.

## If disaster strikes: one-command restore

If the volume is ever lost or you're setting up a brand-new server, and
offsite backup was turned on beforehand, the app **restores itself
automatically on boot** — no action needed. It notices there's no local
database, finds the latest backup in the bucket, and rebuilds it before
starting.

If you ever need to restore by hand (e.g. onto a fresh machine, before the
app has started), the command is:

```
litestream restore -o /data/sloshbot.db <bucket path>
```

Ask your engineer if you need to run this manually — the automatic restore
above covers the normal case.

## OpenHost

Sloshbot can also run on OpenHost (Imbue's self-host platform), which works
a bit differently from Railway-style hosts described above.

**Deploying:** From the OpenHost dashboard, choose "Deploy New App," point it
at the sloshbot git repo URL, and it builds and runs the Dockerfile
automatically. To push a new version later, just hit the reload/redeploy
button in the dashboard — OpenHost stops the old container and starts the
new one, so there's never two copies writing to the database at once.

**Where secrets live:** OpenHost has no "Environment Variables" panel like
Railway — there's no dashboard way to set custom env vars at all. Instead,
create a file named `secrets.env` inside the app's persistent data folder
(the same folder the database lives in). That folder is
`/data/app_data/sloshbot/` from inside the container (the value of
`OPENHOST_APP_DATA_DIR`), and on the host it lives under
`<persistent_data_dir>/app_data/sloshbot/` — e.g.
`/home/host/.openhost/local_compute_space/persistent_data/app_data/sloshbot/`.
It's the same directory that holds `sloshbot.db`.

Two UI ways to create/edit it, whichever your instance exposes:
- **File Browser** — OpenHost's built-in file-manager app (dufs-based; it
  appears in your dashboard's app list). Open it, navigate into the
  `sloshbot` app_data folder — you'll see `sloshbot.db` there — and create or
  upload `secrets.env`. (Note: it is *not* "filestash"; earlier drafts of
  this doc named the wrong tool.)
- **Terminal shell** — if your instance gives you a host shell, `cd` into the
  path above and create the file with `nano secrets.env` (or upload it and
  drop it in). Watch for editors silently saving it as `secrets.env.txt`.

Put one `KEY=value` line per variable; no quotes needed. Sloshbot reads the
file automatically on every boot. Variables you can put in it:

| Variable | What it does |
|---|---|
| `SLOSHBOT_SECRET_KEY` | Signs each visitor's anonymous identity cookie so no one can impersonate another visitor. You normally don't need to set this yourself — see below. |
| `SLOSHBOT_ADMIN_TOKEN` | Lets your own computer's daily refresh job push fresh events here. You normally don't need to set this yourself either — see below. |
| `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | Same offsite-backup variables described above — same meaning, just placed in this file instead of a dashboard. |

`ANTHROPIC_API_KEY` does **not** go here anymore — event scoring runs on your
own computer now, not on OpenHost. See "The daily refresh now runs on your
own computer" below.

**Self-generating secret key and admin token:** you don't have to invent or
set `SLOSHBOT_SECRET_KEY` or `SLOSHBOT_ADMIN_TOKEN` yourself. On first boot,
if either isn't already present in `secrets.env`, sloshbot generates a random
value and saves it back into that file automatically — visitor identities are
secure, and your computer's daily sync job has a token to authenticate with,
from the very first deploy with zero action from you. (If you ever want to
force new visitor identities, or rotate the sync token, delete the
corresponding line from `secrets.env` and redeploy; a fresh one will be
generated.) You'll need to copy the generated `SLOSHBOT_ADMIN_TOKEN` value out
of `secrets.env` once, to paste into your computer's `.env.sloshbot` — see
below.

**No background refresh runs on the host anymore:** event sources
increasingly block requests from datacenter IPs like OpenHost's, so scraping
and scoring both moved to your own computer, which pushes the finished
catalog here over HTTPS once a day. OpenHost never scrapes and never calls
Claude. See the next section.

**Custom domain via Cloudflare redirect (this is how the live site is set
up):** OpenHost apps get a URL like
`https://sloshbot.kingeryzach2002.selfhost.imbue.com/`. The production domain
`sloshbot.beer` points there via a **Cloudflare Redirect Rule**, not a DNS
proxy. A true proxy/CNAME does *not* work on a shared imbue-hosted box: their
router routes by the `selfhost.imbue.com` hostname and the TLS cert only
covers that name, so pointing an outside domain straight at it breaks. (Native
custom-domain support — imbue terminating TLS for your domain so it stays in
the address bar — was asked for and declined as of this writing; revisit if
that changes.)

The exact working setup, reproducible for any domain on Cloudflare:

1. **DNS** — the hostname must resolve *and* be proxied for a Redirect Rule to
   fire, but there's no real origin (you're bouncing to imbue). So point it at
   a throwaway IP and let Cloudflare's edge intercept: DNS -> Records -> Add
   record -> type **A**, Name `@` (the apex/root), IPv4 **`192.0.2.1`** (a
   reserved "goes nowhere" address — never actually contacted), Proxy status
   **Proxied** (orange cloud). For a subdomain instead of the root, use its
   name (e.g. `events`) as the record Name.
2. **Redirect Rule** — Rules -> Redirect Rules -> Create Rule (the "Redirect
   to a different domain" template is the closest starting point):
   - Match: **Hostname** **equals** `sloshbot.beer` (expression
     `http.host eq "sloshbot.beer"`).
   - Then: URL redirect, Type **Dynamic**, Expression
     `concat("https://sloshbot.kingeryzach2002.selfhost.imbue.com", http.request.uri.path)`,
     Status code **301**, "Preserve query string" checked. (Dynamic + the
     `concat` carries the path through so `sloshbot.beer/map` lands on the map;
     a **Static** redirect to `https://sloshbot.kingeryzach2002.selfhost.imbue.com/`
     also works but sends every path to the homepage.)
   - Deploy. Cloudflare auto-issues the TLS cert for the domain within a few
     minutes.

Note: this is a redirect, not a proxy, so visitors' browsers show the
`selfhost.imbue.com` address in the bar after landing — that's expected, not a
bug, and there's no way around it without native custom-domain support (see
above).

## The daily refresh now runs on your own computer

Event sites increasingly block requests coming from datacenter servers like
OpenHost or Railway, so scraping and scoring no longer happen on the hosted
server at all. Instead, your own Mac scrapes and scores events once a day and
pushes the finished results to the server over the internet. The server
itself never scrapes anything and never calls Claude anymore — it just serves
whatever was last pushed to it.

**One-time setup, on your Mac:**

1. In the `sloshbot` folder, copy `.env.sloshbot.example` to a new file named
   `.env.sloshbot` (this file is never committed to git — it holds secrets).
2. Open it and fill in three values:
   - `SLOSHBOT_PROD_URL` — the URL of your hosted server (e.g.
     `https://sloshbot.kingeryzach2002.selfhost.imbue.com`).
   - `SLOSHBOT_ADMIN_TOKEN` — copy this from the server's `secrets.env` file
     (OpenHost auto-generates it on first boot; open `secrets.env` via the
     File Browser app as described above and copy the
     `SLOSHBOT_ADMIN_TOKEN=...` line's value).
   - `ANTHROPIC_API_KEY` — your Claude API key. This now belongs here, not on
     the hosted server.
3. Run `scripts/install_launchd.sh` once. This registers a daily job with
   macOS (launchd — the built-in scheduler, similar to cron) that runs the
   refresh automatically. Safe to re-run any time you change the schedule.

**Checking logs:** the job's output (both normal logs and errors) is written
to `~/Library/Logs/sloshbot/pipeline.log`. Open it in any text editor, or
`tail -f ~/Library/Logs/sloshbot/pipeline.log` in Terminal to watch it live.

**Running it manually** (don't want to wait for the schedule, or testing a
change):

```
launchctl kickstart gui/$(id -u)/com.sloshbot.pipeline
```

**What happens on the schedule:** the job is set to run daily at 7:30am. If
your laptop is asleep at 7:30 (lid closed), macOS runs the missed job as soon
as it wakes up instead of skipping the day — you don't need to keep the
laptop on a schedule or awake overnight.

**After pulling new code:** if you (or an engineer) change how the server or
the refresh job work, redeploy the server from the OpenHost/Railway dashboard
(see "Deploying" above) so it picks up the new code — the daily job on your
Mac also needs to be running the latest code, so re-run
`scripts/install_launchd.sh` after pulling changes there too.
