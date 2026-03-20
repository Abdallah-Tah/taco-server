#!/usr/bin/env python3
"""
Polymarket Scanner - Find and analyze trading opportunities.
Scans all active markets, scores them, and identifies the best bets.
"""
import requests
import json
import sys
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"

def fetch_markets(limit=500):
    """Fetch all active markets."""
    all_markets = []
    offset = 0
    while offset < limit:
        batch = min(100, limit - offset)
        r = requests.get(f"{GAMMA_API}/markets", params={
            'active': 'true', 'closed': 'false', 
            'limit': batch, 'offset': offset,
            'order': 'liquidityNum', 'ascending': 'false'
        })
        data = r.json()
        if not data:
            break
        all_markets.extend(data)
        offset += batch
    return all_markets

def analyze_market(m):
    """Score a market for trading potential."""
    vol = float(m.get('volume', 0) or 0)
    liq = float(m.get('liquidity', 0) or 0)
    prices = json.loads(m.get('outcomePrices', '[]'))
    if not prices:
        return None
    
    yes_p = float(prices[0])
    no_p = float(prices[1]) if len(prices) > 1 else 1 - yes_p
    end_str = m.get('endDate', '')
    question = m.get('question', '?')
    tags = m.get('tags', []) or []
    
    # Skip near-settled markets
    if yes_p < 0.03 or yes_p > 0.97:
        return None
    
    # Skip very low liquidity
    if liq < 1000:
        return None
    
    # Calculate days to expiry
    days_to_expiry = 999
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            days_to_expiry = (end_dt - datetime.now(timezone.utc)).days
        except:
            pass
    
    # SCORING
    score = 0
    
    # Liquidity score (higher = better, can enter/exit easily)
    if liq > 100000: score += 30
    elif liq > 50000: score += 25
    elif liq > 20000: score += 20
    elif liq > 10000: score += 15
    elif liq > 5000: score += 10
    else: score += 5
    
    # Volume score (active markets)
    if vol > 1000000: score += 20
    elif vol > 100000: score += 15
    elif vol > 10000: score += 10
    else: score += 5
    
    # Odds competitiveness (closer to 50/50 = more uncertain = more potential)
    edge = abs(yes_p - 0.5)
    if edge < 0.10: score += 20  # Very competitive
    elif edge < 0.20: score += 15
    elif edge < 0.30: score += 10
    elif edge < 0.40: score += 5
    
    # Time value (shorter = faster resolution = faster profit)
    if 1 <= days_to_expiry <= 7: score += 15
    elif 7 < days_to_expiry <= 30: score += 10
    elif 30 < days_to_expiry <= 90: score += 5
    
    # Categorize
    category = 'other'
    q_lower = question.lower()
    tag_str = ' '.join(tags).lower() if tags else ''
    
    if any(k in q_lower or k in tag_str for k in ['bitcoin', 'btc', 'eth', 'crypto', 'token', 'solana', 'defi']):
        category = 'crypto'
    elif any(k in q_lower or k in tag_str for k in ['trump', 'biden', 'president', 'election', 'congress', 'senate', 'democrat', 'republican']):
        category = 'politics'
    elif any(k in q_lower or k in tag_str for k in ['nba', 'nfl', 'mlb', 'premier league', 'champions', 'world cup', 'tennis', 'f1', 'ufc']):
        category = 'sports'
    elif any(k in q_lower or k in tag_str for k in ['fed', 'interest rate', 'inflation', 'gdp', 'cpi']):
        category = 'economics'
    elif any(k in q_lower or k in tag_str for k in ['iran', 'ukraine', 'war', 'military', 'ceasefire', 'regime']):
        category = 'geopolitics'
    
    return {
        'question': question,
        'yes': yes_p,
        'no': no_p,
        'liq': liq,
        'vol': vol,
        'end': end_str[:10] if end_str else '?',
        'days': days_to_expiry,
        'score': score,
        'category': category,
        'condition_id': m.get('conditionId', ''),
        'tokens': m.get('clobTokenIds', ''),
    }

def main():
    print("🔍 Scanning Polymarket...\n")
    markets = fetch_markets(500)
    print(f"Fetched {len(markets)} active markets\n")
    
    analyzed = []
    for m in markets:
        result = analyze_market(m)
        if result:
            analyzed.append(result)
    
    analyzed.sort(key=lambda x: x['score'], reverse=True)
    
    print(f"Found {len(analyzed)} tradeable markets\n")
    print("=" * 80)
    
    # Top opportunities by category
    categories = {}
    for a in analyzed:
        cat = a['category']
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(a)
    
    for cat in ['crypto', 'economics', 'geopolitics', 'politics', 'sports', 'other']:
        items = categories.get(cat, [])
        if not items:
            continue
        print(f"\n{'🪙' if cat=='crypto' else '📊' if cat=='economics' else '🌍' if cat=='geopolitics' else '🏛️' if cat=='politics' else '⚽' if cat=='sports' else '📦'} {cat.upper()} ({len(items)} markets)")
        print("-" * 60)
        for a in items[:5]:
            print(f"  Score: {a['score']:3d} | ${a['liq']:>10,.0f} liq | End: {a['end']} ({a['days']}d)")
            print(f"  {a['question'][:90]}")
            print(f"  YES: {a['yes']:.3f}  NO: {a['no']:.3f}")
            print()
    
    # Overall top 10
    print("\n" + "=" * 80)
    print("🏆 TOP 10 OVERALL OPPORTUNITIES")
    print("=" * 80)
    for i, a in enumerate(analyzed[:10], 1):
        print(f"\n#{i} [Score: {a['score']}] [{a['category'].upper()}]")
        print(f"  {a['question'][:100]}")
        print(f"  YES: {a['yes']:.3f}  NO: {a['no']:.3f}")
        print(f"  Liq: ${a['liq']:,.0f} | Vol: ${a['vol']:,.0f} | Expires: {a['end']} ({a['days']}d)")

if __name__ == '__main__':
    main()
