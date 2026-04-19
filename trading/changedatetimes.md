# Trading Config Change Log

Used by Claude to pick up context between sessions. Read me first.

---

## 2026-04-18 (22:15–22:35 ET) — BTC/ETH 15m: soft-band, gamma-fallback fix, skip audit tool

### Context
User reported "btc and eth are skipping a lot" and asked to verify opportunity cost of skipped signals + confirm Polymarket v2 API usage.

### V2 API verification
All bots already use v2 bases (`gamma-api.` / `clob.` / `data-api.polymarket.com` — Polymarket updated in-place, no `/v2/` URL prefix). No legacy `strapi-matic`, `subgraph`, or `/v1/` Polymarket URLs anywhere.

### Code changes
1. **Soft band [0.50, 0.55) at 0.5× size** — `polymarket_btc15m.py` + `polymarket_eth15m.py`, maker + snipe paths.
   - New constants: `SIGNAL_SOFT_MIN_ENTRY_PRICE` (BTC) / `SNIPE_SOFT_MIN_PRICE` (ETH) default 0.50; `SIGNAL_SOFT_BAND_SIZE_MULT` / `SNIPE_SOFT_BAND_SIZE_MULT` default 0.50.
   - Hard reject now at 0.50 instead of 0.55; entries in [0.50, 0.55) get `bucket="soft_band"` and half size.
   - Env overrides: `BTC15M_SIGNAL_SOFT_MIN_ENTRY_PRICE`, `BTC15M_SIGNAL_SOFT_BAND_SIZE_MULT`, matching `ETH15M_*`.
   - Startup banner now logs `soft_min=0.50 soft_mult=0.50`.
2. **Gamma-vs-CLOB pricing fix (Option A)** — `choose_direction_book_btc` / `choose_direction_book_eth`.
   - Was: on divergence (`mid_vs_gamma`, `ask_vs_gamma_quote`) the bot swapped to gamma's stale `outcomePrices`. Downstream mismatch-abort guard was defeated because by the time it ran `book == gamma_book` so `mismatch_diff ≈ 0`.
   - Now: only fall back to gamma on `clob_error` / `missing_best_ask`. On divergence, keep CLOB book (with `reason` still set so downstream abort fires on real CLOB vs gamma).
   - New log labels: `clob_trusted_despite_divergence` (divergence case) vs `gamma_fallback` (CLOB unavailable case). Replaces old `Using gamma market quote fallback` which hid both.
3. **Dead-code cleanup** — removed unused `get_clob_prices()` from 5 files (btc15m, eth15m, sol15m, btc5m, xrp15m). Each had a corrupted query param `&喝着=1` (literal Chinese characters, probably Cursor/copy-paste artifact). Never called anywhere, but the garbage URL was a time bomb if someone ever wired it up.

### New tool: `scripts/polymarket_skip_audit.py`
- Scans `/tmp/polymarket_{btc,eth}15m.log` for `FILTER: ... skipping low price` lines.
- Correlates each with the preceding `[*-BOOK]` line to pull direction + token_id.
- Dedupes by (token_id, 15m window) and queries CLOB v2 `/prices-history` (fidelity=1min) to get the final price in the window.
- Classifies WIN (≥0.95) / LOSS (≤0.05) / UNRESOLVED, computes hypothetical P/L at configurable size.
- Outputs CSV + stdout summary bucketed by 5¢ bands.

### First audit run (last 24h before fix)
- 258 skips → 45 unique windows → 40 resolved
- **31W / 9L = 77.5% WR, +$738 hypothetical P/L at $12/trade**
- By band:
  - **0.50–0.55 (soft band): 10W/0L, +$110** — directly validates soft-band change
  - 0.45–0.50: 7W/2L, +$66
  - <0.45: 14W/7L, +$533 (INFLATED by the gamma mispricing bug — entry prices in these skips were gamma-cached, not actually fillable)

### Nightly cron
- `17 3 * * * cd /home/abdaltm86/.openclaw/workspace/trading && .polymarket-venv/bin/python3 scripts/polymarket_skip_audit.py --hours 24 --out audits/skip_audit_$(date +%Y-%m-%d).csv >> audits/skip_audit.log 2>&1`
- First fire: 2026-04-19 03:17 ET.
- Output dir: `trading/audits/` (created).

### Restart
- BTC15M PID 3570750
- ETH15M PID 3570742
- Both banners confirm `soft_min=0.50 soft_mult=0.50`.

### Follow-ups deferred
- WebSocket migration for book updates (plan #3 from analysis) — biggest latency win available, not blocked but held for separate session.
- Once a week or two of `audits/skip_audit_*.csv` accumulates, revisit the 0.50–0.55 band WR empirically vs the 100% single-day snapshot.
- SOL15M min-entry filter still missing `SIGNAL_MIN_ENTRY_PRICE` (noted in earlier entry, still deferred).

---

## 2026-04-18 (16:25–16:30 ET) — CB-GRID post-only skip + rejection safety + Telegram fix

### Code changes
- `scripts/coinbase_momentum.py`:
  1. **Buy-side post-only skip**: pre-check in `waiting_buy` branch — if `lvl.buy_price >= snapshot.best_ask`, skip the cycle (no API call). Logs once per (pair, level) via in-memory `_buy_skip_logged` set, resets when buy succeeds.
  2. **Max-rejection safety pause**: `_safety_pause` triggered after `rejection_count >= 50` on a level (catches future unknown rejection loops).
  3. **Telegram helper rewrite**: replaced silent sync `tg()` with BTC15M-style threaded send + `[TG] Sending:` and `[TG-ERR]` audit logs. Added `threading` import.
  4. **Telegram token fix**: hardcoded fallback token was **stale/revoked** (`AA…JSK0UIwi0d4` → 401 Unauthorized). Updated to match the valid BTC15M token (`AA…pWuyzByXOo`). Explains why user never saw CB-GRID Telegram despite multiple fills (ETH 04-16, SOL 04-18 03:54 UTC).

### Why
- SOL-USD L2 had `buy_price=$87.00` from a grid placed when SOL was ~$89 anchor. SOL dropped to ~$86. Post-only BUY at $87 crossed the ask → Coinbase rejected with `INVALID_LIMIT_PRICE_POST_ONLY` every cycle. **1961 rejections in ~6 hours**, silent, no safety pause.
- `_maker_safe_sell_price` existed for sells (clamps to bid+tick) but had no buy-side equivalent. The skip preserves grid semantics — level waits for market to come down rather than clamping up.
- Telegram 401 discovered via the new audit logs — proof that the old silent `try/except` was hiding delivery failures. Same class of bug as the redeem notification fix earlier today.

### Restart
- Final PID **3505291** (after two restart cycles — first for fix #1/#2, second for Telegram token/helper)
- Clean log confirmed: `[CB-GRID] Skip buy SOL-USD lvl=2: price=$87.00 >= ask=$86.19 — waiting for market drop` (single log line, no retry spam)
- Test Telegram delivered successfully (no `[TG-ERR]` 401) — fills will now notify.

### Other bots this session — no changes
- **AERO** (coinbase_aero_grid.py): dry, waiting L1 $0.4041. Leave 1–2 weeks to gather data.
- **SPREAD** (polymarket_spread_capture.py): dry + scan-only, books at parity. Leave as-is.
- **MOMO** (coinbase_momo.py): correctly filtering `fast_below_slow`. Leave as-is; will engage when EMA9 crosses above EMA21.

---

## 2026-04-18 (16:05–16:15 ET) — notification audit trail

### Code change
- `scripts/polymarket_redeem.py`: rewrote `_send_telegram` to match BTC15M style (env-driven `CHAT_ID`/`TOPIC_ID`, threaded send, `[TG] Sending:` + `[TG-ERR]` logs). Added `log()` helper writing to `/tmp/polymarket_redeem.log`. Added `[NOTIFY]` audit lines in `notify_redeem` so every call leaves a trace.

### Why
- 4 real redeems happened between 02:07 and 12:35 ET on 2026-04-18 but `/tmp/polymarket_redeem_notified.json` was never written (notifications never fired). Silent `try/except` hid the failure reason.
- End-to-end test with both a win and a loss: ✅ logs, ✅ Telegram delivered, ✅ dedupe file populated.

### Audit tail command for next session
```
tail -50 /tmp/polymarket_redeem.log
```

---

## 2026-04-18 (11:20–11:55 ET)

### Config changes (secrets.env)
- `SOL15M_MAKER_ENABLED`: true → **false**
- `BTC15M_SIGNAL_MIN_ENTRY_PRICE`: 0.38 → **0.55**
- `ETH15M_SIGNAL_MIN_ENTRY_PRICE`: 0.38 → **0.55**

### Code changes
- `scripts/polymarket_redeem.py`: added Telegram cha-ching (wins) + loss alerts. Single notification choke point — fires for every redeem regardless of caller (engines, daemon, manual). Added `/tmp/polymarket_redeem_notified.json` dedupe (7-day window).
- `scripts/polymarket_auto_redeem_daemon.py`: removed duplicate `send_telegram_cha_ching` + `send_pushcut_notification` calls from main loop (redeem.py owns it now).

### Process restarts after changes
- auto-redeem daemon → PID 3457402
- BTC15M → PID 3458297 (banner confirms `min_entry=0.55`)
- ETH15M → PID 3458470 (banner confirms `min_entry=0.55`)
- SOL15M restarted earlier in session (maker=False dry=False)

### Why these changes (evidence)
- **SOL maker off**: 3 stop-loss events today logged with `order=None` — never executed on-chain. Polymarket positions API confirms zero open crypto 15m positions. "Losses" were phantom (maker buys never filled, bot tracked them as filled).
- **Min entry 0.38 → 0.55**: API-driven backtest over 180 resolved markets showed 0.40-0.55 band has 31.7% hit rate (17W/36L) — bleeding book. Cutting it shifts overall hit rate from 59.4% → ~71%.
- **Notifications**: `polymarket_redeem.py` is called by 8 scripts (btc15m, eth15m, sol15m, xrp15m, btc5m, reconcile, overnight_monitor, auto_redeem_daemon). Only daemon had Telegram code. User missed 10 winning redeems ~$100 over last 3 days (no cha-ching).

### Backtest summary (227 trades, 180 resolved)
| Dimension | Result |
|---|---|
| Overall hit rate | 59.4% (107/180) |
| ETH (best asset) | 65.0% (n=60) |
| BTC | 56.8% (n=88) |
| SOL | 55.0% (n=20) |
| XRP | 58.3% (n=12) |
| Entry ≥0.85 | 88.2% (n=17) |
| Entry 0.70–0.85 | 75.0% (n=48) |
| Entry 0.55–0.70 | 62.9% (n=62) |
| Entry 0.40–0.55 | **31.7% (n=41) — losing band** |
| Entry <0.40 | **33.3% (n=12) — losing band** |
| Up side | 61.8% (n=102) |
| Down side | 56.4% (n=78) |

### Deferred / not done
- **SOL min-entry filter**: SOL bot has `SIGNAL_MAX_ENTRY_PRICE` but no `_MIN_`. Needs code edit to `polymarket_sol15m.py`. SOL maker is off so not actively trading that band — address if/when re-enabling.
- **Hour-14-UTC (10 AM ET) skip**: Backtest shows 15.4% hit rate at that hour (n=13). Worth a skip filter, but smaller sample and needs code changes in all three bots.
- **Review spread bot** (`polymarket_spread_capture.py`) — requested, pending.
- **Review AERO dry run** (`coinbase_aero_grid.py`) — requested, pending.

### Current live trading state (as of this entry)
- BTC15M: LIVE, maker+taker, min 0.55, max 0.72, up_max 0.62, down_max 0.72
- ETH15M: LIVE, maker+taker, min 0.55, max 0.78, up_max 0.65, down_max 0.72
- SOL15M: LIVE taker only, max 0.70 (maker OFF)
- Auto-redeem daemon: running
- Coinbase scalper: dry run
- Polymarket 15m generally: active

### Spread bot review (polymarket_spread_capture.py)
- Mode: DRY-RUN + SCAN-ONLY (never flipped to live). $5 size, 1¢ min edge.
- Running since 2026-04-17 15:15, 20+ hours, zero entries/fills.
- Book state: combined asks 1.00–1.03, combined bids 0.97–0.99 → edge always negative or zero. Polymarket MMs keep books at parity.
- **Verdict**: healthy, correctly filtering. Rare-opportunity bot, fires only on dislocations. No config change warranted. Leave running for data.

### AERO grid bot review (coinbase_aero_grid.py)
- Mode: DRY-RUN (no live execution wiring exists). Virtual $100, $0 deployed.
- Running since 2026-04-17 21:25, ~14 hours, 0 positions.
- Grid levels: L1 $0.4041 (-4.7%), L2 $0.3850 (-9.1%), L3 $0.3660 (-13.6%). Current AERO ~$0.4224.
- No fills because no dip deep enough. Not a bot issue.
- Config notes:
  - `MAX_DEPLOYED_USD=8.0` (env overrides code default of 6.0)
  - `DAILY_LOSS_LIMIT_USD=1.0` on $100 virtual = 1% daily stop, conservative
  - RSI range 15–75 very permissive (effectively not filtering)
  - 4.5% spacing + 8% stop means L1 could stop-out in one adverse candle
- **Verdict**: config defensible for dry-run learning. Let it gather data before tuning.

---
