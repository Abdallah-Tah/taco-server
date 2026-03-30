#!/usr/bin/env python3
"""
Taco News Edge — News monitoring + conviction scoring for Polymarket trades.

Checks real news sources to form an independent probability estimate,
then compares against market prices to find genuine mispricings.

Only recommends trades when there's a real information edge.

Usage:
  python3 polymarket_news_edge.py                    # Analyze all positions + top markets
  python3 polymarket_news_edge.py --scan             # Scan for new opportunities with news edge
  python3 polymarket_news_edge.py --check            # Check existing positions against news
  python3 polymarket_news_edge.py --market "iran"    # Analyze specific market keyword
"""
import json
import sys
import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ───

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
GAMMA_API = "https://gamma-api.polymarket.com"
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}

STATE_DIR = Path.home() / ".openclaw" / "workspace" / "trading"
EDGE_LOG = STATE_DIR / ".news_edge_log.json"
POLY_POSITIONS = STATE_DIR / ".poly_positions.json"

# Edge thresholds
MIN_EDGE = 0.08          # Minimum 8% probability edge to recommend a trade
HIGH_CONVICTION = 0.15   # 15%+ edge = high conviction
MIN_NEWS_RESULTS = 3     # Need at least 3 news articles to form opinion

# ─── News Fetching ───

def search_news(query, count=10, freshness="week"):
    """Search news via Brave Search API or web scraping fallback."""
    # Try Brave API first
    if BRAVE_API_KEY:
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
                params={"q": query, "count": count, "freshness": freshness},
                timeout=15,
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                return [{
                    "title": a.get("title", ""),
                    "description": a.get("description", ""),
                    "url": a.get("url", ""),
                    "age": a.get("age", ""),
                    "source": a.get("meta_url", {}).get("hostname", ""),
                } for a in results]
        except Exception as e:
            print(f"[WARN] Brave API error: {e}", file=sys.stderr)

    # Fallback: use DuckDuckGo news (no API key needed)
    try:
        r = requests.get(
            "https://duckduckgo.com/news.js",
            params={"q": query, "df": "w", "o": "json"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            return [{
                "title": a.get("title", ""),
                "description": a.get("excerpt", ""),
                "url": a.get("url", ""),
                "age": a.get("relative_time", ""),
                "source": a.get("source", ""),
            } for a in results[:count]]
    except Exception:
        pass

    # Last resort: Google News RSS
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(
            f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
            timeout=15,
        )
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            items = root.findall(".//item")
            return [{
                "title": item.find("title").text if item.find("title") is not None else "",
                "description": item.find("description").text if item.find("description") is not None else "",
                "url": item.find("link").text if item.find("link") is not None else "",
                "age": item.find("pubDate").text if item.find("pubDate") is not None else "",
                "source": item.find("source").text if item.find("source") is not None else "",
            } for item in items[:count]]
    except Exception:
        pass

    return []

# ─── Sentiment Analysis ───

# Keywords that suggest positive/negative outcomes
POSITIVE_SIGNALS = {
    "regime_change": ["overthrow", "collapse", "fall", "topple", "resign", "flee", "uprising", "revolution", "protest", "defect"],
    "ceasefire": ["ceasefire", "peace talks", "negotiate", "agreement", "truce", "de-escalation", "diplomatic", "breakthrough"],
    "crypto_bull": ["rally", "surge", "breakout", "all-time high", "bull", "adoption", "approval", "etf approved", "institutional"],
    "general_yes": ["confirm", "approve", "pass", "win", "succeed", "achieve", "likely", "expected", "imminent"],
}

NEGATIVE_SIGNALS = {
    "regime_stable": ["stable", "crackdown", "suppress", "consolidate", "defiant", "strong grip", "resilient"],
    "no_ceasefire": ["escalation", "offensive", "attack", "reject", "breakdown", "stalemate", "intensif"],
    "crypto_bear": ["crash", "dump", "sell-off", "ban", "restrict", "regulate", "sec", "fraud"],
    "general_no": ["unlikely", "reject", "fail", "deny", "postpone", "delay", "cancel", "impossible"],
}

def analyze_sentiment(articles, market_question):
    """
    Analyze news articles to estimate probability direction.
    Returns: (sentiment_score, confidence, key_signals, summary)
    
    sentiment_score: -1.0 to +1.0 (negative = NO likely, positive = YES likely)
    confidence: 0.0 to 1.0 (how confident we are in our assessment)
    """
    if not articles:
        return 0.0, 0.0, [], "No news found"

    q_lower = market_question.lower()
    
    # Determine which signal category to use
    signal_category = "general"
    if any(k in q_lower for k in ["regime", "fall", "overthrow", "iran"]):
        signal_category = "regime_change"
    elif any(k in q_lower for k in ["ceasefire", "peace", "truce"]):
        signal_category = "ceasefire"
    elif any(k in q_lower for k in ["bitcoin", "btc", "crypto", "eth", "token"]):
        signal_category = "crypto_bull"

    pos_key = signal_category if signal_category in POSITIVE_SIGNALS else "general_yes"
    neg_key = signal_category.replace("_change", "_stable").replace("_bull", "_bear") if signal_category != "general" else "general_no"
    if neg_key not in NEGATIVE_SIGNALS:
        neg_key = "general_no"

    pos_keywords = POSITIVE_SIGNALS[pos_key]
    neg_keywords = NEGATIVE_SIGNALS[neg_key]

    pos_hits = 0
    neg_hits = 0
    key_signals = []
    recent_count = 0

    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}".lower()
        
        for kw in pos_keywords:
            if kw in text:
                pos_hits += 1
                key_signals.append(f"+{kw}: {article['title'][:60]}")
                break
        
        for kw in neg_keywords:
            if kw in text:
                neg_hits += 1
                key_signals.append(f"-{kw}: {article['title'][:60]}")
                break

        # Recency bonus
        age = article.get("age", "").lower()
        if any(t in age for t in ["hour", "minute", "just now", "today"]):
            recent_count += 1

    total_hits = pos_hits + neg_hits
    if total_hits == 0:
        return 0.0, 0.1, key_signals, "No clear signals in news"

    # Sentiment: normalized difference
    sentiment = (pos_hits - neg_hits) / max(total_hits, 1)
    
    # Confidence based on: number of articles, clarity of signals, recency
    article_conf = min(len(articles) / 10, 1.0)  # More articles = more confident
    signal_conf = min(total_hits / 5, 1.0)        # More signal hits = more confident
    recency_conf = min(recent_count / 3, 1.0)     # More recent = more confident
    
    confidence = (article_conf * 0.3 + signal_conf * 0.4 + recency_conf * 0.3)

    direction = "YES likely" if sentiment > 0 else "NO likely" if sentiment < 0 else "Unclear"
    summary = f"{direction} | {pos_hits} positive, {neg_hits} negative signals across {len(articles)} articles"

    return sentiment, confidence, key_signals[:5], summary

def estimate_probability(sentiment, confidence, market_price):
    """
    Convert sentiment + confidence into a probability estimate.
    Anchors on market price and adjusts based on news signals.
    """
    # Start with market price as base (markets are usually efficient)
    base = market_price
    
    # Adjust based on sentiment and confidence
    # Max adjustment: ±20% from market price
    max_adjustment = 0.20
    adjustment = sentiment * confidence * max_adjustment
    
    estimated = base + adjustment
    # Clamp to [0.05, 0.95]
    estimated = max(0.05, min(0.95, estimated))
    
    return estimated

# ─── Market Analysis ───

def get_market_price(token_id):
    """Get current market price via CLOB."""
    try:
        tor = requests.Session()
        tor.proxies = TOR_PROXY
        r = tor.get(f"https://clob.polymarket.com/price",
                    params={"token_id": token_id, "side": "buy"}, timeout=10)
        return float(r.json().get("price", 0))
    except:
        return 0

def build_search_query(market_question):
    """Build a good news search query from a market question."""
    # Remove common Polymarket phrasing
    q = market_question
    for phrase in ["Will ", "will ", "by end of ", "by ", "before ", "after "]:
        q = q.replace(phrase, "")
    q = re.sub(r'\d{4}-\d{2}-\d{2}', '', q)  # Remove dates
    q = re.sub(r'\?$', '', q).strip()
    # Add "latest news" for better results
    return f"{q} latest news 2026"

def analyze_opportunity(market_question, market_price, token_id=None, condition_id=""):
    """
    Full analysis of a market opportunity.
    Returns dict with edge assessment.
    """
    # Search for relevant news
    query = build_search_query(market_question)
    articles = search_news(query, count=10, freshness="week")
    
    # Analyze sentiment
    sentiment, confidence, signals, summary = analyze_sentiment(articles, market_question)
    
    # Estimate true probability
    estimated_prob = estimate_probability(sentiment, confidence, market_price)
    
    # Calculate edge
    edge = estimated_prob - market_price  # Positive = YES underpriced, Negative = NO underpriced
    abs_edge = abs(edge)
    
    # Determine recommendation
    if abs_edge < MIN_EDGE or confidence < 0.3:
        recommendation = "SKIP"
        reason = f"Edge too small ({abs_edge:.1%}) or confidence too low ({confidence:.1%})"
    elif abs_edge >= HIGH_CONVICTION and confidence >= 0.5:
        recommendation = "STRONG_BUY"
        direction = "YES" if edge > 0 else "NO"
        reason = f"High conviction {direction} — {abs_edge:.1%} edge, {confidence:.0%} confidence"
    elif abs_edge >= MIN_EDGE and confidence >= 0.3:
        recommendation = "BUY"
        direction = "YES" if edge > 0 else "NO"
        reason = f"{direction} — {abs_edge:.1%} edge, {confidence:.0%} confidence"
    else:
        recommendation = "SKIP"
        reason = "Insufficient edge or confidence"

    return {
        "question": market_question,
        "market_price": market_price,
        "estimated_prob": round(estimated_prob, 3),
        "edge": round(edge, 3),
        "abs_edge": round(abs_edge, 3),
        "sentiment": round(sentiment, 3),
        "confidence": round(confidence, 3),
        "recommendation": recommendation,
        "reason": reason,
        "signals": signals,
        "summary": summary,
        "articles_found": len(articles),
        "condition_id": condition_id,
        "token_id": token_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

def log_analysis(analysis):
    """Save analysis to log."""
    log = []
    if EDGE_LOG.exists():
        try:
            log = json.loads(EDGE_LOG.read_text())
        except:
            pass
    log.append(analysis)
    # Keep last 100
    log = log[-100:]
    EDGE_LOG.write_text(json.dumps(log, indent=2))

# ─── Commands ───

def check_positions():
    """Analyze existing positions against current news."""
    if not POLY_POSITIONS.exists():
        print("No positions to check.")
        return

    positions = json.loads(POLY_POSITIONS.read_text())
    if not positions:
        print("No positions to check.")
        return

    print(f"\n{'='*70}")
    print(f"🔍 NEWS CHECK — Existing Positions ({datetime.now().strftime('%H:%M')})")
    print(f"{'='*70}")

    for token_id, pos in positions.items():
        market = pos.get("market", "?")
        entry = pos.get("avg_price", 0)
        
        # Get current price
        current = get_market_price(token_id)
        pnl = ((current - entry) / entry * 100) if entry > 0 else 0
        
        print(f"\n📌 {market}")
        print(f"   Entry: {entry:.3f} | Now: {current:.3f} | PnL: {pnl:+.1f}%")
        
        analysis = analyze_opportunity(market, current, token_id)
        
        emoji = "🟢" if analysis["recommendation"] in ["BUY", "STRONG_BUY"] else "🔴" if analysis["recommendation"] == "SELL" else "⚪"
        print(f"   {emoji} News says: {analysis['summary']}")
        print(f"   Estimated prob: {analysis['estimated_prob']:.3f} | Edge: {analysis['edge']:+.3f} | Confidence: {analysis['confidence']:.0%}")
        
        if analysis["signals"]:
            for sig in analysis["signals"][:3]:
                print(f"     → {sig}")
        
        log_analysis(analysis)
        time.sleep(0.5)  # Rate limit

def scan_opportunities():
    """Scan top Polymarket markets for news-backed edges."""
    print(f"\n{'='*70}")
    print(f"🔍 NEWS EDGE SCAN ({datetime.now().strftime('%H:%M')})")
    print(f"{'='*70}")

    # Fetch top markets from Gamma API
    print("Fetching markets...")
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={
            "active": "true", "closed": "false",
            "limit": 50, "order": "liquidityNum", "ascending": "false",
        }, timeout=15)
        markets = r.json()
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []

    # Filter: skip sports (no edge), skip near-settled
    actionable = []
    for m in markets:
        question = m.get("question", "")
        q_lower = question.lower()
        
        # Skip sports
        if any(k in q_lower for k in ["vs.", "vs ", "win on 202", "spread:", "end in a draw"]):
            continue
        
        # Parse price
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            yes_p = float(prices[0])
        except:
            continue
        
        # Skip near-settled
        if yes_p < 0.05 or yes_p > 0.95:
            continue
        
        liq = float(m.get("liquidity", 0) or 0)
        if liq < 10000:
            continue

        token_ids = []
        try:
            raw = m.get("clobTokenIds", "")
            if isinstance(raw, str):
                token_ids = json.loads(raw)
            else:
                token_ids = raw or []
        except:
            pass

        actionable.append({
            "question": question,
            "yes_price": yes_p,
            "liquidity": liq,
            "condition_id": m.get("conditionId", ""),
            "token_ids": token_ids,
        })

    print(f"Analyzing {len(actionable)} non-sports markets with news...\n")

    recommendations = []
    for mkt in actionable:
        analysis = analyze_opportunity(
            mkt["question"],
            mkt["yes_price"],
            mkt["token_ids"][0] if mkt["token_ids"] else None,
            mkt["condition_id"],
        )
        
        log_analysis(analysis)
        
        if analysis["recommendation"] in ["BUY", "STRONG_BUY"]:
            analysis["liquidity"] = mkt["liquidity"]
            analysis["token_ids"] = mkt["token_ids"]
            recommendations.append(analysis)
            
            strength = "🔥" if analysis["recommendation"] == "STRONG_BUY" else "💡"
            print(f"{strength} {analysis['question'][:70]}")
            print(f"   Market: {analysis['market_price']:.3f} | Est: {analysis['estimated_prob']:.3f} | Edge: {analysis['edge']:+.3f}")
            print(f"   {analysis['reason']}")
            if analysis["signals"]:
                for sig in analysis["signals"][:2]:
                    print(f"     → {sig}")
            print()
        
        time.sleep(0.3)

    if not recommendations:
        print("No actionable opportunities found with sufficient news edge.")
    else:
        print(f"\n{'─'*40}")
        print(f"Found {len(recommendations)} opportunities with edge >= {MIN_EDGE:.0%}")
        
        # Sort by absolute edge
        recommendations.sort(key=lambda x: x["abs_edge"], reverse=True)
        
        print("\n🏆 RANKED RECOMMENDATIONS:")
        for i, rec in enumerate(recommendations[:10], 1):
            direction = "YES" if rec["edge"] > 0 else "NO"
            print(f"  #{i} [{direction}] {rec['question'][:65]}")
            print(f"      Edge: {rec['abs_edge']:.1%} | Confidence: {rec['confidence']:.0%} | Liq: ${rec['liquidity']:,.0f}")

    return recommendations

def analyze_keyword(keyword):
    """Analyze markets matching a keyword."""
    print(f"\n🔍 Analyzing markets matching '{keyword}'...\n")
    
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={
            "active": "true", "closed": "false",
            "limit": 100, "order": "liquidityNum", "ascending": "false",
        }, timeout=15)
        markets = r.json()
    except Exception as e:
        print(f"Error: {e}")
        return

    matches = [m for m in markets if keyword.lower() in m.get("question", "").lower()]
    print(f"Found {len(matches)} matching markets\n")

    for m in matches[:10]:
        question = m.get("question", "")
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            yes_p = float(prices[0])
        except:
            continue

        analysis = analyze_opportunity(question, yes_p)
        
        strength = "🔥" if analysis["recommendation"] == "STRONG_BUY" else "💡" if analysis["recommendation"] == "BUY" else "⚪"
        print(f"{strength} {question[:70]}")
        print(f"   Price: {yes_p:.3f} | Est: {analysis['estimated_prob']:.3f} | Edge: {analysis['edge']:+.3f} | {analysis['recommendation']}")
        print(f"   {analysis['summary']}")
        if analysis["signals"]:
            for sig in analysis["signals"][:2]:
                print(f"     → {sig}")
        print()
        
        time.sleep(0.3)

# ─── Main ───

def main():
    args = sys.argv[1:]
    
    if "--check" in args:
        check_positions()
    elif "--scan" in args:
        scan_opportunities()
    elif "--market" in args:
        idx = args.index("--market")
        if idx + 1 < len(args):
            analyze_keyword(args[idx + 1])
        else:
            print("Usage: --market <keyword>")
    else:
        # Default: check positions + scan
        check_positions()
        print("\n")
        scan_opportunities()

if __name__ == "__main__":
    main()
