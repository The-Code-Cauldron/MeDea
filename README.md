[//]: # (ARCH-MDA-3e8b5f1a)

# MeDea

[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-navy?logo=github&logoColor=white&color=1C3F6E)](https://github.com/sponsors/The-Architect-Neo)

**Signal over noise.**

The news is not balanced. MeDea measures the imbalance — reading 32 global newsrooms every 30 minutes, scoring every headline for sentiment and framing quality, and returning a single number: the ProCon Score.

Bloomberg does this for financial markets at £20,000/year per seat. MeDea does it for the world.

---

## The ProCon Score

Every headline is classified Pro (constructive, positive, worth your attention) or Con (fear-coded, alarmist, filtered out). Edge cases are held for editorial judgement — never forced.

**ProCon Score = (Pro headlines / Total headlines) × 100**

A score of 35 means 65% of today's headlines are negative. A score of 72 means the signal is clean. One number. Actionable.

---

## What's live

- 32 independent newsrooms across six continents — scored every 30 minutes
- Framing risk detection — catches geographic/institutional misleads
- Force-negative override — VADER sentiment corrected for domain blind spots
- Topic filter — Politics · War · Crime · Health · Economy · Environment · Technology · Education · Human Rights
- The Dispatch — editor-curated stories with archive links for paywalled content
- OpinionSays — direct editorial voice from The Architect
- Your Signal — geo-detected regional news layer per reader
- Ad rotation — 5 independent slots, up to 5 advertisers each, 45-second cycle
- Direct message to the editor — Stripe-powered £1.50 shoutout
- Analytics — impression tracking, story clicks, live reader count
- PWA — installable on any phone, any OS, no app store required
- Secure admin — session-based, login-gated, no token in URL

---

## Stack

Python · Flask · VADER Sentiment · feedparser · Neon Postgres · Railway · Stripe

---

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

First load: 15–20 seconds while the feed fetch runs. Admin login at `/login`.

---

## Deploy

Connect to Railway. Set required environment variables. Push to `main` — Railway auto-deploys.

```
web: python app.py
```

---

## Licence

MIT — see LICENSE

© 2026 The Architect · [the-architect-neo.github.io](https://the-architect-neo.github.io/)
