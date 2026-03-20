#!/usr/bin/env python3
"""
Taco Polymarket Scanner v2 — Production-ready opportunity engine.

Combines:
  - Gamma API for rich market metadata (title, tags, category, volume, liquidity)
  - CLOB API (via Tor) for orderbook spread/depth
  - LMSR math for EV calculations
  - Category classification (sports, politics, crypto, economics, geopolitics)
  - Tradability filtering (liquidity, spread, time horizon, acceptance)
  - Ranked output with actionable trade signals

Usage:
  python3 polymarket_scanner_v2.py              # Full scan, top opportunities
  python3 polymarket_scanner_v2.py --category crypto   # Filter by category
  python3 polymarket_scanner_v2.py --json        # JSON output for piping
  python3 polymarket_scanner_v2.py --execute     # Show execution-ready details (condition_id, tokens)
"""
import json
import math
import os
import sys
import requests
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ───

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}

# Secrets
SECRETS_FILE = Path.home() / ".config" / "openclaw" / "secrets.env"

# Scoring thresholds
MIN_LIQUIDITY = 5000        # Minimum $ liquidity to consider
MIN_VOLUME_24H = 1000       # Minimum 24h volume
MAX_SPREAD = 0.08           # Max bid-ask spread (8 cents)
MIN_SCORE = 40              # Minimum composite score to surface

# ─── Category Keywords ───

CATEGORY_RULES = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
        "token", "defi", "nft", "blockchain", "binance", "coinbase",
        "dogecoin", "doge", "xrp", "cardano", "polygon", "matic",
        "altcoin", "memecoin", "stablecoin", "usdc", "usdt", "halving",
    ],
    "sports": [
        "nba", "nfl", "mlb", "nhl", "premier league", "champions league",
        "world cup", "tennis", "f1", "formula 1", "ufc", "boxing",
        "super bowl", "playoffs", "finals", "championship", "mvp",
        "la liga", "serie a", "bundesliga", "ligue 1", "europa league",
        "grand slam", "wimbledon", "us open", "march madness",
        "stanley cup", "world series",
    ],
    "politics": [
        "trump", "biden", "president", "election", "congress", "senate",
        "democrat", "republican", "governor", "supreme court", "scotus",
        "impeach", "veto", "executive order", "cabinet", "nomination",
        "primary", "caucus", "poll", "approval rating", "midterm",
        "speaker", "majority leader", "rnc", "dnc",
    ],
    "economics": [
        "fed", "federal reserve", "interest rate", "inflation", "cpi",
        "gdp", "unemployment", "jobs report", "fomc", "rate cut",
        "rate hike", "recession", "treasury", "yield", "bond",
        "tariff", "trade war", "sanctions", "debt ceiling",
        "stock market", "s&p", "nasdaq", "dow",
    ],
    "geopolitics": [
        "iran", "ukraine", "russia", "china", "taiwan", "war",
        "military", "ceasefire", "nato", "regime", "invasion",
        "missile", "nuclear", "sanctions", "north korea", "israel",
        "gaza", "hamas", "hezbollah", "coup", "territorial",
    ],
    "tech": [
        "ai", "artificial intelligence", "openai", "chatgpt", "google",
        "apple", "meta", "microsoft", "tesla", "spacex", "neuralink",
        "agi", "gpt", "llm", "robot", "autonomous",
    ],
    "culture": [
        "oscar", "grammy", "emmy", "netflix", "spotify", "tiktok",
        "twitter", "youtube", "celebrity", "movie", "album", "tour",
        "viral", "streaming", "box office",
    ],
}

# ─── LMSR Math ───

def expected_value(market_price, true_prob):
    """EV of buying YES at market_price when true prob is true_prob."""
    return true_prob * (1 - market_price) - (1 - true_prob) * market_price

def kelly_fraction(market_price, true_prob, mult=0.25):
    """Quarter-Kelly sizing."""
    if true_prob <= market_price or market_price <= 0 or market_price >= 1:
        return 0
    odds = (1 - market_price) / market_price
    kelly = (true_prob * odds - (1 - true_prob)) / odds
    return max(0, kelly * mult)

# ─── Helpers ───

def load_secrets():
    """Load secrets from env file."""
    secrets = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets

def tor_session():
    """Create a requests session routed through Tor."""
    s = requests.Session()
    s.proxies = TOR_PROXY
    return s

SPORTS_PATTERNS = [
    # "X vs. Y" or "X vs Y" pattern (common for game markets)
    r"\bvs\.?\s",
    # Common team suffixes
    r"\b(fc|sc|cf|afc|united|city|rovers|wanderers|athletic)\b",
    # "win on YYYY-MM-DD" pattern
    r"win on \d{4}-\d{2}-\d{2}",
    # "end in a draw" pattern
    r"end in a draw",
    # League/tournament patterns
    r"\b(league|cup|championship|finals|playoffs|series|match|game)\b",
]

import re
_SPORTS_RE = [re.compile(p, re.IGNORECASE) for p in SPORTS_PATTERNS]

def classify_market(question, tags=None):
    """Classify a market into a category. Sports detection is prioritized."""
    text = question.lower()
    tag_text = ""
    if tags:
        tag_text = " ".join(t.lower() for t in tags if isinstance(t, str))
    full_text = text + " " + tag_text

    # Priority 1: Check sports patterns first (catches "X vs Y" game markets)
    sports_signal = sum(1 for rx in _SPORTS_RE if rx.search(question))
    sports_keyword_hits = sum(1 for kw in CATEGORY_RULES["sports"] if kw in full_text)

    if sports_signal >= 1 or sports_keyword_hits >= 2:
        return "sports"

    # Priority 2: Standard keyword matching
    scores = {}
    for cat, keywords in CATEGORY_RULES.items():
        if cat == "sports":
            continue  # Already handled
        hits = sum(1 for kw in keywords if kw in full_text)
        if hits > 0:
            scores[cat] = hits

    if not scores:
        return "other"
    return max(scores, key=scores.get)

# ─── Data Fetchers ───

def fetch_gamma_markets(limit=500):
    """Fetch rich market metadata from Gamma API (no geo-restriction)."""
    all_markets = []
    offset = 0
    while offset < limit:
        batch = min(100, limit - offset)
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": batch,
                    "offset": offset,
                    "order": "liquidityNum",
                    "ascending": "false",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            all_markets.extend(data)
            offset += batch
        except Exception as e:
            print(f"[WARN] Gamma fetch error at offset {offset}: {e}", file=sys.stderr)
            break
    return all_markets

def fetch_clob_orderbook(token_id, tor_sess):
    """Fetch orderbook from CLOB API via Tor for spread/depth."""
    try:
        r = tor_sess.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_best_bid_ask(book):
    """Extract best bid/ask from orderbook."""
    if not book:
        return None, None
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    # Bids: highest first (best bid = max price)
    best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
    # Asks: lowest first (best ask = min price)
    best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None
    return best_bid, best_ask

def get_book_depth(book, levels=5):
    """Sum depth of top N levels on each side."""
    if not book:
        return 0, 0
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_depth = sum(float(b.get("size", 0)) for b in bids[:levels])
    ask_depth = sum(float(a.get("size", 0)) for a in asks[:levels])
    return bid_depth, ask_depth

# ─── Analysis Engine ───

def analyze_market(m):
    """Full analysis of a single market. Returns scored dict or None."""
    # Parse basics
    question = m.get("question", "?")
    vol_total = float(m.get("volume", 0) or 0)
    vol_24h = float(m.get("volume24hr", 0) or 0)
    liq = float(m.get("liquidity", 0) or 0)
    liq_clob = float(m.get("liquidityClob", 0) or 0)
    best_liq = max(liq, liq_clob)
    end_str = m.get("endDate", "")
    tags = m.get("tags", []) or []
    group_slug = m.get("groupSlug", "")
    condition_id = m.get("conditionId", "")
    clob_token_ids = m.get("clobTokenIds", "")
    accepting = m.get("acceptingOrders", True)

    # Parse prices
    try:
        prices = json.loads(m.get("outcomePrices", "[]"))
        yes_p = float(prices[0])
        no_p = float(prices[1]) if len(prices) > 1 else 1 - yes_p
    except:
        return None

    # Hard filters
    if not accepting:
        return None
    if yes_p < 0.04 or yes_p > 0.96:
        return None  # Near-settled
    if best_liq < MIN_LIQUIDITY:
        return None
    if vol_24h < MIN_VOLUME_24H:
        return None

    # Days to expiry
    days_to_expiry = 999
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            days_to_expiry = max(0, (end_dt - datetime.now(timezone.utc)).days)
        except:
            pass

    # ─── SCORING (0-100 scale) ───
    score = 0

    # 1. Liquidity (0-25)
    if best_liq > 500000:
        score += 25
    elif best_liq > 200000:
        score += 22
    elif best_liq > 100000:
        score += 20
    elif best_liq > 50000:
        score += 17
    elif best_liq > 20000:
        score += 14
    elif best_liq > 10000:
        score += 10
    else:
        score += 5

    # 2. Volume activity (0-20)
    if vol_24h > 500000:
        score += 20
    elif vol_24h > 100000:
        score += 17
    elif vol_24h > 50000:
        score += 14
    elif vol_24h > 10000:
        score += 10
    elif vol_24h > 5000:
        score += 7
    else:
        score += 3

    # 3. Price competitiveness — closer to 50/50 = more uncertain = more opportunity (0-20)
    edge_from_center = abs(yes_p - 0.5)
    if edge_from_center < 0.08:
        score += 20
    elif edge_from_center < 0.15:
        score += 17
    elif edge_from_center < 0.25:
        score += 13
    elif edge_from_center < 0.35:
        score += 8
    elif edge_from_center < 0.45:
        score += 4

    # 4. Time horizon (0-15) — shorter = faster capital turnover
    if 1 <= days_to_expiry <= 3:
        score += 15
    elif 4 <= days_to_expiry <= 7:
        score += 13
    elif 8 <= days_to_expiry <= 14:
        score += 11
    elif 15 <= days_to_expiry <= 30:
        score += 8
    elif 31 <= days_to_expiry <= 90:
        score += 5
    elif days_to_expiry > 90:
        score += 2

    # 5. Volume/liquidity ratio — high ratio = active trading relative to depth (0-10)
    vl_ratio = vol_24h / best_liq if best_liq > 0 else 0
    if vl_ratio > 1.0:
        score += 10
    elif vl_ratio > 0.5:
        score += 8
    elif vl_ratio > 0.2:
        score += 5
    elif vl_ratio > 0.05:
        score += 3

    # 6. Total volume (market maturity) (0-10)
    if vol_total > 10000000:
        score += 10
    elif vol_total > 1000000:
        score += 8
    elif vol_total > 100000:
        score += 5
    else:
        score += 2

    # Classify
    category = classify_market(question, tags)

    # Parse token IDs
    token_ids = []
    if clob_token_ids:
        try:
            if isinstance(clob_token_ids, str):
                token_ids = json.loads(clob_token_ids)
            else:
                token_ids = clob_token_ids
        except:
            pass

    return {
        "question": question,
        "yes": yes_p,
        "no": no_p,
        "liq": best_liq,
        "vol_24h": vol_24h,
        "vol_total": vol_total,
        "end": end_str[:10] if end_str else "?",
        "days": days_to_expiry,
        "score": score,
        "category": category,
        "condition_id": condition_id,
        "token_ids": token_ids,
        "group_slug": group_slug,
        "tags": tags,
        "accepting": accepting,
        "vl_ratio": round(vl_ratio, 3),
    }

def enrich_with_orderbook(opportunities, top_n=20):
    """Fetch CLOB orderbooks for top N opportunities via Tor. Adds spread + depth."""
    if not opportunities:
        return opportunities

    tor_sess = tor_session()

    # Test Tor connectivity
    try:
        t = tor_sess.get(f"{CLOB_API}/time", timeout=10)
        if t.status_code != 200:
            print("[WARN] Tor/CLOB unreachable, skipping orderbook enrichment", file=sys.stderr)
            return opportunities
    except Exception as e:
        print(f"[WARN] Tor error: {e}, skipping orderbook enrichment", file=sys.stderr)
        return opportunities

    enriched = 0
    for opp in opportunities[:top_n]:
        token_ids = opp.get("token_ids", [])
        if not token_ids:
            continue

        # Fetch YES token orderbook
        yes_token = token_ids[0] if len(token_ids) > 0 else None
        if not yes_token:
            continue

        book = fetch_clob_orderbook(yes_token, tor_sess)
        if book:
            best_bid, best_ask = get_best_bid_ask(book)
            bid_depth, ask_depth = get_book_depth(book)

            opp["best_bid"] = best_bid
            opp["best_ask"] = best_ask
            opp["spread"] = round(best_ask - best_bid, 4) if best_bid and best_ask else None
            opp["bid_depth"] = round(bid_depth, 2)
            opp["ask_depth"] = round(ask_depth, 2)

            # Adjust score based on spread
            spread = opp["spread"]
            if spread is not None:
                if spread <= 0.02:
                    opp["score"] += 5  # Tight spread bonus
                elif spread <= 0.04:
                    opp["score"] += 3
                elif spread > MAX_SPREAD:
                    opp["score"] -= 5  # Wide spread penalty

            enriched += 1
            time.sleep(0.15)  # Rate limit

    print(f"[INFO] Enriched {enriched}/{min(top_n, len(opportunities))} markets with orderbook data", file=sys.stderr)
    return opportunities

# ─── Output ───

CAT_EMOJI = {
    "crypto": "🪙",
    "sports": "⚽",
    "politics": "🏛️",
    "economics": "📊",
    "geopolitics": "🌍",
    "tech": "🤖",
    "culture": "🎬",
    "other": "📦",
}

def print_report(opportunities, category_filter=None, show_execution=False):
    """Print formatted scanner report."""
    if category_filter:
        opportunities = [o for o in opportunities if o["category"] == category_filter]

    if not opportunities:
        print("No opportunities found matching criteria.")
        return

    print(f"\n{'='*80}")
    print(f"🌮 TACO POLYMARKET SCANNER v2 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}")
    print(f"Markets analyzed: {len(opportunities)} | Min score: {MIN_SCORE}")
    if category_filter:
        print(f"Category filter: {category_filter}")

    # Filter by min score
    top = [o for o in opportunities if o["score"] >= MIN_SCORE]

    # Category breakdown
    cats = {}
    for o in top:
        cats.setdefault(o["category"], []).append(o)

    for cat in ["crypto", "sports", "politics", "economics", "geopolitics", "tech", "culture", "other"]:
        items = cats.get(cat, [])
        if not items:
            continue
        emoji = CAT_EMOJI.get(cat, "📦")
        print(f"\n{emoji} {cat.upper()} ({len(items)} markets)")
        print("-" * 60)
        for o in items[:5]:
            spread_str = f"spread:{o['spread']:.3f}" if o.get("spread") is not None else "spread:?"
            print(f"  Score:{o['score']:3d} | ${o['liq']:>10,.0f} liq | {spread_str} | {o['end']} ({o['days']}d)")
            print(f"  YES:{o['yes']:.3f}  NO:{o['no']:.3f} | Vol24h:${o['vol_24h']:>10,.0f} | VL:{o['vl_ratio']}")
            print(f"  {o['question'][:95]}")
            if show_execution and o.get("condition_id"):
                print(f"  → cond:{o['condition_id'][:20]}... | tokens:{len(o.get('token_ids',[]))} | slug:{o.get('group_slug','')[:30]}")
            print()

    # Top 15 overall
    print(f"\n{'='*80}")
    print(f"🏆 TOP 15 OVERALL (score >= {MIN_SCORE})")
    print(f"{'='*80}")
    for i, o in enumerate(top[:15], 1):
        emoji = CAT_EMOJI.get(o["category"], "📦")
        spread_str = f"{o['spread']:.3f}" if o.get("spread") is not None else "?"
        print(f"\n#{i:2d} {emoji} [{o['category'].upper()}] Score: {o['score']}")
        print(f"    {o['question'][:100]}")
        print(f"    YES:{o['yes']:.3f} | Liq:${o['liq']:,.0f} | Vol24h:${o['vol_24h']:,.0f} | Spread:{spread_str} | {o['end']} ({o['days']}d)")
        if show_execution and o.get("condition_id"):
            print(f"    cond_id: {o['condition_id']}")
            if o.get("token_ids"):
                print(f"    YES_token: {o['token_ids'][0][:40]}...")

    # Summary stats
    print(f"\n{'─'*40}")
    cat_counts = {cat: len(items) for cat, items in cats.items()}
    print(f"Category breakdown: {json.dumps(cat_counts)}")
    print(f"Total above threshold: {len(top)}")
    avg_score = sum(o['score'] for o in top) / len(top) if top else 0
    print(f"Average score: {avg_score:.1f}")

def output_json(opportunities, category_filter=None):
    """Output JSON for piping to other tools."""
    if category_filter:
        opportunities = [o for o in opportunities if o["category"] == category_filter]
    top = [o for o in opportunities if o["score"] >= MIN_SCORE]
    print(json.dumps(top, indent=2, default=str))

# ─── Main ───

def main():
    args = sys.argv[1:]
    category_filter = None
    json_mode = "--json" in args
    show_execution = "--execute" in args

    if "--category" in args:
        idx = args.index("--category")
        if idx + 1 < len(args):
            category_filter = args[idx + 1].lower()

    if not json_mode:
        print("🌮 Taco Polymarket Scanner v2")
        print("Fetching markets from Gamma API...\n")

    # Step 1: Fetch all active markets
    raw_markets = fetch_gamma_markets(limit=500)
    if not json_mode:
        print(f"Fetched {len(raw_markets)} active markets")

    # Step 2: Analyze and score
    opportunities = []
    for m in raw_markets:
        result = analyze_market(m)
        if result:
            opportunities.append(result)

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    if not json_mode:
        print(f"Passed filters: {len(opportunities)} markets")

    # Step 3: Enrich top opportunities with CLOB orderbook data
    if not json_mode:
        print("Enriching top markets with orderbook data (via Tor)...")
    opportunities = enrich_with_orderbook(opportunities, top_n=25)

    # Re-sort after enrichment adjustments
    opportunities.sort(key=lambda x: x["score"], reverse=True)

    # Step 4: Output
    if json_mode:
        output_json(opportunities, category_filter)
    else:
        print_report(opportunities, category_filter, show_execution)

if __name__ == "__main__":
    main()
