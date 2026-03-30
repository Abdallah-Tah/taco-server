#!/usr/bin/env python3
"""
Fast News-Driven Polymarket Trading Engine.

Separate module from the existing scanner. Dry-run by default.

Usage:
  python3 polymarket_news_arb.py watchlist
  python3 polymarket_news_arb.py scan
  python3 polymarket_news_arb.py dry-run
  python3 polymarket_news_arb.py run --live
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

import requests

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
SCRIPTS = ROOT / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS))

from config import (
    NEWS_SCAN_INTERVAL,
    NEWS_WATCHLIST_REFRESH,
    NEWS_ARTICLE_MAX_AGE,
    NEWS_MIN_KEYWORD_MATCH,
    NEWS_MIN_EDGE,
    NEWS_HIGH_CONVICTION_EDGE,
    NEWS_DEFAULT_SIZE,
    NEWS_HIGH_SIZE,
    NEWS_MAX_SIZE,
    NEWS_TP,
    NEWS_SL,
    NEWS_TIME_EXIT_HOURS,
    NEWS_MAX_POSITIONS,
    NEWS_SOURCES,
)
from journal import log_trade_open, log_trade_close, get_trades
from portfolio import _load_portfolio, _save_portfolio, check_drawdown, snapshot, is_paused
import polymarket_executor as pex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("poly_news_arb")

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_HOST = "https://clob.polymarket.com"
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
WATCHLIST_FILE = ROOT / ".poly_news_watchlist.json"
SEEN_ARTICLES_FILE = ROOT / ".poly_news_seen_articles.json"
POSITIONS_FILE = ROOT / ".poly_news_arb_positions.json"
EVENT_LOG_FILE = ROOT / ".poly_news_arb_log.json"

ALLOWED_CATEGORIES = {"politics", "crypto", "geopolitics", "tech", "business"}
BLOCKED_TITLE_TERMS = {"fifa", "world cup", "nba", "nfl", "mlb", "nhl", "super bowl", "championship", "match", "game", "vs.", "gta vi", "album", "movie", "song", "box office", "celebrity", "oscars", "grammys"}
GOOD_EVENT_TERMS = {
    "ceasefire", "peace", "truce", "deal", "agreement", "regime", "overthrow", "out as", "exit",
    "election", "nomination", "indicted", "convicted", "sentenced", "ruling", "court", "sec",
    "etf", "approved", "approval", "ban", "restrict", "military", "attack", "invade", "vote",
    "senate", "house", "policy", "tariff", "sanctions", "whistleblower", "hearing", "primary",
    "president", "presidential", "war", "conflict", "alien", "uap", "ufo", "disclosure", "leader"
}
BAD_EVENT_TERMS = {
    "hit $", "$1m", "$150k", "$200k", "market cap", "fdv", "one day after launch", "before gta vi",
    "price", "all-time high", "ath", "released", "album", "movie", "world cup"
}
MANUAL_MARKET_HINTS = [
    "iran", "regime", "ceasefire", "ukraine", "uap", "ufo", "aliens", "jd vance",
    "republican primary", "sec", "etf", "crypto regulation", "military", "war", "attack",
    "trump", "biden", "senate", "house", "president"
]
STOPWORDS = {
    "will", "the", "a", "an", "of", "in", "on", "for", "to", "by", "before", "after", "this", "that",
    "be", "is", "are", "or", "and", "vs", "than", "with", "at", "from", "it", "as", "its", "into",
    "gta", "vi", "2025", "2026", "2027"
}
CATEGORY_HINTS = {
    "politics": ["trump", "biden", "president", "senate", "house", "election", "supreme court", "congress", "white house", "governor", "mayor"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "token", "etf", "sec", "binance", "coinbase"],
    "geopolitics": ["russia", "ukraine", "china", "taiwan", "iran", "israel", "gaza", "war", "ceasefire", "nato", "putin", "xi"],
    "tech": ["apple", "google", "openai", "microsoft", "meta", "tesla", "ai", "chatgpt", "xai", "anthropic", "nvidia"],
    "business": ["ipo", "earnings", "merger", "acquisition", "bankruptcy", "fed", "inflation", "tariff", "company", "shares"]
}
EXTRA_KEYWORDS = {
    "ceasefire": ["peace", "truce", "negotiations", "talks", "war"],
    "russia": ["putin", "moscow", "kremlin"],
    "ukraine": ["kyiv", "zelensky"],
    "iran": ["regime", "tehran", "sanctions"],
    "bitcoin": ["btc", "crypto"],
    "ethereum": ["eth", "crypto"],
    "solana": ["sol", "crypto"],
    "approved": ["approval", "approved", "passes", "passed"],
    "deal": ["agreement", "signed"],
    "aliens": ["uap", "ufo", "disclosure"],
}
MARKET_SPECIFIC_SIGNALS = {
    "iran_regime": {
        "keywords": ["iran", "regime", "overthrow", "fall"],
        "positive_for_yes": ["invasion", "strikes", "bombing", "regime change", "protests", "uprising", "revolution", "overthrow", "military action", "attack iran", "collapse", "instability", "sanctions tighten", "opposition"],
        "negative_for_yes": ["diplomacy", "nuclear deal", "stabilize", "de-escalation", "negotiations succeed", "lifted sanctions", "allies support iran"],
    },
    "ceasefire": {
        "keywords": ["russia", "ukraine", "ceasefire", "truce", "peace"],
        "positive_for_yes": ["peace talks", "ceasefire", "negotiations", "diplomatic", "truce", "settlement", "backs ukraine", "pressure russia", "concessions", "framework", "envoy", "mediator", "peace plan"],
        "negative_for_yes": ["escalation", "offensive", "missile strike", "troops deployed", "rejected peace", "no ceasefire", "stalled talks", "breakdown"],
    },
    "crypto_regulation": {
        "keywords": ["crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "ban", "restrict"],
        "positive_for_yes": ["ban", "restrict", "prohibit", "crackdown", "illegal", "enforcement", "sec sues", "regulatory action"],
        "negative_for_yes": ["approved", "embraces", "legalize", "regulatory clarity", "etf approved", "adoption"],
    },
    "uap": {
        "keywords": ["aliens", "uap", "ufo", "non-human", "disclosure"],
        "positive_for_yes": ["confirmed", "disclosure", "non-human", "ufo evidence", "congressional hearing", "whistleblower", "classified briefing", "pentagon confirms"],
        "negative_for_yes": ["debunked", "hoax", "misidentified", "no evidence", "natural phenomenon"],
    },
}
POSITIVE_YES = {
    "deal", "agreement", "approved", "confirmed", "progress", "breakthrough",
    "signed", "passed", "talks succeed", "ceasefire", "peace", "resolution",
    "victory", "wins"
}
NEGATIVE_YES = {
    "failed", "rejected", "collapsed", "breakdown", "stalled", "sanctions",
    "attack", "escalation", "tensions", "threatens", "blocks", "vetoed",
    "canceled", "killed"
}
NEUTRAL_NOISE = {"talks", "discusses", "considers", "may", "could", "reportedly", "sources say"}


@dataclass
class WatchMarket:
    market_id: str
    condition_id: str
    title: str
    slug: str
    keywords: list[str]
    anchor_keywords: list[str]
    current_yes_price: float
    current_no_price: float
    yes_token_id: str
    no_token_id: str
    liquidity: float
    min_order_size: float
    min_tick_size: float
    category: str
    url: str
    end_date: str


@dataclass
class NewsItem:
    title: str
    snippet: str
    url: str
    source: str
    published_ts: float
    keyword: str


@dataclass
class MatchCandidate:
    market: WatchMarket
    article: NewsItem
    matched_keywords: list[str]
    sentiment_side: str | None
    signal_strength: str
    estimated_shift_pct: float
    market_price: float
    target_probability: float
    edge_pct: float


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def append_event(kind: str, payload: dict) -> None:
    log = load_json(EVENT_LOG_FILE, [])
    log.append({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload})
    save_json(EVENT_LOG_FILE, log[-500:])


def parse_json_field(val, default):
    if val is None:
        return default
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


def infer_category(title: str) -> str | None:
    t = title.lower()
    scores = {cat: 0 for cat in CATEGORY_HINTS}
    for cat, words in CATEGORY_HINTS.items():
        for word in words:
            if word in t:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def extract_keywords(title: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9']+", title.lower())
    out = []
    for word in words:
        if len(word) < 3 or word in STOPWORDS:
            continue
        out.append(word)
        out.extend(EXTRA_KEYWORDS.get(word, []))
    # phrases
    lowered = title.lower()
    if "ceasefire" in lowered:
        out.extend(["ceasefire", "peace", "truce", "negotiations", "war"])
    if "etf" in lowered:
        out.extend(["etf", "approved", "approval", "sec"])
    uniq = []
    seen = set()
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq[:12]


def parse_market_end_date(m: dict) -> datetime | None:
    raw = m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_good_event_market(title: str) -> bool:
    t = title.lower()
    if any(term in t for term in BAD_EVENT_TERMS):
        return False
    return any(term in t for term in GOOD_EVENT_TERMS)


def matches_manual_market_hint(title: str) -> bool:
    t = title.lower()
    return any(h in t for h in MANUAL_MARKET_HINTS)


def derive_anchor_keywords(title: str, keywords: list[str]) -> list[str]:
    t = title.lower()
    anchors = []
    if any(x in t for x in ["russia", "ukraine", "ceasefire"]):
        anchors += ["russia", "ukraine", "kremlin", "moscow", "kyiv"]
    if "putin" in t:
        anchors += ["putin"]
    if "zelensky" in t or "zelenskyy" in t:
        anchors += ["zelenskyy", "zelensky"]
    if "china" in t or "taiwan" in t or "invade taiwan" in t:
        anchors += ["china", "taiwan", "beijing", "taipei"]
    if "trump" in t and "impeach" in t:
        anchors += ["trump", "impeach"]
    elif "trump" in t:
        anchors += ["trump"]
    if "republican" in t and "senate" in t:
        anchors += ["republican", "gop", "senate"]
    if "democratic" in t and "house" in t:
        anchors += ["democrat", "democratic", "house"]
    if any(x in t for x in ["colombia", "colombian", "cepeda", "valencia", "espriella"]):
        anchors += ["colombia", "colombian", "cepeda", "valencia", "espriella"]
    if "maxwell" in t or "ghislaine" in t:
        anchors += ["maxwell", "ghislaine"]
    if not anchors:
        for kw in keywords:
            if len(kw) >= 5 and not kw.isdigit():
                anchors.append(kw)
        anchors = anchors[:4]
    uniq = []
    seen = set()
    for a in anchors:
        al = a.lower()
        if al in seen:
            continue
        seen.add(al)
        uniq.append(a)
    return uniq


def market_search_queries(market: WatchMarket) -> list[str]:
    title = market.title.lower()
    queries = []
    if "iran" in title:
        queries += ["iran", "tehran", "regime", "iran war", "iran regime"]
    if any(x in title for x in ["russia", "ukraine", "ceasefire", "truce", "peace"]):
        queries += ["ukraine", "ceasefire", "peace talks", "russia war"]
    if any(x in title for x in ["uap", "ufo", "alien", "aliens"]):
        queries += ["UAP", "UFO", "pentagon disclosure"]
    if "jd vance" in title or ("vance" in title and "2028" in title):
        queries += ["JD Vance", "2028 republican primary"]
    if any(x in title for x in ["sec", "etf", "crypto", "bitcoin", "ethereum", "solana"]):
        queries += ["SEC", "crypto regulation", "ETF approved"]
    if any(x in title for x in ["trump", "biden", "senate", "house", "republican", "democrat", "president", "election"]):
        queries += ["US politics", "congress", "election"]
    if any(x in title for x in ["war", "military", "attack", "missile", "strike", "conflict"]):
        queries += ["military conflict", "missile strike", "war escalation"]
    queries += market.anchor_keywords[:3]
    queries += market.keywords[:4]
    uniq = []
    seen = set()
    for q in queries:
        q = q.strip()
        if not q:
            continue
        ql = q.lower()
        if ql in seen:
            continue
        seen.add(ql)
        uniq.append(q)
    return uniq[:10]


def gamma_fetch_active(limit: int = 500) -> list[dict]:
    items = []
    offset = 0
    while len(items) < limit:
        r = requests.get(GAMMA_API, params={
            "limit": 100,
            "offset": offset,
            "active": True,
            "closed": False,
            "archived": False,
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not data:
            break
        items.extend(data)
        offset += len(data)
        if len(data) < 100:
            break
    return items[:limit]


def build_watchlist() -> list[WatchMarket]:
    markets = gamma_fetch_active(limit=800)
    watch = []
    seen_market_ids = set()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=366)
    for m in markets:
        try:
            liquidity = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            if liquidity <= 5000:
                continue
            outcomes = parse_json_field(m.get("outcomes"), [])
            prices = [float(x) for x in parse_json_field(m.get("outcomePrices"), [])]
            token_ids = parse_json_field(m.get("clobTokenIds"), [])
            if len(outcomes) != 2 or len(prices) != 2 or len(token_ids) != 2:
                continue
            title = m.get("question") or ""
            t_lower = title.lower()
            if any(term in t_lower for term in BLOCKED_TITLE_TERMS):
                continue
            if not is_good_event_market(title) and not matches_manual_market_hint(title):
                continue
            end_dt = parse_market_end_date(m)
            if not end_dt or end_dt > cutoff:
                continue
            category = (m.get("category") or infer_category(title) or "").lower()
            if category not in ALLOWED_CATEGORIES:
                continue
            yes_price = prices[0]
            no_price = prices[1]
            if not (0.05 <= yes_price <= 0.90):
                continue
            keywords = extract_keywords(title)
            if len(keywords) < 2:
                continue
            market_id = str(m.get("id") or "")
            if market_id in seen_market_ids:
                continue
            seen_market_ids.add(market_id)
            watch.append(WatchMarket(
                market_id=market_id,
                condition_id=str(m.get("conditionId") or ""),
                title=title,
                slug=m.get("slug") or "",
                keywords=keywords,
                anchor_keywords=derive_anchor_keywords(title, keywords),
                current_yes_price=yes_price,
                current_no_price=no_price,
                yes_token_id=str(token_ids[0]),
                no_token_id=str(token_ids[1]),
                liquidity=liquidity,
                min_order_size=float(m.get("orderMinSize") or 5),
                min_tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
                category=category,
                url=f"https://polymarket.com/event/{m.get('slug')}",
                end_date=end_dt.isoformat(),
            ))
        except Exception as e:
            logger.debug("watchlist skip: %s", e)
    watch.sort(key=lambda x: (x.liquidity, -abs(0.5 - x.current_yes_price)), reverse=True)
    save_json(WATCHLIST_FILE, [asdict(w) for w in watch])
    return watch


def load_watchlist(force_refresh: bool = False) -> list[WatchMarket]:
    if force_refresh or not WATCHLIST_FILE.exists() or (time.time() - WATCHLIST_FILE.stat().st_mtime) > NEWS_WATCHLIST_REFRESH:
        return build_watchlist()
    return [WatchMarket(**x) for x in load_json(WATCHLIST_FILE, [])]


def parse_pubdate(text: str) -> float | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def google_news_rss(keyword: str) -> list[NewsItem]:
    url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=en-US&gl=US&ceid=US:en"
    r = requests.get(url, timeout=6)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.findall('.//item')[:10]:
        title = item.findtext('title', default='')
        link = item.findtext('link', default='')
        desc = item.findtext('description', default='')
        pub = item.findtext('pubDate', default='')
        ts = parse_pubdate(pub)
        if not ts:
            continue
        items.append(NewsItem(title=title, snippet=desc, url=link, source='google_rss', published_ts=ts, keyword=keyword))
    return items


def duckduckgo_news(keyword: str) -> list[NewsItem]:
    try:
        r = requests.get(
            "https://duckduckgo.com/news.js",
            params={"q": keyword, "df": "d", "o": "json"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        items = []
        for row in data.get("results", [])[:10]:
            age = (row.get("date") or row.get("time") or "").strip()
            ts = None
            if age.isdigit():
                ts = float(age)
            items.append(NewsItem(
                title=row.get("title", ""),
                snippet=row.get("excerpt", ""),
                url=row.get("url", ""),
                source='duckduckgo',
                published_ts=ts or time.time(),
                keyword=keyword,
            ))
        return items
    except Exception:
        return []


def reddit_atom_feed(url: str, source: str) -> list[NewsItem]:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Taco/1.0"}, timeout=10)
        if r.status_code != 200:
            return []
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.content)
        items = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:content", default="", namespaces=ns) or entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ns) or entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
            link = ""
            for link_el in entry.findall("atom:link", ns):
                href = link_el.attrib.get("href", "")
                rel = link_el.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
            ts = None
            if published:
                try:
                    dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                except Exception:
                    ts = parse_pubdate(published)
            items.append(NewsItem(title=title, snippet=summary, url=link, source=source, published_ts=ts or time.time(), keyword=source))
        return items
    except Exception:
        return []


def fetch_news_for_keyword(keyword: str) -> list[NewsItem]:
    items = []
    if "google_rss" in NEWS_SOURCES:
        try:
            items.extend(google_news_rss(keyword))
        except Exception as e:
            logger.debug("google rss failed for %s: %s", keyword, e)
    # DuckDuckGo is optional and slower/noisier; only use it if Google RSS returned nothing.
    if not items and "duckduckgo" in NEWS_SOURCES:
        items.extend(duckduckgo_news(keyword))
    return items


def fetch_reddit_feeds() -> list[NewsItem]:
    items = []
    items.extend(reddit_atom_feed('https://www.reddit.com/r/worldnews/new/.rss', 'reddit_worldnews'))
    items.extend(reddit_atom_feed('https://www.reddit.com/r/cryptocurrency/new/.rss', 'reddit_cryptocurrency'))
    return items


def dedupe_articles(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen = set()
    out = []
    for item in sorted(items, key=lambda x: x.published_ts, reverse=True):
        key = item.url or item.title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def recent_articles(items: Iterable[NewsItem]) -> list[NewsItem]:
    cutoff = time.time() - NEWS_ARTICLE_MAX_AGE
    return [x for x in items if x.published_ts and x.published_ts >= cutoff]


def keyword_match(article: NewsItem, market: WatchMarket) -> tuple[list[str], list[str]]:
    headline = article.title.lower()
    text = f"{article.title} {article.snippet}".lower()
    matched = []
    for kw in market.keywords:
        if kw in text:
            matched.append(kw)
    anchor_matches = []
    for anchor in market.anchor_keywords:
        if anchor.lower() in headline:
            anchor_matches.append(anchor)
    return sorted(set(matched)), sorted(set(anchor_matches))


def fetch_fresh_price(token_id: str, side: str = "buy") -> float | None:
    sess = requests.Session()
    sess.proxies = TOR_PROXY
    try:
        r = sess.get(f"{CLOB_HOST}/price", params={"token_id": token_id, "side": side}, timeout=8)
        if r.status_code == 200:
            return float(r.json().get("price", 0) or 0)
    except Exception:
        return None
    return None


def market_signal_sets(market: WatchMarket) -> tuple[set[str], set[str]]:
    title = market.title.lower()
    pos = set(POSITIVE_YES)
    neg = set(NEGATIVE_YES)
    for cfg in MARKET_SPECIFIC_SIGNALS.values():
        if any(k in title for k in cfg['keywords']):
            pos.update(cfg['positive_for_yes'])
            neg.update(cfg['negative_for_yes'])
    return pos, neg


def analyze_sentiment(article: NewsItem, market: WatchMarket | None = None) -> tuple[str | None, str, float]:
    text = f"{article.title} {article.snippet}".lower()
    pos_dict, neg_dict = (set(POSITIVE_YES), set(NEGATIVE_YES)) if market is None else market_signal_sets(market)
    pos_hits = [w for w in pos_dict if w in text]
    neg_hits = [w for w in neg_dict if w in text]
    neutral_hits = [w for w in NEUTRAL_NOISE if w in text]

    if len(pos_hits) >= 2 and len(neg_hits) == 0:
        return "YES", "strong", 15.0
    if len(neg_hits) >= 2 and len(pos_hits) == 0:
        return "NO", "strong", 15.0
    if len(pos_hits) >= 1 and len(neg_hits) == 0 and len(neutral_hits) == 0:
        return None, "weak_positive", 0.0
    return None, "skip", 0.0


def scan_news_cycle(watchlist: list[WatchMarket], debug: bool = False) -> list[MatchCandidate] | tuple[list[MatchCandidate], dict]:
    seen_articles = load_json(SEEN_ARTICLES_FILE, {})
    now = time.time()
    candidates: list[MatchCandidate] = []
    partial_matches = []

    queries = []
    for market in watchlist[:20]:
        queries.extend(market_search_queries(market))
    uniq_queries = []
    seen_queries = set()
    for q in queries:
        ql = q.lower()
        if ql in seen_queries:
            continue
        seen_queries.add(ql)
        uniq_queries.append(q)
    uniq_queries = uniq_queries[:40]

    fetched = []
    for q in uniq_queries:
        fetched.extend(fetch_news_for_keyword(q))
    fetched.extend(fetch_reddit_feeds())
    fetched_total = len(fetched)
    deduped_articles = dedupe_articles(fetched)
    recent = recent_articles(deduped_articles)
    evaluated_articles = []

    for article in recent:
        seen_key = article.url or article.title
        if seen_key in seen_articles:
            continue
        evaluated_articles.append(article)
        for market in watchlist:
            matched, anchor_matches = keyword_match(article, market)
            if len(matched) == 0:
                continue
            side, strength, shift = analyze_sentiment(article, market)
            skip_reason = None
            fresh_price = None
            target_probability = None
            edge = None
            non_anchor_matches = [m for m in matched if m.lower() not in {a.lower() for a in anchor_matches}]
            if len(anchor_matches) == 0:
                skip_reason = "missing anchor keyword"
            elif len(non_anchor_matches) < 1:
                skip_reason = "missing additional keyword"
            elif not side:
                skip_reason = "ambiguous or weak sentiment"
            else:
                fresh_price = fetch_fresh_price(market.yes_token_id if side != "NO" else market.no_token_id, side="buy")
                if not fresh_price:
                    fresh_price = market.current_yes_price if side != "NO" else market.current_no_price
                target_probability = fresh_price + (shift / 100.0)
                target_probability = max(0.01, min(0.99, target_probability))
                edge = (target_probability - fresh_price) * 100.0
                if edge < NEWS_MIN_EDGE:
                    skip_reason = f"edge too small ({edge:.2f}% < {NEWS_MIN_EDGE:.2f}%)"
            partial_matches.append({
                "headline": article.title,
                "market": market.title,
                "matched_keywords": matched,
                "anchor_keywords": anchor_matches,
                "match_count": len(matched),
                "sentiment_side": side,
                "signal_strength": strength,
                "edge_pct": edge,
                "skip_reason": skip_reason,
            })
            if skip_reason:
                continue
            logger.info("NEWS HIT: '%s' matches market '%s' (keywords: %s)", article.title[:120], market.title[:120], ", ".join(matched))
            append_event("news_hit", {
                "headline": article.title,
                "market": market.title,
                "matched_keywords": matched,
                "source": article.source,
                "url": article.url,
            })
            candidates.append(MatchCandidate(
                market=market,
                article=article,
                matched_keywords=matched,
                sentiment_side=side,
                signal_strength=strength,
                estimated_shift_pct=shift,
                market_price=fresh_price,
                target_probability=target_probability,
                edge_pct=edge,
            ))
        seen_articles[seen_key] = now
    cutoff = now - 86400
    seen_articles = {k: v for k, v in seen_articles.items() if v >= cutoff}
    save_json(SEEN_ARTICLES_FILE, seen_articles)
    if not debug:
        return candidates
    partial_matches.sort(key=lambda x: (x['match_count'], len(x['matched_keywords'])), reverse=True)
    stats = {
        "fetched_total": fetched_total,
        "deduped_total": len(deduped_articles),
        "recent_total": len(recent),
        "evaluated_total": len(evaluated_articles),
        "partial_match_total": len(partial_matches),
        "queries_used": uniq_queries,
        "top_partial_matches": partial_matches[:20],
    }
    return candidates, stats


def _same_market_open_any_engine(condition_id: str, token_id: str) -> bool:
    shared_positions = load_json(ROOT / ".poly_positions.json", {})
    if token_id in shared_positions:
        return True
    local_positions = load_json(POSITIONS_FILE, {})
    if token_id in local_positions:
        return True
    for t in get_trades(limit=1000, closed_only=False):
        if t.get("timestamp_close"):
            continue
        asset = str(t.get("asset") or "")
        notes = str(t.get("notes") or "")
        if token_id in asset or condition_id in asset or condition_id in notes:
            return True
    return False


def _news_arb_open_positions() -> dict:
    return load_json(POSITIONS_FILE, {})


def drawdown_paused() -> tuple[bool, str]:
    portfolio = _load_portfolio()
    snap = snapshot()
    total = snap.get("total_usd") or 100.0
    portfolio = check_drawdown(portfolio, total)
    _save_portfolio(portfolio)
    paused = is_paused(portfolio)
    return paused, f"capital=${total:.2f}"


def choose_trade(candidates: list[MatchCandidate]) -> MatchCandidate | None:
    viable = []
    open_positions = _news_arb_open_positions()
    if len(open_positions) >= NEWS_MAX_POSITIONS:
        logger.info("Max news-arb positions reached (%s)", NEWS_MAX_POSITIONS)
        return None
    for c in candidates:
        if not c.sentiment_side or c.signal_strength == "weak":
            continue
        if c.edge_pct < NEWS_MIN_EDGE:
            continue
        token_id = c.market.yes_token_id if c.sentiment_side == "YES" else c.market.no_token_id
        if _same_market_open_any_engine(c.market.condition_id, token_id):
            continue
        viable.append(c)
    viable.sort(key=lambda x: (x.edge_pct, x.market.liquidity), reverse=True)
    return viable[0] if viable else None


def calc_order_size_usd(edge_pct: float) -> float:
    usd = NEWS_HIGH_SIZE if edge_pct >= NEWS_HIGH_CONVICTION_EDGE else NEWS_DEFAULT_SIZE
    return min(usd, NEWS_MAX_SIZE)


def usd_to_shares(usd: float, price: float, min_order_size: float) -> float:
    shares = max(min_order_size, round(usd / max(price, 0.01), 2))
    return shares


def save_news_position(entry: dict) -> None:
    data = _news_arb_open_positions()
    data[entry["token_id"]] = entry
    save_json(POSITIONS_FILE, data)


def remove_news_position(token_id: str) -> None:
    data = _news_arb_open_positions()
    data.pop(token_id, None)
    save_json(POSITIONS_FILE, data)


def execute_trade(candidate: MatchCandidate, live: bool = False) -> dict:
    paused, reason = drawdown_paused()
    if paused:
        return {"status": "SKIP", "reason": f"drawdown paused: {reason}"}

    side = candidate.sentiment_side
    if side not in {"YES", "NO"}:
        return {"status": "SKIP", "reason": "ambiguous sentiment"}

    token_id = candidate.market.yes_token_id if side == "YES" else candidate.market.no_token_id
    price = candidate.market_price
    usd = calc_order_size_usd(candidate.edge_pct)
    shares = usd_to_shares(usd, price, candidate.market.min_order_size)
    order_side = pex.BUY

    payload = {
        "market": candidate.market.title,
        "slug": candidate.market.slug,
        "url": candidate.market.url,
        "side": side,
        "token_id": token_id,
        "price": round(price, 4),
        "edge_pct": round(candidate.edge_pct, 2),
        "target_probability": round(candidate.target_probability * 100, 2),
        "headline": candidate.article.title,
        "source": candidate.article.source,
        "shares": shares,
        "usd_size": usd,
        "matched_keywords": candidate.matched_keywords,
        "strength": candidate.signal_strength,
    }

    logger.info("TRADE: %s | side=%s | price=%.3f | edge=%.1f%% | headline='%s'",
                candidate.market.title[:80], side, price, candidate.edge_pct, candidate.article.title[:120])
    append_event("trade_candidate", payload)

    if not live:
        return {"status": "DRY_RUN", **payload}

    client = pex.get_client()
    original_save_position = pex.save_position
    try:
        pex.save_position = lambda token_id, amount, price, side_str, market_question="", condition_id="": None
        result = pex.place_order(client, token_id, shares, price, order_side, candidate.market.title, candidate.market.condition_id)
    finally:
        pex.save_position = original_save_position

    if not result:
        return {"status": "FAILED", **payload}

    trade_id = log_trade_open(
        trade_id=str(uuid.uuid4()),
        engine="polymarket_news_arb",
        asset=token_id,
        category=candidate.market.category,
        direction=f"BUY_{side}",
        entry_price=price,
        position_size=shares,
        position_size_usd=usd,
        edge_percent=candidate.edge_pct,
        confidence=1.0 if candidate.signal_strength == "strong" else 0.7,
        regime="news",
        notes=json.dumps({
            "condition_id": candidate.market.condition_id,
            "headline": candidate.article.title,
            "url": candidate.article.url,
            "market": candidate.market.title,
            "side": side,
        })[:1000],
    )
    save_news_position({
        "trade_id": trade_id,
        "token_id": token_id,
        "condition_id": candidate.market.condition_id,
        "market": candidate.market.title,
        "side": side,
        "entry_price": price,
        "peak_price": price,
        "shares": shares,
        "usd_size": usd,
        "headline": candidate.article.title,
        "opened": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "LIVE", "trade_id": trade_id, **payload}


def monitor_positions(live: bool = False) -> list[dict]:
    positions = _news_arb_open_positions()
    results = []
    now = datetime.now(timezone.utc)
    for token_id, pos in list(positions.items()):
        try:
            current = fetch_fresh_price(token_id, side="sell") or float(pos["entry_price"])
            entry = float(pos["entry_price"])
            side = pos["side"]
            opened = datetime.fromisoformat(pos["opened"])
            peak = float(pos.get("peak_price", entry))
            if current > peak:
                pos["peak_price"] = current
                peak = current
            move_pct = ((current - entry) / entry) * 100.0
            favorable = move_pct if side == "YES" else ((current - entry) / entry) * 100.0
            adverse = favorable
            hold_s = int((now - opened).total_seconds())
            exit_reason = None
            if favorable >= NEWS_TP:
                exit_reason = "TP"
            elif adverse <= NEWS_SL:
                exit_reason = "SL"
            elif hold_s >= int(NEWS_TIME_EXIT_HOURS * 3600):
                exit_reason = "TIME"
            elif ((peak - entry) / entry) * 100.0 >= 5.0 and ((current - peak) / peak) * 100.0 <= -3.0:
                exit_reason = "MOMENTUM"
            save_news_position(pos)
            if not exit_reason:
                continue
            pnl_pct = ((current - entry) / entry) * 100.0
            pnl_abs = (current - entry) * float(pos["shares"])
            results.append({
                "market": pos["market"], "entry_price": entry, "exit_price": current,
                "pnl_percent": pnl_pct, "hold_duration": hold_s, "trigger_headline": pos.get("headline", ""),
                "exit_reason": exit_reason,
            })
            if live:
                client = pex.get_client()
                pex.place_order(client, token_id, float(pos["shares"]), current, pex.SELL, pos["market"], pos["condition_id"])
                log_trade_close(pos["trade_id"], exit_price=current, pnl_absolute=pnl_abs, pnl_percent=pnl_pct,
                                exit_type=exit_reason, hold_duration_seconds=hold_s,
                                notes=json.dumps({"headline": pos.get("headline", ""), "market": pos["market"]})[:1000])
                remove_news_position(token_id)
        except Exception as e:
            logger.exception("monitor position failed for %s: %s", token_id[:10], e)
    return results


def cmd_watchlist() -> int:
    watch = load_watchlist(force_refresh=True)
    print(f"WATCHLIST_SIZE {len(watch)}")
    for i, m in enumerate(watch[:20], 1):
        print(f"{i:02d}. [{m.category}] ${m.liquidity:,.0f} yes={m.current_yes_price:.3f} no={m.current_no_price:.3f} min={m.min_order_size:g}")
        print(f"    {m.title}")
        print(f"    slug={m.slug}")
        print(f"    keywords={', '.join(m.keywords[:10])}")
    return 0


def cmd_scan() -> tuple[int, list[MatchCandidate]]:
    watch = load_watchlist()
    candidates = scan_news_cycle(watch)
    print(f"MATCHES_FOUND {len(candidates)}")
    for c in candidates[:20]:
        print(f"- {c.article.title[:110]}")
        print(f"  market={c.market.title}")
        print(f"  matched={', '.join(c.matched_keywords)}")
        print(f"  side={c.sentiment_side} strength={c.signal_strength} price={c.market_price:.3f} edge={c.edge_pct:.2f}%")
    return 0, candidates


def cmd_dry_run() -> int:
    _, candidates = cmd_scan()
    pick = choose_trade(candidates)
    if not pick:
        print("DRY_RUN_RESULT no actionable trade found this cycle")
        return 0
    result = execute_trade(pick, live=False)
    print("DRY_RUN_RESULT")
    print(json.dumps(result, indent=2))
    return 0


def run_loop(live: bool = False) -> int:
    print(f"START news-arb loop live={live} scan_interval={NEWS_SCAN_INTERVAL}s refresh={NEWS_WATCHLIST_REFRESH}s")
    watch = load_watchlist(force_refresh=True)
    last_watch_refresh = time.time()
    while True:
        try:
            if time.time() - last_watch_refresh >= NEWS_WATCHLIST_REFRESH:
                watch = load_watchlist(force_refresh=True)
                last_watch_refresh = time.time()
            monitor_positions(live=live)
            candidates = scan_news_cycle(watch)
            pick = choose_trade(candidates)
            if pick:
                result = execute_trade(pick, live=live)
                print(json.dumps(result, indent=2))
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] no actionable news trade")
        except Exception as e:
            logger.exception("loop error: %s", e)
        time.sleep(NEWS_SCAN_INTERVAL)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="dry-run", choices=["watchlist", "scan", "dry-run", "run"])
    ap.add_argument("--live", action="store_true", help="enable live orders in run mode only")
    args = ap.parse_args()

    if args.command == "watchlist":
        return cmd_watchlist()
    if args.command == "scan":
        return cmd_scan()[0]
    if args.command == "dry-run":
        return cmd_dry_run()
    return run_loop(live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
