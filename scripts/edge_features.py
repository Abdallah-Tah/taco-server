#!/usr/bin/env python3
"""Pure helpers for edge telemetry feature extraction."""
from __future__ import annotations

import math
import time


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def safe_spread(best_bid, best_ask):
    bid = _to_float(best_bid)
    ask = _to_float(best_ask)
    if bid is None or ask is None:
        return None
    return ask - bid


def midprice(best_bid, best_ask, fallback_price=None):
    bid = _to_float(best_bid)
    ask = _to_float(best_ask)
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return _to_float(fallback_price)


def _price_at_or_before(price_points, target_ts):
    for ts, price in reversed(price_points):
        if ts <= target_ts:
            return _to_float(price)
    return None


def _calc_return(now_price, prev_price):
    if now_price is None or prev_price in (None, 0):
        return None
    return (now_price - prev_price) / prev_price


def _stddev(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _calc_vol(price_points, now_ts, window_sec):
    window = [float(p) for t, p in price_points if t >= now_ts - window_sec]
    if len(window) < 3:
        return None
    returns = []
    for i in range(1, len(window)):
        prev = window[i - 1]
        if prev:
            returns.append((window[i] - prev) / prev)
    return _stddev(returns)


def _size_from_level(level):
    if level is None:
        return None
    if isinstance(level, dict):
        for key in ("size", "qty", "quantity", "amount", "volume"):
            if key in level:
                return _to_float(level.get(key))
        return None
    if isinstance(level, (list, tuple)):
        if len(level) >= 2:
            return _to_float(level[1])
    return None


def _sum_depth_sizes(levels, depth):
    if not levels:
        return None
    total = 0.0
    used = 0
    for level in levels[:depth]:
        size = _size_from_level(level)
        if size is None:
            continue
        total += size
        used += 1
    return total if used else None


def _imbalance(bid_sz, ask_sz):
    if bid_sz is None or ask_sz is None:
        return None
    denom = bid_sz + ask_sz
    if denom <= 0:
        return None
    return (bid_sz - ask_sz) / denom


def _microprice(best_bid, best_ask, bid_size, ask_size):
    bid = _to_float(best_bid)
    ask = _to_float(best_ask)
    bid_sz = _to_float(bid_size)
    ask_sz = _to_float(ask_size)
    if bid is None or ask is None or bid_sz is None or ask_sz is None:
        return None
    denom = bid_sz + ask_sz
    if denom <= 0:
        return None
    return (ask * bid_sz + bid * ask_sz) / denom


def build_feature_snapshot(
    *,
    price_points=None,
    now_ts=None,
    best_bid=None,
    best_ask=None,
    fallback_price=None,
    bid_size=None,
    ask_size=None,
    depth_bids=None,
    depth_asks=None,
    seconds_remaining=None,
):
    if now_ts is None:
        now_ts = int(time.time())

    clean_points = []
    for ts, price in price_points or []:
        p = _to_float(price)
        if p is None:
            continue
        try:
            clean_points.append((int(ts), p))
        except Exception:
            continue

    price_now = clean_points[-1][1] if clean_points else _to_float(fallback_price)
    price_1s_ago = _price_at_or_before(clean_points, now_ts - 1)
    price_3s_ago = _price_at_or_before(clean_points, now_ts - 3)
    price_5s_ago = _price_at_or_before(clean_points, now_ts - 5)
    price_10s_ago = _price_at_or_before(clean_points, now_ts - 10)
    price_30s_ago = _price_at_or_before(clean_points, now_ts - 30)

    top_bid_size = _to_float(bid_size)
    top_ask_size = _to_float(ask_size)
    if top_bid_size is None and depth_bids:
        top_bid_size = _size_from_level(depth_bids[0])
    if top_ask_size is None and depth_asks:
        top_ask_size = _size_from_level(depth_asks[0])

    snapshot = {
        "seconds_remaining": seconds_remaining,
        "best_bid": _to_float(best_bid),
        "best_ask": _to_float(best_ask),
        "spread": safe_spread(best_bid, best_ask),
        "midprice": midprice(best_bid, best_ask, fallback_price=fallback_price),
        "microprice": _microprice(best_bid, best_ask, top_bid_size, top_ask_size),
        "price_now": price_now,
        "price_1s_ago": price_1s_ago,
        "price_3s_ago": price_3s_ago,
        "price_5s_ago": price_5s_ago,
        "price_10s_ago": price_10s_ago,
        "price_30s_ago": price_30s_ago,
        "ret_1s": _calc_return(price_now, price_1s_ago),
        "ret_3s": _calc_return(price_now, price_3s_ago),
        "ret_5s": _calc_return(price_now, price_5s_ago),
        "ret_10s": _calc_return(price_now, price_10s_ago),
        "ret_30s": _calc_return(price_now, price_30s_ago),
        "vol_10s": _calc_vol(clean_points, now_ts, 10),
        "vol_30s": _calc_vol(clean_points, now_ts, 30),
        "vol_60s": _calc_vol(clean_points, now_ts, 60),
        "imbalance_1": _imbalance(top_bid_size, top_ask_size),
        "imbalance_3": _imbalance(
            _sum_depth_sizes(depth_bids, 3),
            _sum_depth_sizes(depth_asks, 3),
        ),
    }
    return snapshot
