[//]: # (ARCH-MDA-3e8b5f1a)

# MeDea — News Signal Ratio

[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-%E2%9D%A4-navy?logo=github&logoColor=white&color=1C3F6E)](https://github.com/sponsors/The-Architect-Neo)

The news is not balanced. MeDea measures the imbalance.

Crawls RSS feeds every hour, scores each headline as positive or negative using VADER sentiment analysis, and displays a single ratio — the ProCon Score. High score means clean signal. Low score means heavy filter load.

## What it shows

- The ProCon Score — percentage of headlines worth reading vs filtered out
- Per-source breakdown with inline ratio bars
- Top positive and negative headlines by confidence
- Flagged headlines held for human review rather than forced into a bucket

## Run locally

```bash
pip install flask feedparser vaderSentiment requests certifi
python app.py
```

Open `http://localhost:5000`. First load takes 10–15 seconds while the initial fetch runs. The loader shows per-feed progress in real time.

## Deploy to Railway

Connect this repo to a Railway service. No environment variables required for base operation.

```
web: python app.py
```

## Stack

Python · Flask · VADER Sentiment · feedparser · RSS

## Licence

MIT — see LICENSE

© 2026 The Architect
