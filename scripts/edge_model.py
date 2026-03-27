#!/usr/bin/env python3
"""Deterministic shadow scoring for edge telemetry."""
from __future__ import annotations


def _clamp(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(hi, value))


def _available(values):
    return [v for v in values if v is not None]


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
        return {
            "model_p_yes": None,
            "model_p_no": None,
            "edge_yes": None,
            "edge_no": None,
            "net_edge": None,
            "confidence": None,
            "regime_ok": None,
            "shadow_decision": "skip_data",
            "shadow_skip_reason": "insufficient_features",
        }

    momentum = (weighted_sum / weight_total) if weight_total else 0.0
    time_pressure = None
    if seconds_remaining is not None:
        if seconds_remaining <= 0:
            return {
                "model_p_yes": None,
                "model_p_no": None,
                "edge_yes": None,
                "edge_no": None,
                "net_edge": None,
                "confidence": None,
                "regime_ok": 0,
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

    regime_ok = 1
    shadow_skip_reason = None
    if spread is not None and spread > 0.20:
        regime_ok = 0
        shadow_skip_reason = "spread_too_wide"
    if vol_mean is not None and vol_mean > 0.02:
        regime_ok = 0
        shadow_skip_reason = "vol_too_high"

    if regime_ok == 0:
        if shadow_skip_reason == "spread_too_wide":
            shadow_decision = "skip_spread"
        else:
            shadow_decision = "skip_regime"
    elif abs(net_edge) < 0.01:
        shadow_decision = "skip_no_edge"
        shadow_skip_reason = "net_edge_below_floor"
    elif net_edge > 0:
        shadow_decision = "place_yes"
    else:
        shadow_decision = "place_no"

    return {
        "model_p_yes": model_p_yes,
        "model_p_no": model_p_no,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "net_edge": net_edge,
        "confidence": confidence,
        "regime_ok": regime_ok,
        "shadow_decision": shadow_decision,
        "shadow_skip_reason": shadow_skip_reason,
    }
