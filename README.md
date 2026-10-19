# Web-Host

Telegram bot + **web console** that hosts static sites and runs apps/bots (Python, Node, Docker) with live logs, env vars and auto-restart. Open your Render URL and sign in.

## What you get

**Web console (`/`)**
- **Overview** - CPU / RAM / disk, service status, 14-day traffic, activity feed, runtime checks (Docker / Git / Node)
- **Sites** - upload a ZIP (drag & drop with progress), file browser + editor, rename, ZIP download, SPA mode, auto-delete timer
- **Services** (Render-style)
  - Create from **ZIP or GitHub repo**, **native or Docker** runtime, **web service or background worker** (Telegram bots)
  - **Live logs** (follow, filter, wrap, copy, save), separate **deploy logs** with success/failure and the exact error
  - **Environment variables** (masked, `.env` paste, shared vars, restart on save)
  - **Files** (browse/edit/upload), **Shell** (one-off commands), **Settings** (build/start cmd, port, auto-restart)
  - Auto-restart with back-off, crash alerts, deploy history, GitHub **auto-deploy webhook**
  - Web services are reverse-proxied at `/svc/<name>/`
- **Telegram** - bot status, users, ban/unban, broadcast

**Telegram bot**
- Users: `/start /mysites /info /qr /rename /delete /expire /stats`; caption a ZIP with the site name to deploy instantly; QR codes; inline buttons
- Admin: `/status /services /logs /deploy /restart /svcstart /svcstop /setenv /env /unsetenv /broadcast /ban /unban /panel`; deploy/crash alerts are sent to you

## Deploy a project straight from GitHub (no ZIP)

1. Console -> **Services -> New service -> Git repository**
2. Public repo: paste the URL. Private repo or the repo picker: set a `GITHUB_TOKEN` (fine-grained token with *Contents: read*) in the host environment or in **Environment -> Shared variables**, then click **Load** to choose from your repos.
3. Optional: copy the **auto-deploy webhook** from the service's Settings into GitHub (*Settings -> Webhooks*, push events) to redeploy on every push.

The token is sent to GitHub as a one-off header, is not written to `.git/config` and does not appear in logs.

## Deploy this project on Render

1. New **Web Service** -> this repo -> **Root Directory** `bot`
2. Environment variables:

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Console login password (**set this**). Falls back to `API_KEY` if unset |
| `BOT_TOKEN` | Telegram bot token (optional) |
| `ADMIN_ID` | Your Telegram user id (admin commands + alerts) |
| `API_KEY` | Key for `/api/upload` |
| `DOMAIN` | Optional; defaults to Render's external URL |
| `DATA_DIR` | `/data` - mount a Render Disk there |
| `GITHUB_TOKEN` | Optional; list/deploy private repos |

If `ADMIN_PASSWORD` and `API_KEY` are both unset, a random password is printed in the deploy logs.

## Important limits

- **Persistence:** Render's free tier has no disk - sites, services and env vars are lost on every redeploy/restart. Attach a Disk (paid) and set `DATA_DIR=/data`. The console warns you when data is not persistent.
- **Docker runtime:** Render/Railway/Vercel do **not** give containers a Docker daemon, so the *Docker* runtime cannot build images there. The console detects this, disables the Docker option, and runs Dockerfile projects **natively** (reads `CMD`, `ENV`, `EXPOSE`, and `pip`/`npm` RUN steps). For real Docker, run on a VPS with `docker compose up -d` (mounts the host Docker socket).
- Services run as the same OS user as the console. Only host code you trust. Variables like `BOT_TOKEN` are **not** inherited by services.
- Free instances sleep when idle, which also pauses your bots. Use a paid instance or an uptime pinger on `/health`.

## Run locally

```bash
cd bot
pip install -r requirements.txt
ADMIN_PASSWORD=secret python webhost.py     # http://localhost:8000
```

## Deploy from CI

```bash
curl -X POST https://YOUR-APP/api/upload -H "X-API-Key: $API_KEY" -F site_name=my-site -F file=@site.zip
```
