#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Any

import requests


def fetch_book(token_id: str, timeout: int = 10) -> dict[str, Any]:
    try:
        r = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        r.raise_for_status()
        ob = r.json() or {}
        bids = [
            {"price": float(x.get("price", 0) or 0), "size": float(x.get("size", 0) or 0)}
            for x in (ob.get("bids") or [])
            if float(x.get("price", 0) or 0) > 0
        ]
        asks = [
            {"price": float(x.get("price", 0) or 0), "size": float(x.get("size", 0) or 0)}
            for x in (ob.get("asks") or [])
            if float(x.get("price", 0) or 0) > 0
        ]
        best_bid = max((x["price"] for x in bids), default=None)
        best_ask = min((x["price"] for x in asks), default=None)
        tick = float(ob.get("tick_size") or 0.01)
        midpoint = round((best_bid + best_ask) / 2, 4) if best_bid is not None and best_ask is not None else None
        spread = round(best_ask - best_bid, 4) if best_bid is not None and best_ask is not None else None
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "tick": tick,
            "spread": spread,
            "bids": bids[:10],
            "asks": asks[:10],
            "raw": ob,
            "error": None,
        }
    except Exception as e:
        return {
            "best_bid": None,
            "best_ask": None,
            "midpoint": None,
            "tick": 0.01,
            "spread": None,
            "bids": [],
            "asks": [],
            "raw": None,
            "error": str(e),
        }


def choose_buy_price(book: dict[str, Any], price_cap: float, mode: str, spread_cap: float, maker_offset: float = 0.0) -> tuple[float | None, str | None]:
    best_bid = book.get("best_bid")
    best_ask = book.get("best_ask")
    tick = max(float(book.get("tick") or 0.01), 0.01)
    spread = book.get("spread")

    if best_ask is None:
        return None, "missing_best_ask"
    if spread is not None and spread > spread_cap:
        return None, f"spread_too_wide:{spread:.4f}"
    if best_ask > price_cap:
        return None, f"slippage_cap_exceeded:{best_ask:.4f}>{price_cap:.4f}"

    if mode == "maker":
        candidate = round(max(0.01, best_ask - max(tick, maker_offset)), 2)
        if candidate > price_cap:
            return None, f"maker_cap_exceeded:{candidate:.4f}>{price_cap:.4f}"
        return candidate, None

    # taker / FOK style
    return round(best_ask, 4), None


def choose_sell_price(book: dict[str, Any], price_floor: float, spread_cap: float) -> tuple[float | None, str | None]:
    best_bid = book.get("best_bid")
    spread = book.get("spread")
    if best_bid is None:
        return None, "missing_best_bid"
    if spread is not None and spread > spread_cap:
        return None, f"spread_too_wide:{spread:.4f}"
    if best_bid < price_floor:
        return None, f"price_floor_breached:{best_bid:.4f}<{price_floor:.4f}"
    return round(best_bid, 4), None


def book_log_fields(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "clob_best_bid": book.get("best_bid"),
        "clob_best_ask": book.get("best_ask"),
        "clob_midpoint": book.get("midpoint"),
        "spread": book.get("spread"),
    }
