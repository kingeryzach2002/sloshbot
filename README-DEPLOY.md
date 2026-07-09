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
| `ANTHROPIC_API_KEY` | Lets the app call Claude to help score/summarize events. Required for scoring to work. |
| `PIPELINE_INTERVAL_HOURS` | How often (in hours) the app refreshes event data in the background. Leave unset (or `0`) if your host has its own scheduler and you're running the refresh job separately instead. |
| `PORT` | Which network port the app listens on. Most hosts set this automatically — you usually don't need to touch it. |

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
(the same folder the database lives in). The easiest way to create/edit it
is with the **filestash** file-manager app available on the instance —
open it, navigate to sloshbot's data folder, and create `secrets.env` as a
plain text file with one `KEY=value` line per variable. Sloshbot reads this
file automatically on every boot. Variables you can put in it:

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Lets the app call Claude to help score/summarize events. Required for scoring to work. |
| `SLOSHBOT_SECRET_KEY` | Signs each visitor's anonymous identity cookie so no one can impersonate another visitor. You normally don't need to set this yourself — see below. |
| `PIPELINE_INTERVAL_HOURS` | How often (in hours) the background refresh runs. Defaults to `4` automatically on OpenHost (see below); set this only to override that default. |
| `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | Same offsite-backup variables described above — same meaning, just placed in this file instead of a dashboard. |

**Self-generating secret key:** you don't have to invent or set
`SLOSHBOT_SECRET_KEY` yourself. On first boot, if it isn't already present
in `secrets.env`, sloshbot generates a random one and saves it back into
that file automatically, so visitor identities are secure from the very
first deploy with zero action from you. (If you ever want to force new
visitor identities — e.g. after a suspected leak — delete that line from
`secrets.env` and redeploy; a fresh one will be generated.)

**Background refresh runs by default:** unlike Railway (where you set
`PIPELINE_INTERVAL_HOURS` yourself because you might be using an external
scheduler), OpenHost has no cron/scheduler feature at all, so sloshbot
defaults the refresh loop to every 4 hours automatically — no setup needed.

**Custom domain via Cloudflare redirect:** OpenHost apps get a URL like
`https://sloshbot.kingeryzach2002.selfhost.imbue.com/`. If you want your
own domain (e.g. `events.yourdomain.com`) to point people there, the
simplest approach is a Cloudflare Redirect Rule rather than a DNS proxy:

1. In the Cloudflare dashboard for your domain, go to Rules -> Redirect
   Rules -> Create Rule.
2. Match: hostname equals `events.yourdomain.com` (or whichever subdomain
   you want).
3. Then: Dynamic/Static redirect to
   `https://sloshbot.kingeryzach2002.selfhost.imbue.com/`, status code
   **301** (permanent).
4. Save and deploy the rule.

Note: this is a redirect, not a proxy, so visitors' browsers will show the
`selfhost.imbue.com` address in the address bar after landing — there's no
way to make the OpenHost URL invisible with a simple redirect. If you want
the custom domain to stay in the address bar, that requires OpenHost to
support custom domains natively (ask your engineer to check current
platform support) rather than a Cloudflare redirect.
