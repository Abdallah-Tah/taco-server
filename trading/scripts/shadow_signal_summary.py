#!/usr/bin/env python3
"""Summarize BTC/ETH shadow signal counts from live logs.

Read-only monitor. No trading logic.
"""
from __future__ import annotations

from pathlib import Path

LOGS = {
    'BTC': Path('/tmp/polymarket_btc15m.log'),
    'ETH': Path('/tmp/polymarket_eth15m.log'),
}


def count_shadow(path: Path, tag: str) -> tuple[int, int, int]:
    if not path.exists():
        return 0, 0, 0
    total = kept = filtered = 0
    for line in path.read_text(errors='ignore').splitlines():
        marker = f'[{tag}-SHADOW] '
        if marker not in line:
            continue
        total += 1
        if f'[{tag}-SHADOW] kept ' in line:
            kept += 1
        elif f'[{tag}-SHADOW] filtered ' in line:
            filtered += 1
    return total, kept, filtered


def main() -> int:
    lines = []
    for tag, path in LOGS.items():
        total, kept, filtered = count_shadow(path, tag)
        lines.append(f"{tag}: {total} signals ({kept} kept / {filtered} filtered)")
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
