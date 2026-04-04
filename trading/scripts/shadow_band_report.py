#!/usr/bin/env python3
"""Shadow-mode price-band report for BTC/ETH.

Reads journal.db trades tagged with shadow=kept|filtered in notes.
Reports per engine:
- kept vs filtered PnL
- impact on total PnL
- trade count reduction
- bucket breakdown
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path.home() / '.openclaw' / 'workspace' / 'trading' / 'journal.db'


def parse_notes(notes: str) -> dict[str, str]:
    out = {}
    for part in (notes or '').split():
        if '=' in part:
            k, v = part.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT engine, entry_price, pnl_absolute, notes, timestamp_close
        FROM trades
        WHERE engine IN ('btc15m','eth15m')
          AND timestamp_close IS NOT NULL
          AND notes LIKE '%shadow=%'
        ORDER BY timestamp_close DESC
    """)
    rows = cur.fetchall()
    conn.close()

    by_engine = defaultdict(list)
    for r in rows:
        meta = parse_notes(r['notes'] or '')
        by_engine[r['engine']].append({
            'entry_price': float(r['entry_price'] or 0),
            'pnl': float(r['pnl_absolute'] or 0),
            'shadow': meta.get('shadow', 'unknown'),
            'bucket': meta.get('shadow_bucket', 'unknown'),
            'reason': meta.get('shadow_reason', ''),
        })

    for engine in ('btc15m', 'eth15m'):
        trades = by_engine.get(engine, [])
        print(f"SHADOW REPORT — {engine}")
        print(f"Tagged resolved trades: {len(trades)}")
        if not trades:
            print("  none yet\n")
            continue
        kept = [t for t in trades if t['shadow'] == 'kept']
        filt = [t for t in trades if t['shadow'] == 'filtered']
        kept_pnl = sum(t['pnl'] for t in kept)
        filt_pnl = sum(t['pnl'] for t in filt)
        total_pnl = sum(t['pnl'] for t in trades)
        print(f"  kept trades:     {len(kept):<4} pnl={kept_pnl:+.2f}")
        print(f"  filtered trades: {len(filt):<4} pnl={filt_pnl:+.2f}")
        print(f"  total pnl:       {total_pnl:+.2f}")
        print(f"  impact if filtered removed: {(-filt_pnl):+.2f}")
        reduction = (len(filt) / len(trades) * 100.0) if trades else 0.0
        print(f"  trade count reduction: {reduction:.1f}%")
        print("  by bucket:")
        buckets = defaultdict(lambda: {'trades':0,'pnl':0.0,'kept':0,'filtered':0})
        for t in trades:
            b = buckets[t['bucket']]
            b['trades'] += 1
            b['pnl'] += t['pnl']
            b[t['shadow']] += 1
        for bucket in sorted(buckets):
            b = buckets[bucket]
            print(f"    {bucket:<8} trades={b['trades']:<3} kept={b['kept']:<3} filtered={b['filtered']:<3} pnl={b['pnl']:+.2f}")
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
