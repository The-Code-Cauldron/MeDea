import os
import re
import math
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import urllib3
import requests
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from flask import Flask, render_template, jsonify

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('medea')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# (name, url, bias_label)
FEEDS = [
    ('BBC News',        'https://feeds.bbci.co.uk/news/rss.xml',                    'Centre'),
    ('The Guardian',    'https://www.theguardian.com/world/rss',                     'Left'),
    ('Sky News',        'https://feeds.skynews.com/feeds/rss/home.xml',              'Right'),
    ('The Independent', 'https://www.independent.co.uk/rss',                         'Centre-Left'),
    ('Reuters',         'https://feeds.reuters.com/reuters/UKTopNews',               'Wire'),
    ('Al Jazeera',      'https://www.aljazeera.com/xml/rss/all.xml',                 'Global'),
    ('City A.M.',       'https://www.cityam.com/feed/',                              'Business'),
    ('UnHerd',          'https://unherd.com/feed/',                                  'Independent'),
    ('Byline Times',    'https://bylinetimes.com/feed/',                             'Independent'),
    ('The Spectator',   'https://www.spectator.co.uk/feed/',                         'Right'),
]

REFRESH_INTERVAL    = 3600
POSITIVE_THRESHOLD  =  0.05
NEGATIVE_THRESHOLD  = -0.05

_analyzer = SentimentIntensityAnalyzer()

_SESSION = requests.Session()
_SESSION.headers.update({'User-Agent': 'MeDea/2.0 (news signal ratio)'})
_SESSION.verify = False

# ── Quality detection ────────────────────────────────────────────────────────

SATIRE_DOMAINS = frozenset({
    'dailymash.co.uk', 'newsthump.com', 'babylonbee.com',
    'theonion.com', 'thepoke.co.uk', 'waterfordwhispersnews.com',
})

_SCARE_QUOTE_RE = re.compile(
    r"['‘’“”][^‘’“”'\"]{2,40}['‘’“”]"
)

_CLICKBAIT_RE = re.compile(
    r'^(here[’\']?s why|you won[’\']?t believe|this is why|turns out|'
    r'it[’\']?s official|why you should|the real reason|everything you need)',
    re.IGNORECASE,
)

_NEGATIVE_CONTEXT = frozenset({
    'crisis', 'fail', 'failure', 'death', 'deaths', 'collapse', 'disaster',
    'scandal', 'fraud', 'corruption', 'chaos', 'emergency', 'catastrophe',
    'tragedy', 'killed', 'dies', 'dead', 'suffer', 'crash', 'blast',
    'attack', 'war', 'conflict', 'riot', 'protest', 'strike',
})

_OPINION_STARTERS = (
    'why ', 'opinion: ', 'comment: ', 'analysis: ',
    'view: ', 'letter: ', 'column: ',
)


def _quality_flags(title, url, compound):
    sarcasm_risk = False
    opinion      = False
    clickbait    = False
    satire       = False
    tl = title.lower().strip()

    try:
        domain = urlparse(url).netloc.lower().lstrip('www.')
        if any(sd in domain for sd in SATIRE_DOMAINS):
            satire = True
    except Exception:
        pass

    if _SCARE_QUOTE_RE.search(title):
        sarcasm_risk = True

    if title.strip().endswith('?'):
        sarcasm_risk = True

    if compound >= POSITIVE_THRESHOLD and (set(tl.split()) & _NEGATIVE_CONTEXT):
        sarcasm_risk = True

    if _CLICKBAIT_RE.match(tl):
        clickbait = True

    if any(tl.startswith(m) for m in _OPINION_STARTERS):
        opinion = True

    return {
        'sarcasm_risk': sarcasm_risk,
        'opinion':      opinion,
        'clickbait':    clickbait,
        'satire':       satire,
    }


# ── Main state ───────────────────────────────────────────────────────────────

_state = {
    'score': None,
    'pro': [], 'con': [], 'flagged': [],
    'sources': {},
    'last_updated': None,
    'pro_count': 0, 'con_count': 0, 'flagged_count': 0,
    'total': 0,
    'ready': False,
}
_lock = threading.Lock()

_progress = {'status': 'idle', 'feeds': {}}
_prog_lock = threading.Lock()


def _classify(compound):
    if compound >= POSITIVE_THRESHOLD:  return 'pro'
    if compound <= NEGATIVE_THRESHOLD:  return 'con'
    return 'flagged'


def _score_class(score):
    if score is None:  return 'dial-neutral'
    if score < 40:     return 'dial-bad'
    if score <= 60:    return 'dial-neutral'
    return 'dial-good'


def _dial_arc(score):
    if score is None:
        return None, None, None
    cx, cy, r = 150, 150, 120
    angle = math.pi * (1 - score / 100)
    ex = cx + r * math.cos(angle)
    ey = cy - r * math.sin(angle)
    path = f'M 30 150 A {r} {r} 0 0 1 {ex:.2f} {ey:.2f}'
    nr = 108
    return path, round(cx + nr * math.cos(angle), 1), round(cy - nr * math.sin(angle), 1)


def _filter_description(score, pro_count, con_count):
    if score is None:
        return 'No scoreable headlines this cycle — all flagged for review'
    total = pro_count + con_count
    if total == 0:
        return 'No headlines scored'
    if score >= 70:
        return f'{con_count} negative headlines filtered — {pro_count} good ones found'
    if score >= 50:
        return f'Mixed signal — {con_count} filtered to reach {pro_count} worth reading'
    return f'Heavy filter load — {con_count} negatives for every {pro_count} worth reading'


def _fetch_one(name, url, bias, results):
    with _prog_lock:
        _progress['feeds'][name] = {'status': 'fetching', 'count': 0}
    pro, con, flagged = [], [], []
    error = False
    try:
        resp = _SESSION.get(url, timeout=15)
        feed = feedparser.parse(resp.content)
        log.info(f'{name}: {len(feed.entries)} entries fetched')
        for entry in feed.entries[:25]:
            title = (entry.get('title') or '').strip()
            if not title:
                continue
            link = (entry.get('link') or '').strip()
            vs = _analyzer.polarity_scores(title)
            compound = round(vs['compound'], 3)
            flags = _quality_flags(title, link, compound)

            if flags['satire']:
                log.info(f'Satire excluded: {title[:60]}')
                continue

            item = {
                'title':        title,
                'source':       name,
                'bias':         bias,
                'compound':     compound,
                'url':          link,
                'sarcasm_risk': flags['sarcasm_risk'],
                'opinion':      flags['opinion'],
                'clickbait':    flags['clickbait'],
            }
            label = _classify(compound)
            if label == 'pro':     pro.append(item)
            elif label == 'con':   con.append(item)
            else:                  flagged.append(item)
    except Exception as exc:
        log.error(f'{name} failed: {exc}')
        error = True

    results[name] = {'pro': pro, 'con': con, 'flagged': flagged, 'error': error, 'bias': bias}
    total = len(pro) + len(con) + len(flagged)
    with _prog_lock:
        _progress['feeds'][name] = {
            'status': 'error' if error else 'done',
            'count':  total,
        }


def _fetch():
    with _prog_lock:
        _progress['status'] = 'fetching'
        _progress['feeds'] = {name: {'status': 'pending', 'count': 0} for name, _, _b in FEEDS}

    results = {}
    threads = [
        threading.Thread(target=_fetch_one, args=(name, url, bias, results), daemon=True)
        for name, url, bias in FEEDS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)

    all_pro, all_con, all_flagged = [], [], []
    sources = {}
    for name, _, bias in FEEDS:
        r = results.get(name, {'pro': [], 'con': [], 'flagged': [], 'error': True, 'bias': bias})
        all_pro.extend(r['pro'])
        all_con.extend(r['con'])
        all_flagged.extend(r['flagged'])
        sources[name] = {
            'pro':     len(r['pro']),
            'con':     len(r['con']),
            'flagged': len(r['flagged']),
            'error':   r.get('error', False),
            'bias':    bias,
        }

    scored = len(all_pro) + len(all_con)
    score = round(len(all_pro) / scored * 100) if scored else None
    log.info(f'Fetch complete — Pro:{len(all_pro)} Con:{len(all_con)} Flagged:{len(all_flagged)} Score:{score}')

    all_pro.sort(key=lambda x: x['compound'], reverse=True)
    all_con.sort(key=lambda x: x['compound'])

    with _lock:
        _state.update({
            'score':         score,
            'pro':           all_pro[:8],
            'con':           all_con[:8],
            'flagged':       all_flagged[:6],
            'sources':       sources,
            'last_updated':  datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC'),
            'pro_count':     len(all_pro),
            'con_count':     len(all_con),
            'flagged_count': len(all_flagged),
            'total':         len(all_pro) + len(all_con) + len(all_flagged),
            'ready':         True,
        })

    with _prog_lock:
        _progress['status'] = 'done'


def _loop():
    while True:
        _fetch()
        time.sleep(REFRESH_INTERVAL)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    with _lock:
        d = {k: v for k, v in _state.items()}
        d['pro']     = list(_state['pro'])
        d['con']     = list(_state['con'])
        d['flagged'] = list(_state['flagged'])
        d['sources'] = dict(_state['sources'])
    d['score_class'] = _score_class(d['score'])
    d['arc_path'], d['nx'], d['ny'] = _dial_arc(d['score'])
    d['description'] = _filter_description(d['score'], d['pro_count'], d['con_count'])
    return render_template('index.html', **d)


@app.route('/api/score')
def api_score():
    with _lock:
        return jsonify({
            'score':        _state['score'],
            'pro':          _state['pro_count'],
            'con':          _state['con_count'],
            'flagged':      _state['flagged_count'],
            'total':        _state['total'],
            'last_updated': _state['last_updated'],
        })


@app.route('/api/progress')
def api_progress():
    with _prog_lock:
        return jsonify({
            'status': _progress['status'],
            'feeds':  dict(_progress['feeds']),
        })


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    threading.Thread(target=_fetch, daemon=True).start()
    return jsonify({'status': 'fetching'})


if __name__ == '__main__':
    threading.Thread(target=_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
