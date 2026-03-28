#!/usr/bin/env python3
"""Deterministic shadow scoring for edge telemetry."""
from __future__ import annotations

SHADOW_EDGE_FLOOR = 0.02
SHADOW_CONFIDENCE_FLOOR = 0.25

REGIME_THRESHOLDS = {
    "trend": {"edge_floor": 0.02, "confidence_floor": 0.20},
    "chop": {"edge_floor": 0.015, "confidence_floor": 0.18},
    "unstable": {"edge_floor": 0.025, "confidence_floor": 0.30},
}


def _clamp(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(hi, value))


def _available(values):
    return [v for v in values if v is not None]


def canonical_shadow_decision(decision):
    if decision == "shadow_trade":
        return "place_yes"
    if decision == "shadow_skip":
        return "skip_unknown"
    if isinstance(decision, str) and (decision.startswith("place_") or decision.startswith("skip_")):
        return decision
    return None


def shadow_is_trade(shadow_decision):
    return canonical_shadow_decision(shadow_decision) == "place_yes"


def shadow_is_skip(shadow_decision):
    canonical = canonical_shadow_decision(shadow_decision)
    return isinstance(canonical, str) and canonical.startswith("skip_")


def decide_shadow_outcome(
    net_edge,
    confidence=None,
    regime_ok=1,
    shadow_skip_reason=None,
    spread=None,
    edge_floor=SHADOW_EDGE_FLOOR,
    confidence_floor=SHADOW_CONFIDENCE_FLOOR,
):
    if regime_ok == 0:
        if shadow_skip_reason == "spread_too_wide" or (shadow_skip_reason is None and spread is not None and spread > 0.20):
            return "skip_spread", "spread_too_wide"
        return "skip_regime", shadow_skip_reason or "regime_not_ok"
    if net_edge is None:
        return "skip_data", "insufficient_features"
    if net_edge <= 0:
        return "skip_no_edge", "net_edge_non_positive"
    if net_edge < edge_floor:
        return "skip_no_edge", "net_edge_below_floor"
    if confidence is None:
        return "skip_no_edge", "confidence_unavailable"
    if confidence < confidence_floor:
        return "skip_no_edge", "confidence_below_floor"
    return "place_yes", None


def classify_btc_regime(snapshot):
    returns = _available([
        snapshot.get("ret_1s"),
        snapshot.get("ret_3s"),
        snapshot.get("ret_5s"),
        snapshot.get("ret_10s"),
        snapshot.get("ret_30s"),
    ])
    spread = snapshot.get("spread")
    vols = _available([snapshot.get("vol_10s"), snapshot.get("vol_30s"), snapshot.get("vol_60s")])
    vol_mean = sum(vols) / len(vols) if vols else None

    if len(returns) < 2:
        return {
            "regime": "unstable",
            "regime_ok": 0,
            "reason": "regime_uncertain",
            "trend_ratio": None,
            "flip_rate": None,
            "realized_vol": vol_mean,
            "spread_quality": None if spread is None else max(0.0, 1.0 - min(spread / 0.20, 1.0)),
            **REGIME_THRESHOLDS["unstable"],
        }

    signs = [1 if r > 0 else -1 if r < 0 else 0 for r in returns]
    non_zero_signs = [s for s in signs if s != 0]
    dominant = max(abs(sum(non_zero_signs)), 0) / max(len(non_zero_signs), 1)
    flips = 0
    comparisons = 0
    prev = None
    for sign in non_zero_signs:
        if prev is not None:
            comparisons += 1
            if sign != prev:
                flips += 1
        prev = sign
    flip_rate = (flips / comparisons) if comparisons else 0.0
    spread_quality = None if spread is None else max(0.0, 1.0 - min(spread / 0.20, 1.0))

    if spread is not None and spread > 0.20:
        regime = "unstable"
        reason = "spread_too_wide"
        regime_ok = 0
    elif vol_mean is not None and vol_mean > 0.02:
        regime = "unstable"
        reason = "vol_too_high"
        regime_ok = 0
    elif vol_mean is None:
        regime = "unstable"
        reason = "regime_uncertain"
        regime_ok = 0
    elif dominant >= 0.75 and flip_rate <= 0.25 and vol_mean <= 0.012 and (spread is None or spread <= 0.08):
        regime = "trend"
        reason = "trend_consistency"
        regime_ok = 1
    elif flip_rate >= 0.40 or dominant <= 0.55 or vol_mean <= 0.012:
        regime = "chop"
        reason = "chop_detected"
        regime_ok = 1
    else:
        regime = "unstable"
        reason = "regime_uncertain"
        regime_ok = 0

    return {
        "regime": regime,
        "regime_ok": regime_ok,
        "reason": reason,
        "trend_ratio": dominant,
        "flip_rate": flip_rate,
        "realized_vol": vol_mean,
        "spread_quality": spread_quality,
        **REGIME_THRESHOLDS[regime],
    }


def resolve_thresholds(snapshot, asset=None, engine=None):
    thresholds = {
        "regime": "fixed",
        "regime_ok": 1,
        "reason": "fixed_defaults",
        "edge_floor": SHADOW_EDGE_FLOOR,
        "confidence_floor": SHADOW_CONFIDENCE_FLOOR,
        "trend_ratio": None,
        "flip_rate": None,
        "realized_vol": None,
        "spread_quality": None,
    }
    if asset == "BTC" or engine == "btc15m":
        thresholds.update(classify_btc_regime(snapshot))
    return thresholds


def score_edge(snapshot, asset=None, engine=None):
    returns = {
        "ret_1s": snapshot.get("ret_1s"),
        "ret_3s": snapshot.get("ret_3s"),
        "ret_5s": snapshot.get("ret_5s"),
        "ret_10s": snapshot.get("ret_10s"),
        "ret_30s": snapshot.get("ret_30s"),
    }
    weights = {
        "ret_1s": 0.35,
        "ret_3s": 0.25,
        "ret_5s": 0.20,
        "ret_10s": 0.12,
        "ret_30s": 0.08,
    }
    weighted_sum = 0.0
    weight_total = 0.0
    for key, value in returns.items():
        if value is None:
            continue
        w = weights[key]
        weighted_sum += value * w
        weight_total += w

    seconds_remaining = snapshot.get("seconds_remaining")
    spread = snapshot.get("spread")
    vol_inputs = _available([snapshot.get("vol_10s"), snapshot.get("vol_30s"), snapshot.get("vol_60s")])
    vol_mean = sum(vol_inputs) / len(vol_inputs) if vol_inputs else None

    if weight_total == 0 and spread is None and seconds_remaining is None:
        thresholds = resolve_thresholds(snapshot, asset=asset, engine=engine)
        return {
            "model_p_yes": None,
            "model_p_no": None,
            "edge_yes": None,
            "edge_no": None,
            "net_edge": None,
            "confidence": None,
            "regime": thresholds.get("regime"),
            "regime_ok": None,
            "adaptive_net_edge_floor": thresholds.get("edge_floor"),
            "adaptive_confidence_floor": thresholds.get("confidence_floor"),
            "shadow_decision": "skip_data",
            "shadow_skip_reason": "insufficient_features",
        }

    momentum = (weighted_sum / weight_total) if weight_total else 0.0
    time_pressure = None
    if seconds_remaining is not None:
        if seconds_remaining <= 0:
            thresholds = resolve_thresholds(snapshot, asset=asset, engine=engine)
            return {
                "model_p_yes": None,
                "model_p_no": None,
                "edge_yes": None,
                "edge_no": None,
                "net_edge": None,
                "confidence": None,
                "regime": thresholds.get("regime"),
                "regime_ok": 0,
                "adaptive_net_edge_floor": thresholds.get("edge_floor"),
                "adaptive_confidence_floor": thresholds.get("confidence_floor"),
                "shadow_decision": "skip_time",
                "shadow_skip_reason": "seconds_remaining_non_positive",
            }
        time_pressure = _clamp(1.0 - (float(seconds_remaining) / 900.0), 0.0, 1.0)

    spread_penalty = spread if spread is not None else 0.0
    vol_penalty = vol_mean if vol_mean is not None else 0.0
    signal = momentum * 16.0
    if time_pressure is not None:
        signal += (0.02 * time_pressure) if momentum >= 0 else (-0.02 * time_pressure)
    signal -= spread_penalty * 0.50
    signal -= vol_penalty * 0.20

    model_p_yes = _clamp(0.5 + signal, 0.01, 0.99)
    model_p_no = _clamp(1.0 - model_p_yes, 0.01, 0.99)

    market_mid = snapshot.get("midprice")
    if market_mid is None:
        market_mid = 0.5
    market_mid = _clamp(market_mid, 0.01, 0.99)

    edge_yes = model_p_yes - market_mid
    edge_no = model_p_no - (1.0 - market_mid)
    net_edge = edge_yes - edge_no

    confidence = _clamp(abs(momentum) * 150.0 + abs(net_edge) * 4.0 - spread_penalty * 0.5 - vol_penalty * 0.2, 0.01, 0.99)

    thresholds = resolve_thresholds(snapshot, asset=asset, engine=engine)
    regime_ok = thresholds.get("regime_ok", 1)
    shadow_skip_reason = None if regime_ok else thresholds.get("reason")
    if spread is not None and spread > 0.20:
        regime_ok = 0
        shadow_skip_reason = "spread_too_wide"
    if vol_mean is not None and vol_mean > 0.02:
        regime_ok = 0
        shadow_skip_reason = "vol_too_high"

    shadow_decision, shadow_skip_reason = decide_shadow_outcome(
        net_edge=net_edge,
        confidence=confidence,
        regime_ok=regime_ok,
        shadow_skip_reason=shadow_skip_reason,
        spread=spread,
        edge_floor=thresholds["edge_floor"],
        confidence_floor=thresholds["confidence_floor"],
    )

    return {
        "model_p_yes": model_p_yes,
        "model_p_no": model_p_no,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "net_edge": net_edge,
        "confidence": confidence,
        "regime": thresholds.get("regime"),
        "regime_ok": regime_ok,
        "adaptive_net_edge_floor": thresholds["edge_floor"],
        "adaptive_confidence_floor": thresholds["confidence_floor"],
        "trend_ratio": thresholds.get("trend_ratio"),
        "flip_rate": thresholds.get("flip_rate"),
        "realized_vol": thresholds.get("realized_vol"),
        "spread_quality": thresholds.get("spread_quality"),
        "shadow_decision": shadow_decision,
        "shadow_skip_reason": shadow_skip_reason,
    }
