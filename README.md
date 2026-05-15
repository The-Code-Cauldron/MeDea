[//]: # (ARCH-MDA-3e8b5f1a)

# MeDea — News Signal Intelligence

[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-%E2%9D%A4-navy?logo=github&logoColor=white&color=1C3F6E)](https://github.com/sponsors/The-Architect-Neo)

The news is not balanced. MeDea measures the imbalance — and surfaces what matters.

32 global sources. Scored every 30 minutes. One number tells you whether today's news cycle is worth your attention.

**Live:** medea-production-dd4b.up.railway.app

---

## What it does

- **ProCon Score** — percentage of headlines scoring positive vs filtered out. Updated every 30 minutes.
- **The Dispatch** — editor-curated stories filed directly by The Architect. Persistent, admin-controlled.
- **OpinionSays** — single editorial statement from the editor. Always current.
- **Your Signal** — geo-located section serving regional news based on the reader's country.
- **Topic filter** — filter all sections by topic (Politics, Health, Economy, Technology, Environment, Human Rights etc.)
- **Ad rotation** — 5 independent ad slots, up to 5 advertisers per slot, 45-second rotation.
- **Analytics** — impression tracking, story click intelligence, live reader count (admin only).
- **PWA** — installable on any phone, any OS, no app store.

---

## Stack

Python · Flask · VADER Sentiment · feedparser · psycopg2 · Neon Postgres · Railway

---

## Environment variables (Railway)

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Neon Postgres connection string |
| `DISPATCH_ADMIN_TOKEN` | Yes | Admin password |
| `FLASK_SECRET_KEY` | Yes | Session signing — must be set for persistent sessions |
| `GITHUB_WEBHOOK_SECRET` | Optional | Verifies GitHub Sponsors events |
| `PATREON_WEBHOOK_SECRET` | Optional | Verifies Patreon events |

---

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`. First load takes 15–20 seconds while the initial feed fetch runs.

Admin: `http://localhost:5000/login`

---

## Deploy

Connect to Railway. Set environment variables. Push to main — Railway auto-deploys.

```
web: python app.py
```

---

## Licence

MIT — see LICENSE

© 2026 The Architect
