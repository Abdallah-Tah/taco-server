#!/usr/bin/env python3
"""
scoring.py — Composite momentum scoring for Taco Trader.

Score breakdown (0–100 scale):
  40% — 1h price change
  30% — volume acceleration (h1 vol vs h6 vol/6)
  15% — liquidity depth
  15% — holder trend proxy (buy/sell txn ratio)
"""


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def compute_score(pair_data: dict) -> dict:
    """
    Compute a composite momentum score from a DexScreener pair dict.

    Expected keys in pair_data (all optional, default to 0):
        priceChange.h1      float  — 1h price change %
        priceChange.h6      float  — 6h price change %
        volume.h1           float  — 1h USD volume
        volume.h6           float  — 6h USD volume
        liquidity.usd       float  — pool liquidity in USD
        txns.h1.buys        int    — buy transactions last 1h
        txns.h1.sells       int    — sell transactions last 1h

    Returns:
        {
            "score":         float,  # 0–100 composite
            "h1_component":  float,
            "vol_component": float,
            "liq_component": float,
            "holder_component": float,
            "vol_acceleration": float,  # ratio h1_vol / (h6_vol/6), useful externally
        }
    """
    pc = pair_data.get("priceChange", {}) or {}
    vol = pair_data.get("volume", {}) or {}
    liq = pair_data.get("liquidity", {}) or {}
    txns_h1 = (pair_data.get("txns", {}) or {}).get("h1", {}) or {}

    h1_pct  = float(pc.get("h1", 0) or 0)
    h6_vol  = float(vol.get("h6", 0) or 0)
    h1_vol  = float(vol.get("h1", 0) or 0)
    liq_usd = float(liq.get("usd", 0) or 0)
    buys    = int(txns_h1.get("buys", 0) or 0)
    sells   = int(txns_h1.get("sells", 0) or 0)

    # ── 1h price change (40%) ──────────────────────────────────────────────────
    # Map [-20, +80] → [0, 40].  Below -20 gives 0; above 80 capped at 40.
    h1_norm = _clamp((h1_pct + 20) / 100.0, 0.0, 1.0)
    h1_component = h1_norm * 40.0

    # ── Volume acceleration (30%) ──────────────────────────────────────────────
    # Compare h1_vol to h6_vol/6 (hourly average).
    # Ratio of 1x → 15 pts, 2x → 30 pts; below 0.5x → 0 pts.
    h6_hourly = h6_vol / 6.0 if h6_vol > 0 else 0.0
    if h6_hourly > 0:
        vol_accel = h1_vol / h6_hourly
    elif h1_vol > 0:
        vol_accel = 2.0  # no h6 data but h1 has volume — treat as decent signal
    else:
        vol_accel = 0.0

    vol_norm = _clamp((vol_accel - 0.5) / 2.0, 0.0, 1.0)   # 0 at 0.5x, 1 at 2.5x
    vol_component = vol_norm * 30.0

    # ── Liquidity depth (15%) ──────────────────────────────────────────────────
    # $40k → 0 pts,  $250k+ → 15 pts  (log-linear)
    LIQ_MIN = 40_000.0
    LIQ_MAX = 250_000.0
    if liq_usd <= LIQ_MIN:
        liq_norm = 0.0
    elif liq_usd >= LIQ_MAX:
        liq_norm = 1.0
    else:
        import math
        liq_norm = math.log(liq_usd / LIQ_MIN) / math.log(LIQ_MAX / LIQ_MIN)
    liq_component = liq_norm * 15.0

    # ── Holder trend proxy / buy pressure (15%) ────────────────────────────────
    # Use buy/sell txn ratio as a proxy for holder count trend.
    # Ratio >= 2x → 15 pts; 1x → 7.5 pts; 0.5x → 0 pts.
    total = buys + sells
    if total > 0:
        buy_ratio = buys / total           # 0..1
    else:
        buy_ratio = 0.5                    # neutral when no data
    holder_norm = _clamp((buy_ratio - 0.33) / 0.34, 0.0, 1.0)  # 0 at 33%, 1 at 67%+
    holder_component = holder_norm * 15.0

    score = h1_component + vol_component + liq_component + holder_component

    return {
        "score": round(score, 3),
        "h1_component": round(h1_component, 3),
        "vol_component": round(vol_component, 3),
        "liq_component": round(liq_component, 3),
        "holder_component": round(holder_component, 3),
        "vol_acceleration": round(vol_accel, 3),
    }
