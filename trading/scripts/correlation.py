#!/usr/bin/env python3
"""
correlation.py — Market correlation guard for Polymarket.

Groups markets by keyword similarity. Markets sharing 2+ significant keywords
are considered correlated. Prevents over-exposure to a single thesis.
"""
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import MAX_CORRELATED_POSITIONS

logger = logging.getLogger(__name__)

STOP_WORDS = {
    "will", "the", "by", "before", "in", "of", "a", "an", "end",
    "to", "be", "at", "on", "or", "and", "for", "from", "with",
    "is", "are", "was", "were", "that", "this", "it", "its",
    "not", "no", "yes", "per", "as", "up", "so", "than",
    "vs", "does", "do", "has", "have", "more", "less",
}


def _extract_keywords(title: str) -> set:
    """Extract significant keywords from a market title."""
    # lowercase, strip punctuation
    text = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    words = text.split()
    # Remove stop words and short tokens
    keywords = {w for w in words if w not in STOP_WORDS and len(w) >= 3}
    return keywords


def _infer_thesis(keywords: set) -> str:
    """Infer a rough thesis label from keywords."""
    crypto_terms = {"bitcoin", "btc", "eth", "ethereum", "crypto", "sol", "solana",
                    "price", "ath", "reach", "100k", "above", "below", "usd"}
    politics_terms = {"trump", "biden", "election", "president", "congress",
                      "senate", "republican", "democrat", "vote", "win"}
    geo_terms = {"war", "russia", "ukraine", "china", "nato", "military",
                 "attack", "conflict", "ceasefire", "sanctions"}

    overlap_crypto   = keywords & crypto_terms
    overlap_politics = keywords & politics_terms
    overlap_geo      = keywords & geo_terms

    scores = {
        "crypto":   len(overlap_crypto),
        "politics": len(overlap_politics),
        "geopolitics": len(overlap_geo),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        # Use first keyword alphabetically
        return sorted(keywords)[0] if keywords else "general"
    return best


def check_correlation(
    new_market_title: str,
    existing_positions: dict,
    max_correlated: int = None,
) -> tuple:
    """
    Check if a new market is too correlated with existing positions.

    Args:
        new_market_title: Title/question of the new market to consider.
        existing_positions: Dict of {token_id: {market, ...}} currently held.
        max_correlated: Override MAX_CORRELATED_POSITIONS from config.

    Returns:
        (allowed: bool, thesis_group: str, correlated_count: int)
        - allowed=False means the trade is blocked due to correlation.
    """
    if max_correlated is None:
        max_correlated = MAX_CORRELATED_POSITIONS

    new_kw = _extract_keywords(new_market_title)
    thesis_group = _infer_thesis(new_kw)

    if not new_kw:
        # Can't determine keywords — allow with unknown group
        return (True, "unknown", 0)

    correlated_count = 0
    correlated_markets = []

    for token_id, pos in existing_positions.items():
        market_title = pos.get("market", "")
        if not market_title:
            continue
        existing_kw = _extract_keywords(market_title)
        shared = new_kw & existing_kw
        if len(shared) >= 2:
            correlated_count += 1
            correlated_markets.append(market_title[:60])
            logger.debug(
                "Correlated: '%s' <-> '%s' (shared: %s)",
                new_market_title[:50],
                market_title[:50],
                shared,
            )

    allowed = correlated_count < max_correlated

    if not allowed:
        logger.warning(
            "CORRELATION GUARD: blocked '%s' — %d/%d correlated positions in thesis '%s'",
            new_market_title[:60],
            correlated_count,
            max_correlated,
            thesis_group,
        )

    return (allowed, thesis_group, correlated_count)
