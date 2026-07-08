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
| `SLOSHBOT_SECRET_KEY` | A random secret used to keep login sessions secure. Set once, never share it. |
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
