#!/usr/bin/env python3
"""
Taco Polymarket Edge Finder — LMSR-based mispriced market scanner.
Finds markets where Polymarket odds diverge from external data sources.
"""
import json
import math
import requests
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"

# ─── LMSR Math ───

def lmsr_price(q, b, outcome=0):
    """Calculate LMSR price for an outcome."""
    exp_q = [math.exp(qi / b) for qi in q]
    return exp_q[outcome] / sum(exp_q)

def lmsr_cost(q, b):
    """Cost function C(q) = b * ln(sum(e^(qi/b)))."""
    return b * math.log(sum(math.exp(qi / b) for qi in q))

def cost_of_trade(q_before, q_after, b):
    """Cost to move from q_before to q_after."""
    return lmsr_cost(q_after, b) - lmsr_cost(q_before, b)

def expected_value(market_price, true_prob):
    """EV of buying YES at market_price when true prob is true_prob."""
    return true_prob * (1 - market_price) - (1 - true_prob) * market_price

def kelly_fraction(market_price, true_prob, kelly_mult=0.25):
    """Quarter-Kelly sizing for prediction markets."""
    if true_prob <= market_price:
        return 0  # No edge
    edge = true_prob - market_price
    odds = (1 - market_price) / market_price  # decimal odds - 1
    kelly = (true_prob * odds - (1 - true_prob)) / odds
    return max(0, kelly * kelly_mult)

def simulate_buy(initial_q, shares, b, outcome=0, steps=20):
    """Simulate buying shares and calculate average fill + price impact."""
    q = list(initial_q)
    total_cost = 0
    per_step = shares / steps
    
    for i in range(steps):
        q_after = list(q)
        q_after[outcome] += per_step
        step_cost = cost_of_trade(q, q_after, b)
        total_cost += step_cost
        q = q_after
    
    avg_fill = total_cost / shares if shares > 0 else 0
    final_price = lmsr_price(q, b, outcome)
    
    return {
        "avg_fill": avg_fill,
        "final_price": final_price,
        "total_cost": total_cost,
        "price_impact": final_price - lmsr_price(initial_q, b, outcome)
    }

# ─── Market Scanner ───

def fetch_markets(limit=100, min_volume=10000, active_only=True):
    """Fetch active Polymarket markets."""
    params = {
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
        "active": "true" if active_only else "false",
    }
    
    try:
        r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []
    
    results = []
    for m in markets:
        try:
            volume = float(m.get("volume", 0) or 0)
            volume_24h = float(m.get("volume24hr", 0) or 0)
            liquidity = float(m.get("liquidityClob", 0) or 0)
            
            if volume_24h < min_volume:
                continue
            
            # Get YES/NO prices
            outcomes = m.get("outcomePrices", "")
            if isinstance(outcomes, str) and outcomes:
                try:
                    prices = json.loads(outcomes)
                    yes_price = float(prices[0]) if len(prices) > 0 else None
                    no_price = float(prices[1]) if len(prices) > 1 else None
                except:
                    yes_price = None
                    no_price = None
            else:
                yes_price = None
                no_price = None
            
            results.append({
                "id": m.get("id", ""),
                "question": m.get("question", "")[:80],
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_24h": volume_24h,
                "volume_total": volume,
                "liquidity": liquidity,
                "end_date": m.get("endDate", ""),
                "category": m.get("groupSlug", ""),
            })
        except Exception as e:
            continue
    
    return results

def find_edges(markets, external_estimates=None):
    """
    Find mispriced markets.
    
    external_estimates: dict of market_id -> estimated_true_probability
    If not provided, uses heuristic signals to flag potential mispricings.
    """
    edges = []
    
    for m in markets:
        if m["yes_price"] is None:
            continue
        
        market_prob = m["yes_price"]
        
        # If we have an external estimate, calculate edge directly
        if external_estimates and m["id"] in external_estimates:
            true_prob = external_estimates[m["id"]]
            ev = expected_value(market_prob, true_prob)
            edge = true_prob - market_prob
            
            if abs(edge) > 0.05:  # Only flag >5% edge
                kelly = kelly_fraction(market_prob, true_prob)
                direction = "BUY YES" if edge > 0 else "BUY NO"
                
                edges.append({
                    **m,
                    "true_prob": true_prob,
                    "edge": edge,
                    "ev": ev,
                    "kelly": kelly,
                    "direction": direction,
                })
        else:
            # Heuristic: flag markets with extreme prices that might revert
            # Markets near 0.50 with high volume = contested = potential edge
            # Markets near 0.90+ or 0.10- with declining volume = potential overconfidence
            
            volatility_score = 0
            
            # High volume + close to 50/50 = contested market (opportunities)
            if 0.35 <= market_prob <= 0.65:
                volatility_score += 3
            
            # Very high volume signals active disagreement
            if m["volume_24h"] > 100000:
                volatility_score += 2
            elif m["volume_24h"] > 50000:
                volatility_score += 1
            
            # Good liquidity
            if m["liquidity"] > 50000:
                volatility_score += 1
            
            if volatility_score >= 3:
                edges.append({
                    **m,
                    "true_prob": None,
                    "edge": None,
                    "ev": None,
                    "kelly": None,
                    "direction": "RESEARCH",
                    "volatility_score": volatility_score,
                })
    
    # Sort by edge (known) or volatility score (heuristic)
    edges.sort(key=lambda x: abs(x.get("edge") or 0) + (x.get("volatility_score", 0) * 0.1), reverse=True)
    
    return edges

def print_report(edges):
    """Print edge finder report."""
    print(f"\n{'='*80}")
    print(f"🌮 TACO POLYMARKET EDGE FINDER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")
    
    known_edges = [e for e in edges if e["edge"] is not None]
    research = [e for e in edges if e["edge"] is None]
    
    if known_edges:
        print("🎯 CONFIRMED EDGES (external data vs market price):\n")
        for e in known_edges[:10]:
            print(f"  {e['direction']:8s} | {e['question']}")
            print(f"           Market: {e['yes_price']:.2f} | True: {e['true_prob']:.2f} | Edge: {e['edge']:+.2f} | EV: {e['ev']:+.3f}")
            print(f"           Vol24h: ${e['volume_24h']:,.0f} | Liq: ${e['liquidity']:,.0f} | Kelly: {e['kelly']:.1%}")
            print()
    
    if research:
        print("🔍 HIGH-OPPORTUNITY MARKETS (need research to find edge):\n")
        for e in research[:15]:
            side = "contested" if 0.35 <= e["yes_price"] <= 0.65 else "leaning"
            print(f"  {e['question']}")
            print(f"    YES: {e['yes_price']:.2f} | Vol24h: ${e['volume_24h']:,.0f} | Liq: ${e['liquidity']:,.0f} | [{side}]")
            print()

# ─── External Data Integrations ───

def get_fed_rate_probs():
    """
    Placeholder for CME FedWatch-style data.
    In production, scrape CME FedWatch or use bond market data.
    """
    # TODO: Integrate real FedWatch data
    return {}

def get_election_polls():
    """
    Placeholder for polling aggregator data.
    In production, use FiveThirtyEight, RCP, or similar.
    """
    # TODO: Integrate polling data
    return {}

# ─── Main ───

if __name__ == "__main__":
    import sys
    
    print("🌮 Taco Polymarket Edge Finder")
    print("Scanning markets...\n")
    
    # Fetch active markets
    markets = fetch_markets(limit=200, min_volume=5000)
    print(f"Found {len(markets)} active markets with >$5K daily volume\n")
    
    # For now, use heuristic mode (no external estimates)
    # When we add API integrations, we can pass external_estimates
    external = {}
    
    # Merge any external data
    external.update(get_fed_rate_probs())
    external.update(get_election_polls())
    
    edges = find_edges(markets, external if external else None)
    
    print_report(edges)
    
    # Summary
    print(f"\n{'─'*40}")
    print(f"Total markets scanned: {len(markets)}")
    print(f"Confirmed edges: {len([e for e in edges if e['edge'] is not None])}")
    print(f"Research candidates: {len([e for e in edges if e['edge'] is None])}")
    print(f"\nTo add external data sources, edit get_fed_rate_probs() and get_election_polls()")
    print(f"To trade: need Polymarket API key (CLOB API)")
