import os
import re
import math
import hmac
import hashlib
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
import stripe
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('medea')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

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
    # Global expansion
    ('Deutsche Welle',      'https://rss.dw.com/rdf/rss-en-all',                   'Europe · Global','https://www.dw.com/en/'),
    ('Euronews',            'https://www.euronews.com/rss',                         'Europe',        'https://www.euronews.com'),
    ('RFI English',         'https://www.rfi.fr/en/rss',                            'Global',        'https://www.rfi.fr/en'),
    ('RNZ Pacific',         'https://www.rnz.co.nz/rss/pacific.xml',               'Pacific',       'https://www.rnz.co.nz/international/pacific-news'),
    ('IPS News',            'https://www.ipsnews.net/feed/',                        'Global South',  'https://www.ipsnews.net'),
    ('The Conversation Africa', 'https://theconversation.com/africa/articles.atom', 'Africa',        'https://theconversation.com/africa'),
    ('Asia Times',          'https://asiatimes.com/feed/',                          'Asia',          'https://asiatimes.com'),
    ('Global Voices',       'https://globalvoices.org/feed/',                       'Latin America', 'https://globalvoices.org'),
    ('Brasil Wire',         'https://www.brasilwire.com/feed/',                     'Latin America', 'https://www.brasilwire.com'),
    ('Middle East Monitor', 'https://www.middleeastmonitor.com/feed/',               'Middle East',   'https://www.middleeastmonitor.com'),
    ('Mail & Guardian',     'https://mg.co.za/feed/',                               'Africa',        'https://mg.co.za'),
    ('The Register',        'https://www.theregister.com/headlines.atom',           'Technology',    'https://www.theregister.com'),
    ('Carbon Brief',        'https://www.carbonbrief.org/feed/',                    'Environment',   'https://www.carbonbrief.org'),
    ('Al-Monitor',          'https://www.al-monitor.com/rss',                       'Middle East',   'https://www.al-monitor.com'),
    # Finance — market intelligence, insider trades, congressional activity
    ('Seeking Alpha',       'https://seekingalpha.com/market_currents.xml',          'Finance',       'https://seekingalpha.com'),
    ('WSJ Markets',         'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',         'Finance',       'https://www.wsj.com/news/markets'),
]

PRO_SOURCE_CAP      = 4
INVESTIGATIVE_CAP   = 6

INVESTIGATIVE_SOURCES = frozenset({
    'Declassified UK',
    'openDemocracy',
    'Bellingcat',
    'The Canary',
    'ProPublica',
    'The Intercept',
    'Carbon Brief',
})

REFRESH_INTERVAL    = 1800
POSITIVE_THRESHOLD  =  0.15
NEGATIVE_THRESHOLD  = -0.05

_LAUNCH_DATE = datetime(2026, 5, 11, tzinfo=timezone.utc)

def _issue_number():
    return max(1, (datetime.now(timezone.utc) - _LAUNCH_DATE).days + 1)

_analyzer = SentimentIntensityAnalyzer()

_SESSION = requests.Session()
_SESSION.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (compatible; MeDea/2.0; +https://medea-production-dd4b.up.railway.app)'
    )
})
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
    'tragedy', 'kill', 'kills', 'killed', 'killing', 'dies', 'dead', 'suffer', 'crash', 'blast',
    'attack', 'attacks', 'attacked', 'war', 'conflict', 'riot', 'protest', 'strike',
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
    # Conflict/humanitarian — VADER misreads these as positive (relief, aid, UNICEF)
    'civilian', 'civilians', 'airstrike', 'airstrikes', 'bombing', 'bombed',
    'bombardment', 'casualty', 'casualties', 'displaced', 'siege', 'occupation',
    'massacre', 'genocide', 'ceasefire', 'truce', 'troops', 'offensive', 'shelling',
    'wounded', 'humanitarian', 'evacuate', 'evacuation', 'rubble', 'blockade',
    # Crime / violence — VADER misreads justice framing as positive
    'suspect', 'suspects', 'executed', 'execution', 'suicide', 'overdose',
    'trafficking', 'abducted', 'abduction', 'detained', 'detainee',
    'survivor', 'survivors', 'victim', 'victims',
    # Displacement and crisis
    'flee', 'fleeing', 'fled', 'refugee', 'refugees',
    # Terror — always conflict context regardless of framing
    'terror', 'terrorist', 'terrorists', 'terrorism',
})

_FORCE_NEGATIVE_RE = re.compile(
    r'\b(?:unpaid|underpaid|convicted|sentenced|indicted|prosecuted|'
    r'bankrupt|insolvent|receivership|liquidat|'
    r'misconduct|defrauded|scammed|exploited|exploitation|'
    r'repossessed|foreclosed|overcharged|embezzl|'
    r'decimate|suppress|disenfranchis|gerrymander|discriminat|segregat|'
    r'racist|racism|supremacist|neo-nazi|fascist|authoritarian|'
    # Conflict — VADER cannot be trusted here regardless of score
    r'airstrike|airstrikes|massacre|genocide|bombardment|'
    r'ethnic cleansing|war crime|civilian.{0,20}kill|'
    r'kill[s]?\s+\d|killed\s+\d|killing\s+\d|'
    r'children\s+killed|civilians\s+killed|people\s+killed)\b'
    r'|jim crow|voter suppression|voting rights',
    re.IGNORECASE,
)

# Topic tagging — first match wins
_TOPIC_RULES = [
    ('Politics',      re.compile(
        r'\b(?:election|parliament|government|party|vote[sd]?|voting|minister|cabinet|mp|senator|'
        r'congress|president|prime minister|policy|legislation|bill|democrat|republican|tory|labour|'
        r'reform|whitehall|chancellor|budget|keir|starmer|sunak|trump|biden|harris|macron|'
        r'political|politician|campaig|referendum|ballot|constituency|downing street)\b', re.I)),
    ('War & Conflict', re.compile(
        r'\b(?:war|military|troops|missile|ceasefire|nato|bomb|airstrike|invasion|occupation|'
        r'terror|isis|hamas|ukraine|russia|gaza|israel|casualt|frontline|weapon|drone|sanction|'
        r'siege|hostage|conflict|armed|peacekeep|battalion|offensive|attack)\b', re.I)),
    ('Crime & Justice', re.compile(
        r'\b(?:murder|killed|kill|arrested|convicted|sentenced|fraud|theft|scam|gang|police|'
        r'court|verdict|plea|trial|inquest|investigation|shooting|stabbing|assault|rape|'
        r'prison|jailed|custody|criminal|suspect|charged|prosecut|officer|knife|gun)\b', re.I)),
    ('Health',        re.compile(
        r'\b(?:hospital|nhs|cancer|mental health|drug|vaccine|disease|medical|patient|health|'
        r'treatment|clinical|gp|surgery|pandemic|outbreak|virus|drug|medicine|obesity|'
        r'dementia|stroke|diabetes|ambulance|a&e|emergency|wellbeing|pharmaceutical)\b', re.I)),
    ('Finance',       re.compile(
        r'\b(?:insider trading|congressional trade|sec filing|form 4|hedge fund|'
        r'private equity|venture capital|\bipo\b|earnings report|quarterly results|'
        r'merger|acquisition|short sell|activist investor|proxy fight|'
        r'nasdaq|nyse|ftse|s&p 500|dow jones|\bforex\b|commodity|commodities|'
        r'equities|portfolio|derivatives|options|futures|yield curve|bond market|'
        r'wall street|seeking alpha|market cap|share price|stock price)\b', re.I)),
    ('Economy',       re.compile(
        r'\b(?:inflation|gdp|recession|bank|interest rate|mortgage|housing|wages|cost of living|'
        r'economy|market|budget|tax|trade|tariff|investment|growth|poverty|unemployment|'
        r'price|earn|salary|pension|debt|deficit|sterling|pound|dollar|oil|energy bill)\b', re.I)),
    ('Environment',   re.compile(
        r'\b(?:climate|carbon|emission|flood|wildfire|energy|fossil|renewable|solar|wind|'
        r'green|biodiversity|extinction|deforest|pollution|net zero|plastic|ocean|species|'
        r'drought|storm|heatwave|glacier|coal|gas|nuclear|sustainability)\b', re.I)),
    ('Technology',    re.compile(
        r'\b(?:artificial intelligence|\bai\b|algorithm|data breach|cybersecurity|hack|'
        r'social media|surveillance|tech|digital|robot|automation|software|silicon|'
        r'smartphone|app|internet|cloud|quantum|chip|semiconductor|deepfake|openai|'
        r'google|microsoft|apple|meta|amazon|elon|musk|twitter|x\.com)\b', re.I)),
    ('Education',     re.compile(
        r'\b(?:school|university|college|student|teacher|ofsted|exam|curriculum|degree|'
        r'graduate|tuition|literacy|academy|pupil|headteacher|admissions|league table|sats)\b', re.I)),
    ('Human Rights',  re.compile(
        r'\b(?:rights|protest|discriminat|equality|refugee|asylum|immigrant|freedom|'
        r'civil liberties|oppression|jim crow|voter suppression|decimate|racism|racist|'
        r'genocide|apartheid|detention|deportation|diversity|inclusion|lgbt|trans|gender)\b', re.I)),
]


def _get_topic(title):
    for topic, pattern in _TOPIC_RULES:
        if pattern.search(title):
            return topic
    return 'General'


# Known paywalled domains — archive.is link shown alongside original
_PAYWALL_DOMAINS = frozenset({
    'nikkei.com', 'asia.nikkei.com',
    'ft.com',
    'thetimes.co.uk', 'thetimes.com',
    'telegraph.co.uk',
    'newstatesman.com',
    'economist.com',
    'bloomberg.com',
    'wsj.com',
    'washingtonpost.com',
    'nytimes.com',
    'theathletic.com',
    'theinformation.com',
})

# Domains that always score maximum positive — no pipeline, no filters
_PINNED_PRO = frozenset({
    'the-architect-neo.github.io',
})

# Framing risk — headline scores positive but contains a structural falsehood
# Moves pro stories to Held for editorial judgement. Extend as patterns emerge.
_FRAMING_RISK_RE = re.compile(
    # Non-European countries in Eurovision
    r'\b(?:canada|australia|new zealand|japan|china|usa|united states|brazil|india|'
    r'south korea|kazakhstan|kosovo)\b.{0,80}\beurovision\b'
    r'|\beurovision\b.{0,80}\b(?:canada|australia|new zealand|japan|china|usa|'
    r'united states|brazil|india|south korea|kazakhstan|kosovo)\b'
    # Add new patterns below — one per line, pipe-separated
    ,
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
    framing_risk  = False
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

    if _FRAMING_RISK_RE.search(title):
        framing_risk = True

    # War & Conflict headlines almost never belong in the pro feed.
    # Require compound >= 0.5 (strongly positive) — below that, flag for review.
    if compound < 0.5 and _get_topic(title) == 'War & Conflict':
        sarcasm_risk = True

    return {
        'sarcasm_risk':  sarcasm_risk,
        'opinion':       opinion,
        'clickbait':     clickbait,
        'satire':        satire,
        'press_release': press_release,
        'junk':          junk,
        'framing_risk':  framing_risk,
    }


# ── Feed quality utilities ────────────────────────────────────────────────────

_STOPWORDS = frozenset({
    'the','a','an','in','on','at','to','for','of','and','or','is','are',
    'was','were','has','have','had','with','from','by','as','its','it',
    'be','been','but','not','this','that','they','he','she','we','you',
    'after','over','how','who','what','when','where','why','could','says',
    'said','will','new','up','out','over','into','about','more','than','all',
})


def _title_words(title):
    return set(title.lower().split()) - _STOPWORDS


def _deduplicate(items):
    seen, result = [], []
    for item in items:
        words = _title_words(item['title'])
        dup = False
        for s in seen:
            if words and s:
                overlap = len(words & s) / len(words | s)
                if overlap >= 0.48:
                    dup = True
                    break
        if not dup:
            seen.append(words)
            result.append(item)
    return result


def _ago(ts):
    if not ts:
        return ''
    diff = time.time() - ts
    if diff < 60:
        return 'just now'
    if diff < 3600:
        return f'{int(diff/60)}m ago'
    if diff < 86400:
        return f'{int(diff/3600)}h ago'
    return f'{int(diff/86400)}d ago'


def _recency_weight(pub_ts):
    if not pub_ts:
        return 0.75
    age_h = (time.time() - pub_ts) / 3600
    return max(0.3, 1.0 - age_h / 36)


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
_source_errors = {}  # {name: consecutive_error_count}

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
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS opinion_says (
                        id         SERIAL PRIMARY KEY,
                        body       TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS page_views (
                        id         SERIAL PRIMARY KEY,
                        visited_at TIMESTAMPTZ DEFAULT NOW(),
                        device     TEXT,
                        ip_hash    TEXT
                    )
                """)
                cur.execute("""
                    ALTER TABLE page_views ADD COLUMN IF NOT EXISTS ip_hash TEXT
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ads (
                        id         SERIAL PRIMARY KEY,
                        slot       TEXT NOT NULL DEFAULT 'main',
                        advertiser TEXT,
                        url        TEXT NOT NULL,
                        headline   TEXT,
                        strapline  TEXT,
                        cta        TEXT,
                        bg_color   TEXT DEFAULT '#0f0f0f',
                        accent     TEXT DEFAULT '#c9a84c',
                        image_url  TEXT,
                        active     BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS image_url TEXT")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS shoutouts (
                        id              SERIAL PRIMARY KEY,
                        message         TEXT NOT NULL,
                        sender_name     TEXT,
                        sender_email    TEXT,
                        stripe_session  TEXT,
                        paid            BOOLEAN DEFAULT FALSE,
                        replied         BOOLEAN DEFAULT FALSE,
                        created_at      TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ad_impressions (
                        id      SERIAL PRIMARY KEY,
                        ad_id   INTEGER,
                        slot    TEXT,
                        seen_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS story_clicks (
                        id         SERIAL PRIMARY KEY,
                        url_hash   TEXT,
                        topic      TEXT,
                        source     TEXT,
                        clicked_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sponsors (
                        id           SERIAL PRIMARY KEY,
                        platform     TEXT    NOT NULL,
                        sponsor_name TEXT,
                        tier_name    TEXT,
                        amount_cents INTEGER DEFAULT 0,
                        event_type   TEXT,
                        sponsored_at TIMESTAMPTZ DEFAULT NOW(),
                        fulfilled    BOOLEAN DEFAULT FALSE,
                        notes        TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS score_history (
                        id          SERIAL PRIMARY KEY,
                        score       INTEGER,
                        pro_count   INTEGER,
                        con_count   INTEGER,
                        total       INTEGER,
                        recorded_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS subscribers (
                        id            SERIAL PRIMARY KEY,
                        email         TEXT UNIQUE NOT NULL,
                        confirmed     BOOLEAN DEFAULT FALSE,
                        subscribed_at TIMESTAMPTZ DEFAULT NOW()
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
            try:
                domain = urlparse(d.get('url', '')).netloc.lower().lstrip('www.')
                if domain in _PINNED_PRO:
                    d['compound']     = 1.0
                    d['label']        = 'pro'
                    d['sarcasm_risk'] = False
            except Exception:
                pass
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


def _record_score(score, pro_count, con_count, total):
    if not _DB_URL or score is None:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO score_history (score, pro_count, con_count, total) VALUES (%s,%s,%s,%s)',
                    (score, pro_count, con_count, total)
                )
            conn.commit()
    except Exception as exc:
        log.error(f'_record_score: {exc}')


def _get_score_history():
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT DATE(recorded_at AT TIME ZONE 'UTC') AS day,
                           ROUND(AVG(score))::int               AS score
                    FROM score_history
                    WHERE recorded_at >= NOW() - INTERVAL '7 days'
                      AND score IS NOT NULL
                    GROUP BY day
                    ORDER BY day ASC
                """)
                rows = cur.fetchall()
        return [{'day': str(r['day']), 'score': r['score']} for r in rows]
    except Exception as exc:
        log.error(f'_get_score_history: {exc}')
        return []


def _get_sponsors(limit=10, unfulfilled_only=False):
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if unfulfilled_only:
                    cur.execute(
                        "SELECT * FROM sponsors WHERE event_type='created' AND fulfilled=FALSE ORDER BY sponsored_at DESC LIMIT %s",
                        (limit,)
                    )
                else:
                    cur.execute(
                        "SELECT * FROM sponsors WHERE event_type='created' ORDER BY sponsored_at DESC LIMIT %s",
                        (limit,)
                    )
                rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if hasattr(d.get('sponsored_at'), 'strftime'):
                d['sponsored_at'] = d['sponsored_at'].strftime('%d %b %Y')
            result.append(d)
        return result
    except Exception as exc:
        log.error(f'_get_sponsors failed: {exc}')
        return []


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
            try:
                pp = entry.get('published_parsed') or entry.get('updated_parsed')
                pub_ts = time.mktime(pp) if pp else None
            except Exception:
                pub_ts = None
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
                'framing_risk': flags['framing_risk'],
                'opinion':      flags['opinion'],
                'clickbait':    flags['clickbait'],
                'topic':        _get_topic(title),
                'paywalled':    any(pd in link for pd in _PAYWALL_DOMAINS),
                'pub_ts':       pub_ts,
                'pub_ago':      _ago(pub_ts),
            }
            label = _classify(compound)
            if label == 'pro' and (flags['sarcasm_risk'] or flags['framing_risk']):
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
        has_error = r.get('error', False)
        if has_error:
            _source_errors[name] = _source_errors.get(name, 0) + 1
        else:
            _source_errors[name] = 0
        sources[name] = {
            'pro':             len(r['pro']),
            'con':             len(r['con']),
            'flagged':         len(r['flagged']),
            'error':           has_error,
            'consecutive_err': _source_errors.get(name, 0),
            'bias':            bias,
            'website':         website,
        }

    # Deduplicate across all pools
    all_pro     = _deduplicate(all_pro)
    all_con     = _deduplicate(all_con)
    all_flagged = _deduplicate(all_flagged)

    scored = len(all_pro) + len(all_con)
    score = round(len(all_pro) / scored * 100) if scored else None
    log.info(f'Fetch complete — Pro:{len(all_pro)} Con:{len(all_con)} Flagged:{len(all_flagged)} Score:{score}')

    # Sort pro by recency-weighted compound (freshest high-quality stories first)
    all_pro.sort(key=lambda x: x['compound'] * _recency_weight(x.get('pub_ts')), reverse=True)
    all_con.sort(key=lambda x: x['compound'])

    with _lock:
        _state.update({
            'score':         score,
            'pro':           all_pro[:20],
            'con':           all_con[:14],
            'flagged':       all_flagged[:10],
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

    _record_score(score, len(all_pro), len(all_con), len(all_pro) + len(all_con) + len(all_flagged))


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
        # Deep-copy story lists — each request gets independent dicts.
        # Prevents any downstream mutation leaking into shared _state.
        d['pro']     = [dict(s) for s in _state['pro']]
        d['con']     = [dict(s) for s in _state['con']]
        d['flagged'] = [dict(s) for s in _state['flagged']]
        d['sources'] = {k: dict(v) for k, v in _state['sources'].items()}
    d['score_class']   = _score_class(d['score'])
    d['arc_path'], d['nx'], d['ny'] = _dial_arc(d['score'])
    d['description']   = _filter_description(d['score'], d['pro_count'], d['con_count'])
    d['dispatches']           = _db_fetch() if _DB_URL else []
    d['dispatch_live']        = bool(_DB_URL)
    d['admin']                = bool(session.get('admin'))
    d['admin_token']          = ''
    d['sponsors']             = _get_sponsors(limit=12) if _DB_URL else []
    d['unfulfilled_sponsors'] = _get_sponsors(limit=50, unfulfilled_only=True) if (_DB_URL and d['admin']) else []
    d['shoutouts']            = _get_shoutouts(limit=20, unpaid=True) if (_DB_URL and d['admin']) else []
    d['stripe_live']          = bool(_STRIPE_KEY)
    d['opinion']              = _get_opinion()
    d['traffic']              = _get_traffic() if d['admin'] else None
    d['total_visitors']       = _get_total_visitors() if _DB_URL else 0
    d['ad_top']               = _get_ad('top')
    d['ad_mid']               = _get_ad('mid')
    d['ad_bottom']            = _get_ad('bottom')
    d['ad_sidebar']           = _get_ad('sidebar')
    d['ad_feed']              = _get_ad('feed')
    d['ad_top_list']          = _get_ads_for_slot('top')
    d['ad_mid_list']          = _get_ads_for_slot('mid')
    d['ad_bottom_list']       = _get_ads_for_slot('bottom')
    d['ad_sidebar_list']      = _get_ads_for_slot('sidebar')
    d['ad_feed_list']         = _get_ads_for_slot('feed')
    # Geo-feed
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    cc = session.get('geo_country')
    if not cc:
        cc = _get_country(ip)
        session['geo_country'] = cc
    d['regional_stories'], d['country_code'] = _get_regional_stories(
        cc, d['pro'], d['con'], d['flagged']
    )
    d['country_name']  = _country_name(cc)
    d['issue_number']  = _issue_number()
    _log_pageview()
    return render_template('index.html', **d)


@app.route('/api/dispatch')
def api_dispatch():
    return jsonify(_db_fetch() if _DB_URL else [])


def _get_opinion():
    if not _DB_URL:
        return None
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute('SELECT * FROM opinion_says ORDER BY updated_at DESC LIMIT 1')
                row = cur.fetchone()
        if row:
            d = dict(row)
            if hasattr(d.get('updated_at'), 'strftime'):
                d['updated_at'] = d['updated_at'].strftime('%d %b %Y %H:%M UTC')
            return d
    except Exception as exc:
        log.error(f'_get_opinion failed: {exc}')
    return None


_login_attempts  = {}
_login_lock      = threading.Lock()


def _login_rate_ok(ip):
    now = time.time()
    with _login_lock:
        times = [t for t in _login_attempts.get(ip, []) if now - t < 900]
        if len(times) >= 5:
            return False
        times.append(now)
        _login_attempts[ip] = times
        return True


def _ip_hash(ip):
    from datetime import date as _date
    salt = _date.today().isoformat()
    return hashlib.sha256(f'{ip}:{salt}'.encode()).hexdigest()[:32]


def _log_pageview():
    if not _DB_URL:
        return
    try:
        ip     = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        ih     = _ip_hash(ip)
        ua     = (request.headers.get('User-Agent') or '').lower()
        device = 'mobile' if any(m in ua for m in ('mobile', 'android', 'iphone', 'ipad')) else 'desktop'
        with _db_conn() as conn:
            with conn.cursor() as cur:
                # Check if this IP+device already has a record today
                cur.execute("""
                    SELECT id FROM page_views
                    WHERE ip_hash = %s AND device = %s AND visited_at >= CURRENT_DATE
                    LIMIT 1
                """, (ih, device))
                row = cur.fetchone()
                if row:
                    # Refresh timestamp — keeps unique visitor count accurate
                    # but 'readers now' query sees the current visit
                    cur.execute("UPDATE page_views SET visited_at = NOW() WHERE id = %s", (row[0],))
                else:
                    cur.execute("INSERT INTO page_views (device, ip_hash) VALUES (%s, %s)", (device, ih))
            conn.commit()
    except Exception:
        pass


_SELF_PROMOS = {
    'top': {
        'advertiser': 'Investigator',
        'url':        'https://investigator-production-3d5b.up.railway.app',
        'headline':   'Investigator',
        'strapline':  'Fraud intelligence and investigative tools. Built by a practitioner for practitioners.',
        'cta':        'Open the tool →',
        'bg_color':   '#1a1a2e',
        'accent':     '#e94560',
        'self_promo': True,
    },
    'mid': {
        'advertiser': 'YCG',
        'url':        'https://yourcommsgroup.com',
        'headline':   'YCG',
        'strapline':  'Business telecoms. SIMs, VoIP, leased lines, IoT, MDM. One partner. No complexity.',
        'cta':        'Talk to us →',
        'bg_color':   '#111111',
        'accent':     '#CC0201',
        'self_promo': True,
    },
    'bottom': {
        'advertiser': 'QuantumProtect',
        'url':        'https://quantumprotection-production.up.railway.app',
        'headline':   'QuantumProtect',
        'strapline':  'Post-quantum cryptography. Protect your data against the threats already coming.',
        'cta':        'Explore free →',
        'bg_color':   '#0f1a2e',
        'accent':     '#2a5f43',
        'self_promo': True,
    },
    'sidebar': {
        'advertiser': 'MeDea',
        'url':        'https://medea-production-dd4b.up.railway.app',
        'headline':   'MeDea',
        'strapline':  'Signal over noise. 32 global sources. Scored hourly. On every phone on earth.',
        'cta':        'Share MeDea →',
        'bg_color':   '#1c1a16',
        'accent':     '#2a5f43',
        'image_url':  None,
        'self_promo': True,
    },
    'feed': {
        'advertiser': 'NetSecure',
        'url':        'https://the-architect-neo.github.io/',
        'headline':   'NetSecure',
        'strapline':  'Enterprise network security for organisations that cannot afford to be breached.',
        'cta':        'Learn more →',
        'bg_color':   '#0f1a2e',
        'accent':     '#4a9eff',
        'self_promo': True,
    },
}


def _get_ad(slot='mid'):
    ads = _get_ads_for_slot(slot)
    return ads[0] if ads else _SELF_PROMOS.get(slot, _SELF_PROMOS['mid'])


def _get_ads_for_slot(slot):
    if not _DB_URL:
        return [_SELF_PROMOS.get(slot, _SELF_PROMOS['mid'])]
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM ads WHERE slot=%s AND active=TRUE ORDER BY created_at ASC",
                    (slot,)
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else [_SELF_PROMOS.get(slot, _SELF_PROMOS['mid'])]
    except Exception:
        return [_SELF_PROMOS.get(slot, _SELF_PROMOS['mid'])]


# ── Geo-feed ──────────────────────────────────────────────────────────────────

_REGION_SOURCES = {
    'GB': ['The Guardian','Sky News','The Independent','Byline Times','Declassified UK','New Statesman','Novara Media','UnHerd','The Canary'],
    'IE': ['The Guardian','openDemocracy','Byline Times'],
    'US': ['ProPublica','The Intercept','Democracy Now'],
    'CA': ['ProPublica','The Intercept','Democracy Now'],
    'AU': ['RNZ Pacific','The Conversation UK'],
    'NZ': ['RNZ Pacific'],
    'ZA': ['Mail & Guardian','The Conversation Africa'],
    'NG': ['The Conversation Africa','IPS News'],
    'KE': ['The Conversation Africa','IPS News'],
    'GH': ['The Conversation Africa','IPS News'],
    'ET': ['The Conversation Africa','IPS News'],
    'IN': ['Asia Times','IPS News'],
    'PK': ['Asia Times','IPS News'],
    'JP': ['Asia Times'],
    'SG': ['Asia Times'],
    'PH': ['Asia Times'],
    'ID': ['Asia Times'],
    'TH': ['Asia Times'],
    'AR': ['Buenos Aires Herald','Brasil Wire'],
    'BR': ['Brasil Wire','Buenos Aires Herald'],
    'MX': ['Buenos Aires Herald','IPS News'],
    'CO': ['IPS News','Buenos Aires Herald'],
    'CL': ['Buenos Aires Herald','IPS News'],
    'IL': ['Middle East Monitor','Al-Monitor'],
    'PS': ['Middle East Monitor','Middle East Eye'],
    'EG': ['Middle East Monitor','Middle East Eye'],
    'SA': ['Middle East Monitor'],
    'JO': ['Middle East Monitor','Al-Monitor'],
    'LB': ['Middle East Eye','Al-Monitor'],
    'IQ': ['Middle East Eye','Al-Monitor'],
    'UA': ['Bellingcat','openDemocracy','Deutsche Welle'],
    'RU': ['Bellingcat','openDemocracy'],
    'FR': ['RFI English','Euronews','Deutsche Welle'],
    'DE': ['Deutsche Welle','Euronews'],
    'IT': ['Euronews','Deutsche Welle'],
    'ES': ['Euronews','Deutsche Welle'],
    'PL': ['Deutsche Welle','openDemocracy'],
    'SE': ['Deutsche Welle','Euronews'],
    'NO': ['Deutsche Welle','Euronews'],
    'NL': ['Deutsche Welle','Euronews'],
    'BE': ['Euronews','Deutsche Welle'],
    'CH': ['Deutsche Welle','Euronews'],
    'AT': ['Deutsche Welle','Euronews'],
    'CR': ['Buenos Aires Herald','IPS News'],  # Costa Rica — nod of the hat
}

_GEO_SESSION = requests.Session()
_GEO_SESSION.headers.update({'User-Agent': 'MeDea/2.0 geo'})


def _get_country(ip):
    if not ip or ip in ('127.0.0.1', '::1', '0.0.0.0'):
        return 'XX'
    try:
        resp = _GEO_SESSION.get(
            f'http://ip-api.com/json/{ip}?fields=countryCode',
            timeout=2,
        )
        return resp.json().get('countryCode', 'XX') or 'XX'
    except Exception:
        return 'XX'


def _get_regional_stories(country_code, pro, con, flagged, limit=6):
    sources = _REGION_SOURCES.get(country_code, [])
    if not sources:
        return [], 'XX'

    r_pro  = [s for s in pro     if s.get('source') in sources]
    r_flag = [s for s in flagged if s.get('source') in sources]
    r_con  = [s for s in con     if s.get('source') in sources]

    # Promote high-confidence flagged stories to pro for regional display.
    # sarcasm_risk is too conservative for regional good news — lead with positive.
    promoted  = []
    held_rest = []
    for s in r_flag:
        if s.get('compound', 0) >= 0.2:
            sc = dict(s)       # copy — never mutate main state
            sc['label'] = 'pro'
            promoted.append(sc)
        else:
            held_rest.append(s)

    pos_pool = sorted(r_pro + promoted, key=lambda x: x.get('compound', 0), reverse=True)
    neg_pool = sorted(held_rest,        key=lambda x: x.get('compound', 0), reverse=True)
    con_pool = sorted(r_con,            key=lambda x: x.get('compound', 0))

    ordered = (pos_pool + neg_pool + con_pool)[:limit]
    return ordered, country_code


def _country_name(cc):
    names = {
        'GB':'United Kingdom','IE':'Ireland','US':'United States','CA':'Canada',
        'AU':'Australia','NZ':'New Zealand','ZA':'South Africa','NG':'Nigeria',
        'KE':'Kenya','GH':'Ghana','ET':'Ethiopia','IN':'India','PK':'Pakistan',
        'JP':'Japan','SG':'Singapore','PH':'Philippines','ID':'Indonesia','TH':'Thailand',
        'AR':'Argentina','BR':'Brazil','MX':'Mexico','CO':'Colombia','CL':'Chile',
        'IL':'Israel','PS':'Palestine','EG':'Egypt','SA':'Saudi Arabia','JO':'Jordan',
        'LB':'Lebanon','IQ':'Iraq','UA':'Ukraine','RU':'Russia','FR':'France',
        'DE':'Germany','IT':'Italy','ES':'Spain','PL':'Poland','SE':'Sweden',
        'NO':'Norway','NL':'Netherlands','BE':'Belgium','CH':'Switzerland','AT':'Austria',
        'CR':'Costa Rica',
    }
    return names.get(cc, cc)


def _get_total_visitors():
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM page_views')
                return cur.fetchone()[0] or 0
    except Exception:
        return 0


def _get_traffic():
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE visited_at >= NOW() - INTERVAL '1 day')  AS today,
                        COUNT(*) FILTER (WHERE visited_at >= NOW() - INTERVAL '7 days') AS week,
                        COUNT(*)                                                          AS total,
                        COUNT(*) FILTER (WHERE device = 'mobile')                        AS mobile,
                        COUNT(*) FILTER (WHERE device = 'desktop')                       AS desktop
                    FROM page_views
                """)
                row = cur.fetchone()
        return {'today': row[0], 'week': row[1], 'total': row[2],
                'mobile': row[3], 'desktop': row[4]}
    except Exception:
        return None


def _admin_check(data=None):
    if session.get('admin'):
        return True
    if data:
        admin = os.environ.get('DISPATCH_ADMIN_TOKEN', '')
        return bool(admin and data.get('token') == admin)
    return False


def _auth_error():
    return jsonify({'error': 'Session expired — please log in again', 'redirect': '/login'}), 403


@app.route('/api/submit', methods=['POST'])
def api_submit():
    if not _DB_URL:
        return jsonify({'error': 'Dispatch not configured'}), 503
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    try:
        submit_domain = urlparse(url).netloc.lower().lstrip('www.')
    except Exception:
        submit_domain = ''
    custom_title = data.get('custom_title', '').strip()
    if custom_title:
        title = custom_title
    else:
        title = _extract_title(url)
        if not title:
            return jsonify({'error': 'Could not read a headline from that URL'}), 422
    if submit_domain in _PINNED_PRO:
        compound = 1.0
        label    = 'pro'
        flags    = {'sarcasm_risk': False, 'opinion': False, 'clickbait': False,
                    'satire': False, 'press_release': False, 'junk': False}
    else:
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
    if not _admin_check():
        return _auth_error()
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
        return _auth_error()
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


@app.route('/api/score/history')
def api_score_history():
    return jsonify(_get_score_history() if _DB_URL else [])


@app.route('/api/subscribe', methods=['POST'])
def api_subscribe():
    if not _DB_URL:
        return jsonify({'error': 'Not available'}), 503
    data  = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    if not email or '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'Valid email address required'}), 400
    if len(email) > 254:
        return jsonify({'error': 'Email too long'}), 400
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO subscribers (email) VALUES (%s) ON CONFLICT (email) DO NOTHING',
                    (email,)
                )
                inserted = cur.rowcount
            conn.commit()
        if inserted:
            log.info(f'New subscriber: {email[:4]}...{email.split("@")[1]}')
            return jsonify({'status': 'subscribed'})
        return jsonify({'status': 'already_subscribed'})
    except Exception as exc:
        log.error(f'subscribe: {exc}')
        return jsonify({'error': 'Could not save — try again'}), 500


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


# ── PWA ───────────────────────────────────────────────────────────────────────

@app.route('/manifest.json')
def pwa_manifest():
    from flask import make_response
    import json
    manifest = {
        'name':             'MeDea — News worth reading',
        'short_name':       'MeDea',
        'description':      'Signal over noise. The news that matters.',
        'start_url':        '/',
        'scope':            '/',
        'display':          'standalone',
        'background_color': '#f6f1e6',
        'theme_color':      '#1c1a16',
        'orientation':      'portrait-primary',
        'categories':       ['news'],
        'lang':             'en-GB',
        'icons': [
            {
                'src':     '/static/icons/icon.svg',
                'sizes':   'any',
                'type':    'image/svg+xml',
                'purpose': 'any maskable',
            }
        ],
        'screenshots': [],
        'related_applications': [],
        'prefer_related_applications': False,
    }
    resp = make_response(json.dumps(manifest, indent=2))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@app.route('/sw.js')
def service_worker():
    from flask import make_response
    sw = """
const CACHE = 'medea-v2';
const OFFLINE = '/offline';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(['/', OFFLINE])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
    return;
  }
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""
    resp = make_response(sw.strip())
    resp.headers['Content-Type']         = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.route('/offline')
def offline():
    return render_template('offline.html')


@app.route('/about')
def about():
    with _lock:
        source_count = len(_state['sources'])
        last_updated = _state['last_updated']
    return render_template('about.html', source_count=source_count, last_updated=last_updated)


# ── Intelligence layer ───────────────────────────────────────────────────────

@app.route('/api/impression', methods=['POST'])
def api_impression():
    if not _DB_URL:
        return jsonify({'ok': True})
    data  = request.get_json(silent=True) or {}
    ad_id = data.get('ad_id')
    slot  = data.get('slot', '')
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO ad_impressions (ad_id, slot) VALUES (%s,%s)', (ad_id, slot))
            conn.commit()
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/click', methods=['POST'])
def api_click():
    if not _DB_URL:
        return jsonify({'ok': True})
    data  = request.get_json(silent=True) or {}
    url   = data.get('url', '')
    topic = data.get('topic', 'General')
    src   = data.get('source', '')
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24] if url else None
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO story_clicks (url_hash, topic, source) VALUES (%s,%s,%s)',
                    (url_hash, topic, src)
                )
            conn.commit()
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/analytics')
def api_analytics():
    if not session.get('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if not _DB_URL:
        return jsonify({})
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                # Impressions today
                cur.execute("SELECT COUNT(*) FROM ad_impressions WHERE seen_at >= CURRENT_DATE")
                imp_today = cur.fetchone()[0]
                # Impressions this week
                cur.execute("SELECT COUNT(*) FROM ad_impressions WHERE seen_at >= NOW() - INTERVAL '7 days'")
                imp_week = cur.fetchone()[0]
                # Top ad today
                cur.execute("""
                    SELECT a.advertiser, a.slot, COUNT(*) as c
                    FROM ad_impressions i
                    JOIN ads a ON a.id = i.ad_id
                    WHERE i.seen_at >= CURRENT_DATE
                    GROUP BY a.advertiser, a.slot
                    ORDER BY c DESC LIMIT 1
                """)
                top_ad = cur.fetchone()
                # Top topic today (story clicks)
                cur.execute("""
                    SELECT topic, COUNT(*) as c FROM story_clicks
                    WHERE clicked_at >= CURRENT_DATE
                    GROUP BY topic ORDER BY c DESC LIMIT 1
                """)
                top_topic = cur.fetchone()
                # Story clicks today
                cur.execute("SELECT COUNT(*) FROM story_clicks WHERE clicked_at >= CURRENT_DATE")
                clicks_today = cur.fetchone()[0]
                # Readers now (last 10 min)
                cur.execute("""
                    SELECT COUNT(DISTINCT ip_hash) FROM page_views
                    WHERE visited_at >= NOW() - INTERVAL '10 minutes'
                """)
                readers_now = cur.fetchone()[0]
                # Active sources this cycle
                with _lock:
                    sources_live = sum(1 for s in _state['sources'].values() if not s.get('error'))
                    sources_total = len(_state['sources'])
        return jsonify({
            'imp_today':    imp_today,
            'imp_week':     imp_week,
            'clicks_today': clicks_today,
            'readers_now':  readers_now,
            'sources_live': sources_live,
            'sources_total': sources_total,
            'top_ad':    {'name': top_ad[0], 'slot': top_ad[1], 'count': top_ad[2]} if top_ad else None,
            'top_topic': {'topic': top_topic[0], 'count': top_topic[1]} if top_topic else None,
            'score':     _state.get('score'),
            'est_revenue_today': round(imp_today / 1000 * 5, 2),  # £5 CPM estimate
        })
    except Exception as exc:
        log.error(f'analytics: {exc}')
        return jsonify({'error': str(exc)}), 500


# ── Ad management ────────────────────────────────────────────────────────────

@app.route('/api/ad', methods=['POST'])
def api_ad_save():
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    url  = data.get('url', '').strip()
    slot = data.get('slot', 'mid').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                # Count active ads in slot — cap at 5 per slot
                cur.execute("SELECT COUNT(*) FROM ads WHERE slot=%s AND active=TRUE", (slot,))
                count = cur.fetchone()[0]
                if count >= 5:
                    return jsonify({'error': 'Slot full — 5 advertisers maximum. Remove one first.'}), 400
                cur.execute("""
                    INSERT INTO ads (slot, advertiser, url, headline, strapline, cta, bg_color, accent, image_url, active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                """, (
                    slot,
                    data.get('advertiser',''),
                    url,
                    data.get('headline',''),
                    data.get('strapline',''),
                    data.get('cta','Visit'),
                    data.get('bg_color','#0f0f0f'),
                    data.get('accent','#c9a84c'),
                    data.get('image_url') or None,
                ))
            conn.commit()
    except Exception as exc:
        log.error(f'Ad save failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'saved'})


@app.route('/api/ad/<int:ad_id>', methods=['DELETE'])
def api_ad_delete(ad_id):
    if not _admin_check():
        return _auth_error()
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE ads SET active=FALSE WHERE id=%s', (ad_id,))
            conn.commit()
    except Exception as exc:
        log.error(f'Ad delete: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'removed'})


@app.route('/api/ad/clear', methods=['POST'])
def api_ad_clear():
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    slot = (request.get_json(silent=True) or {}).get('slot', 'mid')
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE ads SET active=FALSE WHERE slot=%s', (slot,))
            conn.commit()
    except Exception as exc:
        log.error(f'Ad clear failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'cleared'})


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin'):
        return redirect(url_for('index'))
    error = False
    if request.method == 'POST':
        ip    = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        token = request.form.get('token', '').strip()
        admin = os.environ.get('DISPATCH_ADMIN_TOKEN', '')
        if not _login_rate_ok(ip):
            error = True
        elif admin and token == admin:
            session['admin']    = True
            session.permanent   = True
            app.permanent_session_lifetime = __import__('datetime').timedelta(days=30)
            return redirect(url_for('index'))
        else:
            error = True
            log.warning(f'Failed login attempt from {ip}')
    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── OpinionSays ───────────────────────────────────────────────────────────────

@app.route('/api/opinion', methods=['POST'])
def api_opinion_save():
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    body = data.get('body', '').strip()
    if not body:
        return jsonify({'error': 'No text provided'}), 400
    if len(body) > 1000:
        return jsonify({'error': 'Too long — 1000 characters max'}), 400
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM opinion_says')
                cur.execute('INSERT INTO opinion_says (body) VALUES (%s)', (body,))
            conn.commit()
    except Exception as exc:
        log.error(f'opinion save failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'saved', 'body': body})


@app.route('/api/opinion/clear', methods=['POST'])
def api_opinion_clear():
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM opinion_says')
            conn.commit()
    except Exception as exc:
        log.error(f'opinion clear failed: {exc}')
        return jsonify({'error': 'Database error'}), 500
    return jsonify({'status': 'cleared'})


# ── Shoutouts (Stripe) ───────────────────────────────────────────────────────

_STRIPE_KEY     = os.environ.get('STRIPE_SECRET_KEY', '')
_STRIPE_WEBHOOK = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
_SHOUTOUT_PRICE = 150  # pence (£1.50)

if _STRIPE_KEY:
    stripe.api_key = _STRIPE_KEY


def _get_shoutouts(limit=20, unpaid=False):
    try:
        with _db_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if unpaid:
                    cur.execute(
                        "SELECT * FROM shoutouts WHERE paid=TRUE AND replied=FALSE ORDER BY created_at DESC LIMIT %s",
                        (limit,)
                    )
                else:
                    cur.execute(
                        "SELECT * FROM shoutouts WHERE paid=TRUE ORDER BY created_at DESC LIMIT %s",
                        (limit,)
                    )
                rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if hasattr(d.get('created_at'), 'strftime'):
                d['created_at'] = d['created_at'].strftime('%d %b %Y %H:%M')
            result.append(d)
        return result
    except Exception as exc:
        log.error(f'_get_shoutouts: {exc}')
        return []


@app.route('/api/shoutout/checkout', methods=['POST'])
def api_shoutout_checkout():
    if not _STRIPE_KEY:
        return jsonify({'error': 'Payment not configured yet'}), 503
    data         = request.get_json(silent=True) or {}
    message      = (data.get('message') or '').strip()[:280]
    sender_name  = (data.get('name') or '').strip()[:80]
    sender_email = (data.get('email') or '').strip()[:120]
    if not message:
        return jsonify({'error': 'Message required'}), 400
    try:
        base = request.host_url.rstrip('/')
        checkout = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'gbp',
                    'product_data': {
                        'name': 'MeDea — Direct message to The Architect',
                        'description': 'Your message is delivered personally. Every one is read.',
                    },
                    'unit_amount': _SHOUTOUT_PRICE,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=base + '/shoutout/success?sid={CHECKOUT_SESSION_ID}',
            cancel_url=base + '/#shoutout',
            metadata={
                'message':      message,
                'sender_name':  sender_name,
                'sender_email': sender_email,
            },
        )
        # Pre-store unpaid record
        if _DB_URL:
            with _db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shoutouts (message, sender_name, sender_email, stripe_session, paid)
                        VALUES (%s,%s,%s,%s,FALSE)
                    """, (message, sender_name or None, sender_email or None, checkout.id))
                conn.commit()
        return jsonify({'url': checkout.url})
    except Exception as exc:
        log.error(f'Stripe checkout: {exc}')
        return jsonify({'error': 'Payment error — try again'}), 500


@app.route('/shoutout/success')
def shoutout_success():
    sid = request.args.get('sid', '')
    # Confirm payment directly (belt + braces alongside webhook)
    if sid and _STRIPE_KEY and _DB_URL:
        try:
            cs = stripe.checkout.Session.retrieve(sid)
            if cs.payment_status == 'paid':
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            'UPDATE shoutouts SET paid=TRUE WHERE stripe_session=%s',
                            (sid,)
                        )
                    conn.commit()
        except Exception as exc:
            log.warning(f'Shoutout confirm: {exc}')
    return render_template('shoutout_success.html')


@app.route('/webhook/stripe', methods=['POST'])
def webhook_stripe():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')
    if not _STRIPE_WEBHOOK:
        return jsonify({'status': 'no secret'}), 200
    try:
        event = stripe.Webhook.construct_event(payload, sig, _STRIPE_WEBHOOK)
    except Exception:
        return jsonify({'error': 'Bad signature'}), 400
    if event['type'] == 'checkout.session.completed':
        cs = event['data']['object']
        if cs.get('payment_status') == 'paid' and _DB_URL:
            try:
                meta = cs.get('metadata', {})
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            'UPDATE shoutouts SET paid=TRUE WHERE stripe_session=%s',
                            (cs['id'],)
                        )
                        # If record doesn't exist yet (webhook faster than redirect)
                        if cur.rowcount == 0:
                            cur.execute("""
                                INSERT INTO shoutouts (message, sender_name, sender_email, stripe_session, paid)
                                VALUES (%s,%s,%s,%s,TRUE)
                            """, (
                                meta.get('message',''),
                                meta.get('sender_name') or None,
                                meta.get('sender_email') or None,
                                cs['id'],
                            ))
                    conn.commit()
                log.info(f"Shoutout paid: {cs['id']}")
            except Exception as exc:
                log.error(f'Stripe webhook DB: {exc}')
    return jsonify({'status': 'ok'}), 200


@app.route('/api/shoutout/<int:shoutout_id>/reply', methods=['POST'])
def api_shoutout_reply(shoutout_id):
    if not _admin_check():
        return _auth_error()
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE shoutouts SET replied=TRUE WHERE id=%s', (shoutout_id,))
            conn.commit()
    except Exception as exc:
        log.error(f'Shoutout reply: {exc}')
        return jsonify({'error': 'DB error'}), 500
    return jsonify({'status': 'ok'})


# ── Sponsor webhooks ──────────────────────────────────────────────────────────

def _insert_sponsor(platform, sponsor_name, tier_name, amount_cents, event_type):
    if not _DB_URL:
        return
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO sponsors (platform, sponsor_name, tier_name, amount_cents, event_type) VALUES (%s,%s,%s,%s,%s)',
                (platform, sponsor_name, tier_name, amount_cents, event_type)
            )
        conn.commit()


@app.route('/webhook/github-sponsors', methods=['POST'])
def webhook_github():
    secret  = os.environ.get('GITHUB_WEBHOOK_SECRET', '')
    payload = request.get_data()
    sig     = request.headers.get('X-Hub-Signature-256', '')
    if secret:
        expected = 'sha256=' + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({'error': 'Bad signature'}), 401
    data        = request.get_json(silent=True) or {}
    action      = data.get('action', '')
    sponsorship = data.get('sponsorship', {})
    sponsor     = sponsorship.get('sponsor', {})
    tier        = sponsorship.get('tier', {})
    privacy     = sponsorship.get('privacy_level', 'public')
    name        = sponsor.get('login') if privacy == 'public' else None
    tier_name   = tier.get('name', '')
    cents       = tier.get('monthly_price_in_cents', 0)
    try:
        _insert_sponsor('github', name, tier_name, cents, action)
        log.info(f'GitHub sponsor event: {action} {name or "anonymous"}')
    except Exception as exc:
        log.error(f'GitHub webhook DB: {exc}')
    return jsonify({'status': 'ok'}), 200


@app.route('/webhook/patreon', methods=['POST'])
def webhook_patreon():
    secret  = os.environ.get('PATREON_WEBHOOK_SECRET', '')
    payload = request.get_data()
    sig     = request.headers.get('X-Patreon-Signature', '')
    if secret:
        expected = hmac.new(secret.encode(), payload, hashlib.md5).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({'error': 'Bad signature'}), 401
    data     = request.get_json(silent=True) or {}
    event    = request.headers.get('X-Patreon-Event', '')
    name     = None
    cents    = 0
    for item in data.get('included', []):
        if item.get('type') == 'user':
            attrs = item.get('attributes', {})
            if not attrs.get('hide_pledges', True):
                name = attrs.get('full_name') or attrs.get('vanity')
    pledge = data.get('data', {})
    if pledge.get('type') in ('pledge', 'member'):
        cents = pledge.get('attributes', {}).get('amount_cents', 0)
    action_map = {
        'members:pledge:create': 'created', 'pledges:create': 'created',
        'members:pledge:delete': 'cancelled', 'pledges:delete': 'cancelled',
        'members:pledge:update': 'tier_changed', 'pledges:update': 'tier_changed',
    }
    event_type = action_map.get(event, event or 'created')
    try:
        _insert_sponsor('patreon', name, '', cents, event_type)
        log.info(f'Patreon event: {event_type} {name or "anonymous"}')
    except Exception as exc:
        log.error(f'Patreon webhook DB: {exc}')
    return jsonify({'status': 'ok'}), 200


@app.route('/api/sponsors/<int:sponsor_id>/fulfill', methods=['POST'])
def api_sponsor_fulfill(sponsor_id):
    data = request.get_json(silent=True) or {}
    if not _admin_check(data):
        return _auth_error()
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE sponsors SET fulfilled=TRUE WHERE id=%s', (sponsor_id,))
            conn.commit()
    except Exception as exc:
        log.error(f'Fulfill sponsor: {exc}')
        return jsonify({'error': 'DB error'}), 500
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    threading.Thread(target=_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
