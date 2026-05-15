import os
import re
import math
import html as _html
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import urllib3
import requests
import feedparser
import psycopg2
import psycopg2.extras
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from flask import Flask, render_template, jsonify, request

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('medea')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# (name, rss_url, bias_label, website)
FEEDS = [
    ('The Guardian',        'https://www.theguardian.com/world/rss',                'Left',          'https://www.theguardian.com'),
    ('Sky News',            'https://feeds.skynews.com/feeds/rss/home.xml',         'Right',         'https://news.sky.com'),
    ('The Independent',     'https://www.independent.co.uk/rss',                    'Centre-Left',   'https://www.independent.co.uk'),
    ('Al Jazeera',          'https://www.aljazeera.com/xml/rss/all.xml',            'Global',        'https://www.aljazeera.com'),
    ('UnHerd',              'https://unherd.com/feed/',                             'Independent',   'https://unherd.com'),
    ('Byline Times',        'https://bylinetimes.com/feed/',                        'Independent',   'https://bylinetimes.com'),
    ('The Conversation UK', 'https://theconversation.com/uk/articles.atom',         'Academic',      'https://theconversation.com/uk'),
    ('Positive News',       'https://www.positive.news/feed/',                      'Independent',   'https://www.positive.news'),
    ('New Statesman',       'https://www.newstatesman.com/feed/',                   'Centre-Left',   'https://www.newstatesman.com'),
    ('Middle East Eye',     'https://www.middleeasteye.net/rss',                    'Global',        'https://www.middleeasteye.net'),
    ('Declassified UK',     'https://declassifieduk.org/feed/',                     'Investigative', 'https://declassifieduk.org'),
    ('Novara Media',        'https://novaramedia.com/feed/',                        'Left',          'https://novaramedia.com'),
    ('openDemocracy',       'https://opendemocracy.net/feed/',                      'Investigative', 'https://www.opendemocracy.net'),
    ('Bellingcat',          'https://www.bellingcat.com/feed/',                     'Investigative', 'https://www.bellingcat.com'),
    ('The Canary',          'https://thecanary.co/feed/',                           'Investigative', 'https://thecanary.co'),
    ('ProPublica',          'https://feeds.propublica.org/propublica/main',         'Investigative', 'https://www.propublica.org'),
    ('The Intercept',       'https://theintercept.com/feed/?rss',                   'Investigative', 'https://theintercept.com'),
    ('Democracy Now',       'https://www.democracynow.org/democracynow.rss',        'Independent',   'https://www.democracynow.org'),
]

PRO_SOURCE_CAP      = 3
INVESTIGATIVE_CAP   = 5

INVESTIGATIVE_SOURCES = frozenset({
    'Declassified UK',
    'openDemocracy',
    'Bellingcat',
    'The Canary',
    'ProPublica',
    'The Intercept',
})

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
    'vaccine', 'vaccination', 'virus', 'outbreak', 'epidemic', 'pandemic',
    'infection', 'disease', 'variant', 'mutation', 'pathogen', 'measles',
    'hantavirus', 'flu', 'covid', 'tuberculosis', 'mpox',
    'warning', 'nearly', 'lottery', 'stab', 'stabbing', 'shooting', 'murder',
    'rape', 'assault', 'abuse', 'missing', 'arrested', 'charged', 'sentenced',
    'jailed', 'prison', 'hostage', 'kidnap', 'explosion', 'fire', 'flood',
    'drought', 'famine', 'poverty', 'homeless', 'evicted', 'redundan',
    'unpaid', 'underpaid', 'scam', 'lawsuit', 'sued', 'suing',
    'closure', 'layoff', 'layoffs', 'hack', 'hacked', 'breach', 'breached',
    'contaminated', 'contamination', 'toxic', 'recall', 'fined', 'cover-up',
    'misconduct', 'coverup', 'exploitation', 'exploited', 'defrauded',
    'bankrupt', 'insolvent', 'receivership', 'liquidation', 'repossessed',
})

_FORCE_NEGATIVE_RE = re.compile(
    r'\b(?:unpaid|underpaid|convicted|sentenced|indicted|prosecuted|'
    r'bankrupt|insolvent|receivership|liquidat|'
    r'misconduct|defrauded|scammed|exploited|exploitation|'
    r'repossessed|foreclosed|overcharged|embezzl)\b',
    re.IGNORECASE,
)

_PRESS_RELEASE_RE = re.compile(
    r'\b(announces?|appoints?|showcases?|unveils?|rebrands?|'
    r'quarterly (?:dividend|earnings|results)|'
    r'strategic (?:partnership|alliance)|'
    r'(?:managing director|chief executive officer|chief financial officer|'
    r'chief operating officer|chief technology officer) across|'
    r'(?:names?|hires?|adds?) .{2,35} as (?:its |new )?'
    r'(?:ceo|cto|cfo|coo|managing director|head of|director of|president|vice president))\b',
    re.IGNORECASE,
)

_RESULTS_RE = re.compile(
    r'\b(winning numbers?|lotto results?|lottery results?|draw (?:on|for)|'
    r'full time|half time|match report|fixtures?|standings?)\b',
    re.IGNORECASE,
)

_OPINION_STARTERS = (
    'why ', 'opinion: ', 'comment: ', 'analysis: ',
    'view: ', 'letter: ', 'column: ',
)


def _quality_flags(title, url, compound):
    sarcasm_risk  = False
    opinion       = False
    clickbait     = False
    satire        = False
    press_release = False
    junk          = False
    tl = title.lower().strip()

    try:
        domain = urlparse(url).netloc.lower().lstrip('www.')
        if any(sd in domain for sd in SATIRE_DOMAINS):
            satire = True
    except Exception:
        pass

    if _PRESS_RELEASE_RE.search(title):
        press_release = True

    if _RESULTS_RE.search(title):
        junk = True

    if _SCARE_QUOTE_RE.search(title):
        sarcasm_risk = True

    if title.strip().endswith('?'):
        sarcasm_risk = True

    title_words = set(tl.split())
    if compound >= POSITIVE_THRESHOLD and (title_words & _NEGATIVE_CONTEXT):
        sarcasm_risk = True

    if _CLICKBAIT_RE.match(tl):
        clickbait = True

    if any(tl.startswith(m) for m in _OPINION_STARTERS):
        opinion = True

    return {
        'sarcasm_risk':  sarcasm_risk,
        'opinion':       opinion,
        'clickbait':     clickbait,
        'satire':        satire,
        'press_release': press_release,
        'junk':          junk,
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

# ── Database ──────────────────────────────────────────────────────────────────

_DB_URL = os.environ.get('DATABASE_URL', '')


def _db_conn():
    return psycopg2.connect(_DB_URL)


def _init_db():
    if not _DB_URL:
        log.warning('DATABASE_URL not set — Dispatch feature disabled')
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS dispatches (
                        id           SERIAL PRIMARY KEY,
                        url          TEXT    NOT NULL,
                        title        TEXT    NOT NULL,
                        source       TEXT,
                        compound     REAL,
                        label        TEXT,
                        sarcasm_risk BOOLEAN DEFAULT FALSE,
                        opinion      BOOLEAN DEFAULT FALSE,
                        clickbait    BOOLEAN DEFAULT FALSE,
                        submitted_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            conn.commit()
        log.info('Dispatch DB ready')
    except Exception as exc:
        log.error(f'_init_db failed: {exc}')


def _db_insert(sub):
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dispatches
                    (url, title, source, compound, label, sarcasm_risk, opinion, clickbait)
                VALUES
                    (%(url)s, %(title)s, %(source)s, %(compound)s, %(label)s,
                     %(sarcasm_risk)s, %(opinion)s, %(clickbait)s)
                RETURNING submitted_at
            """, sub)
            row = cur.fetchone()
        conn.commit()
    return row[0]


def _db_fetch(limit=8):
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    'SELECT * FROM dispatches ORDER BY submitted_at DESC LIMIT %s',
                    (limit,)
                )
                rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if hasattr(d.get('submitted_at'), 'strftime'):
                d['submitted_at'] = d['submitted_at'].strftime('%d %b %Y %H:%M UTC')
            result.append(d)
        return result
    except Exception as exc:
        log.error(f'_db_fetch failed: {exc}')
        return []


def _db_clear():
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM dispatches')
        conn.commit()


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
            if _FORCE_NEGATIVE_RE.search(title):
                compound = min(compound, NEGATIVE_THRESHOLD - 0.01)
            flags = _quality_flags(title, link, compound)

            if flags['satire'] or flags['press_release'] or flags['junk']:
                log.info(f'Excluded [{("satire" if flags["satire"] else "press_release" if flags["press_release"] else "junk")}]: {title[:60]}')
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
            if label == 'pro' and flags['sarcasm_risk']:
                flagged.append(item)
            elif label == 'pro':
                pro.append(item)
            elif label == 'con':
                con.append(item)
            else:
                flagged.append(item)
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
        _progress['feeds'] = {name: {'status': 'pending', 'count': 0} for name, _, _b, _w in FEEDS}

    results = {}
    threads = [
        threading.Thread(target=_fetch_one, args=(name, url, bias, results), daemon=True)
        for name, url, bias, _w in FEEDS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)

    all_pro, all_con, all_flagged = [], [], []
    sources = {}
    for name, _, bias, website in FEEDS:
        r = results.get(name, {'pro': [], 'con': [], 'flagged': [], 'error': True, 'bias': bias})
        cap = INVESTIGATIVE_CAP if name in INVESTIGATIVE_SOURCES else PRO_SOURCE_CAP
        capped = sorted(r['pro'], key=lambda x: x['compound'], reverse=True)[:cap]
        all_pro.extend(capped)
        all_con.extend(r['con'])
        all_flagged.extend(r['flagged'])
        sources[name] = {
            'pro':     len(r['pro']),
            'con':     len(r['con']),
            'flagged': len(r['flagged']),
            'error':   r.get('error', False),
            'bias':    bias,
            'website': website,
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
    _init_db()
    while True:
        _fetch()
        time.sleep(REFRESH_INTERVAL)


# ── Dispatch (manual submission) ─────────────────────────────────────────────

_DISPATCH_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)
_OG_TITLE_RE = re.compile(
    r'<meta\b[^>]*\bproperty=["\']og:title["\'][^>]*\bcontent=["\']([^"\'<>]{5,250})["\']'
    r'|<meta\b[^>]*\bcontent=["\']([^"\'<>]{5,250})["\'][^>]*\bproperty=["\']og:title["\']',
    re.IGNORECASE,
)
_TW_TITLE_RE = re.compile(
    r'<meta\b[^>]*\bname=["\']twitter:title["\'][^>]*\bcontent=["\']([^"\'<>]{5,250})["\']'
    r'|<meta\b[^>]*\bcontent=["\']([^"\'<>]{5,250})["\'][^>]*\bname=["\']twitter:title["\']',
    re.IGNORECASE,
)
_PAGE_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_TITLE_SUFFIX_RE = re.compile(r'\s*[\|\-–—]\s*[^|\-–—]{3,60}$')


def _extract_title(url):
    try:
        resp = requests.get(url, timeout=12, verify=False, allow_redirects=True,
                            headers={'User-Agent': _DISPATCH_UA})
        if resp.status_code >= 400:
            return None
        text = resp.text
        m = _OG_TITLE_RE.search(text)
        if m:
            return _html.unescape((m.group(1) or m.group(2)).strip())
        m = _TW_TITLE_RE.search(text)
        if m:
            return _html.unescape((m.group(1) or m.group(2)).strip())
        m = _PAGE_TITLE_RE.search(text)
        if m:
            title = _html.unescape(m.group(1).strip())
            title = _TITLE_SUFFIX_RE.sub('', title).strip()
            if len(title) >= 10:
                return title
    except Exception as exc:
        log.warning(f'_extract_title failed: {exc}')
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    with _lock:
        d = {k: v for k, v in _state.items()}
        d['pro']     = list(_state['pro'])
        d['con']     = list(_state['con'])
        d['flagged'] = list(_state['flagged'])
        d['sources'] = dict(_state['sources'])
    d['score_class']   = _score_class(d['score'])
    d['arc_path'], d['nx'], d['ny'] = _dial_arc(d['score'])
    d['description']   = _filter_description(d['score'], d['pro_count'], d['con_count'])
    d['dispatches']    = _db_fetch() if _DB_URL else []
    d['dispatch_live'] = bool(_DB_URL)
    admin_token        = os.environ.get('DISPATCH_ADMIN_TOKEN', '')
    d['admin']         = bool(admin_token and request.args.get('edit') == admin_token)
    d['admin_token']   = admin_token if d['admin'] else ''
    return render_template('index.html', **d)


@app.route('/api/dispatch')
def api_dispatch():
    return jsonify(_db_fetch() if _DB_URL else [])


def _admin_check(data):
    admin = os.environ.get('DISPATCH_ADMIN_TOKEN', '')
    return bool(admin and (data or {}).get('token') == admin)


@app.route('/api/submit', methods=['POST'])
def api_submit():
    if not _DB_URL:
        return jsonify({'error': 'Dispatch not configured'}), 503
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return jsonify({'error': 'Not authorised'}), 403
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    custom_title = data.get('custom_title', '').strip()
    if custom_title:
        title = custom_title
    else:
        title = _extract_title(url)
        if not title:
            return jsonify({'error': 'Could not read a headline from that URL'}), 422
    vs       = _analyzer.polarity_scores(title)
    compound = round(vs['compound'], 3)
    if _FORCE_NEGATIVE_RE.search(title):
        compound = min(compound, NEGATIVE_THRESHOLD - 0.01)
    flags = _quality_flags(title, url, compound)
    label = _classify(compound)
    try:
        domain = urlparse(url).netloc.lower().lstrip('www.')
    except Exception:
        domain = url
    sub = {
        'url':          url,
        'title':        title,
        'source':       domain,
        'compound':     compound,
        'label':        label,
        'sarcasm_risk': flags['sarcasm_risk'],
        'opinion':      flags['opinion'],
        'clickbait':    flags['clickbait'],
    }
    try:
        ts = _db_insert(sub)
        sub['submitted_at'] = ts.strftime('%d %b %Y %H:%M UTC') if hasattr(ts, 'strftime') else str(ts)
    except Exception as exc:
        log.error(f'Dispatch insert failed: {exc}')
        return jsonify({'error': 'Could not save — try again'}), 500
    return jsonify(sub)


@app.route('/api/dispatch/<int:item_id>', methods=['DELETE'])
def api_dispatch_delete(item_id):
    token = request.headers.get('X-Admin-Token', '')
    admin = os.environ.get('DISPATCH_ADMIN_TOKEN', '')
    if not admin or token != admin:
        return jsonify({'error': 'Not authorised'}), 403
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM dispatches WHERE id = %s', (item_id,))
            conn.commit()
    except Exception as exc:
        log.error(f'Dispatch delete failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'deleted'})


@app.route('/api/submit/clear', methods=['POST'])
def api_submit_clear():
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return jsonify({'error': 'Not authorised'}), 403
    try:
        _db_clear()
    except Exception as exc:
        log.error(f'Dispatch clear failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'cleared'})


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
