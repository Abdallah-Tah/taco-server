#!/usr/bin/env python3
"""
Taco Autonomous Trader — 24/7 Solana sniper (v2)
Refactored with:
- config.py for all constants
- scoring.py for composite momentum scoring
- Layered exit system (stop_loss > hard_rotation > take_profit > partial_tp > trailing_stop > stale_rotation)
- High-conviction sizing (score >= 60 AND vol_accel > 2x → 0.12 SOL)
- Signal queue when MAX_OPEN_POSITIONS reached
- Structured exit logging (exit_type, entry_price, exit_price, pnl_percent, hold_duration_seconds, token_address)
- Blacklist with token_category, 7-day cooldown, and logging
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(line_buffering=True)

# ── Load config and scoring ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from config import (
    MIN_LIQUIDITY, MIN_VOLUME_24H, CHANGE_1H_MIN, CHANGE_1H_MAX,
    CHANGE_24H_FLOOR, MOMENTUM_SCORE_MIN, STOP_LOSS, HARD_ROTATION,
    TAKE_PROFIT_FULL, PARTIAL_TP_TRIGGER, TRAILING_STOP_ARM, TRAILING_STOP_GIVE,
    STALE_ROTATION_TIME, STALE_ROTATION_MOVE, DEFAULT_RISK_SOL,
    HIGH_CONVICTION_SOL, MAX_OPEN_POSITIONS, DEFENSIVE_POSITIONS,
    HIGH_CONVICTION_SCORE, HIGH_CONVICTION_VOL_ACCEL,
    BLACKLIST_FAIL_COUNT, BLACKLIST_AVG_LOSS_MAX, BLACKLIST_COOLDOWN_DAYS,
    REBUY_COOLDOWN_HOURS, REENTRY_COOLDOWN_SECONDS, CHECK_INTERVAL, PARTIAL_TP_FRACTION,
    DEFENSIVE_SCORE_BUMP, REGIME_SAMPLE_SIZE, REGIME_DEFENSIVE_AVG,
    REGIME_DEFENSIVE_WINS, REGIME_AGGRESSIVE_AVG, REGIME_AGGRESSIVE_WINS,
    REGIME_WINDOW, AGGRESSIVE_THRESHOLD, DEFENSIVE_THRESHOLD,
    DEFENSIVE_STOP_LOSS, DEFENSIVE_HARD_ROTATION,
)
from scoring import compute_score

# ── Credentials / paths ────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4"
CHAT_ID        = "7520899464"
WALLET         = "J6nK35ud8u6hzqDxuEtVWPsAMzv2v7H6stsxXW2rsnuH"

VENV_PY        = Path("/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3")
SNIPER         = Path("/home/abdaltm86/.openclaw/workspace/trading/scripts/sniper.py")
POSITIONS_FILE = Path("/home/abdaltm86/.openclaw/workspace/trading/.positions.json")
TRADE_LOG      = Path("/home/abdaltm86/.openclaw/workspace/trading/.trade_log.json")
BLACKLIST_FILE = Path("/home/abdaltm86/.openclaw/workspace/trading/.blacklist.json")
WORK_DIR       = Path("/home/abdaltm86/.openclaw/workspace/trading")

# ── Signal queue (candidates waiting for a free slot) ─────────────────────────
_signal_queue: list = []
_reentry_cooldowns: dict = {}


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def tg(msg):
    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=5,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ── Persistence ────────────────────────────────────────────────────────────────

def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            data = json.loads(POSITIONS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_positions(p: dict):
    POSITIONS_FILE.write_text(json.dumps(p, indent=2))


def load_trade_log() -> list:
    if TRADE_LOG.exists():
        try:
            data = json.loads(TRADE_LOG.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def log_trade(entry: dict):
    trades = load_trade_log()
    trades.append({**entry, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    TRADE_LOG.write_text(json.dumps(trades[-500:], indent=2))


def log_exit(
    exit_type: str,
    sym: str,
    mint: str,
    entry_price: float,
    exit_price: float,
    pnl_percent: float,
    hold_duration_seconds: float,
    token_address: str,
    **extra,
):
    """Structured exit log entry."""
    log_trade({
        "event": exit_type,
        "sym": sym,
        "mint": mint,
        "token_address": token_address,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_percent": round(pnl_percent, 4),
        "hold_duration_seconds": round(hold_duration_seconds, 1),
        **extra,
    })


# ── Blacklist ──────────────────────────────────────────────────────────────────

def load_blacklist() -> dict:
    """Returns {mint: {count, avg_loss, last_added_ts, category, ...}}"""
    if BLACKLIST_FILE.exists():
        try:
            data = json.loads(BLACKLIST_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_blacklist(bl: dict):
    BLACKLIST_FILE.write_text(json.dumps(bl, indent=2))


def record_bad_exit(mint: str, sym: str, pnl_pct: float, category: str = "unknown"):
    """Record a failed trade; auto-blacklist if thresholds exceeded."""
    trades = [
        t for t in load_trade_log()
        if t.get("mint") == mint and t.get("event") in {"SL", "ROTATE", "TRAIL", "DEF_PURGE"}
    ]
    losses = [float(t.get("pnl_percent", t.get("pct", 0)) or 0) for t in trades]
    losses.append(pnl_pct)

    if len(losses) >= BLACKLIST_FAIL_COUNT:
        avg_loss = sum(losses) / len(losses)
        if avg_loss <= BLACKLIST_AVG_LOSS_MAX:
            bl = load_blacklist()
            already = bl.get(mint, {})
            bl[mint] = {
                "sym": sym,
                "fail_count": len(losses),
                "avg_loss": round(avg_loss, 4),
                "added_ts": time.time(),
                "category": already.get("category", category),
            }
            save_blacklist(bl)
            log(f"  🚫 BLACKLISTED {sym} ({mint[:8]}…) avg_loss={avg_loss:.1f}%")
            log_trade({
                "event": "BLACKLIST_ADDED",
                "sym": sym,
                "mint": mint,
                "fail_count": len(losses),
                "avg_loss": round(avg_loss, 4),
                "category": bl[mint]["category"],
            })
            return True
    return False


def is_blacklisted(mint: str) -> bool:
    bl = load_blacklist()
    entry = bl.get(mint)
    if not entry:
        return False
    cooldown_secs = BLACKLIST_COOLDOWN_DAYS * 86400
    age = time.time() - float(entry.get("added_ts", 0))
    return age < cooldown_secs


# ── Solana / DexScreener helpers ───────────────────────────────────────────────

def get_sol_balance() -> float:
    r = requests.post(
        "https://api.mainnet-beta.solana.com",
        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]},
        timeout=8,
    )
    return r.json()["result"]["value"] / 1e9


def get_price(mint: str) -> float:
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8)
    pairs = r.json().get("pairs", [])
    return float(pairs[0].get("priceUsd", 0) or 0) if pairs else 0.0


def scan_opportunities(existing_mints: set) -> list:
    """Fetch top boosted tokens and score them with compute_score()."""
    r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=12)
    boosts = [t for t in r.json() if t.get("chainId") == "solana"][:25]
    candidates = []
    for t in boosts:
        addr = t.get("tokenAddress", "")
        if addr in existing_mints:
            continue
        try:
            r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=7)
            pairs = r2.json().get("pairs", [])
            if not pairs:
                continue
            p = pairs[0]
            liq   = float(p.get("liquidity", {}).get("usd",  0) or 0)
            vol24 = float(p.get("volume",    {}).get("h24",  0) or 0)
            h1    = float(p.get("priceChange", {}).get("h1", 0) or 0)
            h24   = float(p.get("priceChange", {}).get("h24",0) or 0)
            sym   = p.get("baseToken", {}).get("symbol", "?")
            price = float(p.get("priceUsd", 0) or 0)

            if not (
                liq  >= MIN_LIQUIDITY
                and vol24 >= MIN_VOLUME_24H
                and CHANGE_1H_MIN <= h1 <= CHANGE_1H_MAX
                and h24 >= CHANGE_24H_FLOOR
            ):
                continue

            score_data = compute_score(p)
            score      = score_data["score"]
            vol_accel  = score_data["vol_acceleration"]

            candidates.append({
                "sym":         sym,
                "addr":        addr,
                "price":       price,
                "liq":         liq,
                "vol":         vol24,
                "h1":          h1,
                "h24":         h24,
                "score":       score,
                "vol_accel":   vol_accel,
                "score_data":  score_data,
            })
        except Exception:
            pass
    return sorted(candidates, key=lambda x: x["score"], reverse=True)


# ── Trade execution ────────────────────────────────────────────────────────────

def execute_buy(mint: str, sol_amount: float) -> str:
    result = subprocess.run(
        [str(VENV_PY), str(SNIPER), "buy", mint, str(sol_amount)],
        capture_output=True, text=True, timeout=60, cwd=str(WORK_DIR),
    )
    return result.stdout + result.stderr


def execute_sell(mint: str, fraction: float = 1.0) -> str:
    result = subprocess.run(
        [str(VENV_PY), str(SNIPER), "sell", mint, str(fraction)],
        capture_output=True, text=True, timeout=60, cwd=str(WORK_DIR),
    )
    return result.stdout + result.stderr


# ── Position helpers ───────────────────────────────────────────────────────────

def position_age_seconds(pos: dict) -> float:
    return max(0.0, time.time() - float(pos.get("bought_at", time.time())))


def cleanup_reentry_cooldowns() -> None:
    now = time.time()
    expired = [mint for mint, then in _reentry_cooldowns.items() if (now - then) >= REENTRY_COOLDOWN_SECONDS]
    for mint in expired:
        _reentry_cooldowns.pop(mint, None)


def record_reentry_cooldown(mint: str) -> None:
    _reentry_cooldowns[mint] = time.time()


def in_rebuy_cooldown(mint: str) -> bool:
    cleanup_reentry_cooldowns()
    then = _reentry_cooldowns.get(mint)
    if then is None:
        return False
    remaining = REENTRY_COOLDOWN_SECONDS - (time.time() - then)
    if remaining > 0:
        until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(then + REENTRY_COOLDOWN_SECONDS))
        log(f"COOLDOWN: skipping {mint}, re-entry blocked until {until}")
        return True
    _reentry_cooldowns.pop(mint, None)
    return False


def recent_trade_stats(limit: int = 12) -> dict:
    trades = [
        t for t in load_trade_log()
        if t.get("event") in {"SL", "ROTATE", "TRAIL", "TP", "PARTIAL_TP"}
    ]
    recent = trades[-limit:]
    if not recent:
        return {"count": 0, "avg_pct": 0.0, "wins": 0, "losses": 0}
    pcts = [float(t.get("pnl_percent", t.get("pct", 0)) or 0) for t in recent]
    return {
        "count":   len(recent),
        "avg_pct": sum(pcts) / len(pcts),
        "wins":    sum(1 for p in pcts if p > 0),
        "losses":  sum(1 for p in pcts if p <= 0),
    }


def regime_settings() -> dict:
    # Use journal.db closed trades as single source of truth
    try:
        import importlib.util as _ilu
        _jpath = Path(__file__).with_name('journal.py')
        _jspec = _ilu.spec_from_file_location('scripts_journal', _jpath)
        _jmod = _ilu.module_from_spec(_jspec)
        _jspec.loader.exec_module(_jmod)
        _jmod.migrate_json_logs()

        _apath = Path(__file__).with_name('analytics.py')
        _aspec = _ilu.spec_from_file_location('scripts_analytics', _apath)
        _amod = _ilu.module_from_spec(_aspec)
        _aspec.loader.exec_module(_amod)

        closed = _amod._get_closed_trades(engine="solana")
        recent = closed[:REGIME_WINDOW]  # most recent N closed trades
        count = len(recent)
        if count > 0:
            wins = sum(1 for t in recent if (t.get("pnl_percent") or 0) > 0)
            win_rate = wins / count
        else:
            win_rate = 0.5  # neutral if no data
    except Exception as e:
        log(f"Regime journal sync failed, using legacy log: {e}")
        stats = recent_trade_stats(REGIME_WINDOW)
        count = stats["count"]
        win_rate = stats["wins"] / max(stats["count"], 1)

    mode      = "normal"
    min_score = MOMENTUM_SCORE_MIN
    max_buy   = HIGH_CONVICTION_SOL
    max_pos   = MAX_OPEN_POSITIONS
    sl        = STOP_LOSS
    stale_t   = STALE_ROTATION_TIME

    if count >= REGIME_WINDOW and win_rate < DEFENSIVE_THRESHOLD:
        mode      = "defensive"
        min_score += DEFENSIVE_SCORE_BUMP
        max_buy    = DEFAULT_RISK_SOL  # no high conviction in defensive
        max_pos    = DEFENSIVE_POSITIONS
        sl         = DEFENSIVE_STOP_LOSS
        stale_t    = 3600   # rotate faster (1h)
        log(f"REGIME: defensive (win_rate={win_rate:.1%}, window={count})")

    elif count >= REGIME_WINDOW and win_rate > AGGRESSIVE_THRESHOLD:
        mode      = "aggressive"
        min_score  = max(MOMENTUM_SCORE_MIN - 5, 25)
        log(f"REGIME: aggressive (win_rate={win_rate:.1%}, window={count})")

    hard_rotation = HARD_ROTATION
    if mode == "defensive":
        hard_rotation = DEFENSIVE_HARD_ROTATION

    return {
        "mode": mode, "min_score": min_score,
        "max_buy": max_buy, "max_positions": max_pos,
        "stop_loss": sl, "hard_rotation": hard_rotation, "stale_time": stale_t,
        "win_rate": win_rate, "sample_count": count,
    }


def buy_size_for_candidate(c: dict, sol_bal: float, regime: dict) -> float:
    """Return SOL size. High-conviction → HIGH_CONVICTION_SOL, else DEFAULT_RISK_SOL."""
    high_conv = (
        c.get("score", 0) >= HIGH_CONVICTION_SCORE
        and c.get("vol_accel", 0) >= HIGH_CONVICTION_VOL_ACCEL
    )
    size    = HIGH_CONVICTION_SOL if high_conv else DEFAULT_RISK_SOL
    max_buy = regime.get("max_buy", HIGH_CONVICTION_SOL)
    size    = min(size, max_buy)
    return min(size, max(0.0, round(sol_bal - 0.01, 3)))


def pnl_snapshot(positions: dict, sol_bal: float) -> dict:
    trades   = load_trade_log()
    realized = [
        t for t in trades
        if t.get("event") in {"SL", "ROTATE", "TRAIL", "TP", "PARTIAL_TP"}
    ]
    realized_avg = (
        sum(float(t.get("pnl_percent", t.get("pct", 0)) or 0) for t in realized) / len(realized)
        if realized else 0.0
    )
    wins   = sum(1 for t in realized if float(t.get("pnl_percent", t.get("pct", 0)) or 0) > 0)
    losses = sum(1 for t in realized if float(t.get("pnl_percent", t.get("pct", 0)) or 0) <= 0)

    open_cost = open_est = 0.0
    for mint, pos in positions.items():
        spent  = float(pos.get("sol_spent", 0) or 0)
        entry  = float(pos.get("entry", pos.get("entry_price", 0)) or 0)
        price  = get_price(mint)
        est    = spent * (price / entry) if entry > 0 and price > 0 else spent
        open_cost += spent
        open_est  += est

    return {
        "realized_events": len(realized),
        "realized_avg_pct": realized_avg,
        "wins":  wins,
        "losses": losses,
        "open_cost": open_cost,
        "open_est":  open_est,
        "open_pnl":  open_est - open_cost,
        "wallet_est": sol_bal + open_est,
    }


# ── Layered exit logic ─────────────────────────────────────────────────────────

def evaluate_exit(pos: dict, price: float, mint: str, regime: dict = None) -> tuple[str, str] | tuple[None, None]:
    """
    Returns (exit_type, human_reason) or (None, None) if no exit triggered.
    Priority: stop_loss > hard_rotation > take_profit > partial_tp >
              trailing_stop > stale_rotation
    """
    entry   = float(pos.get("entry", pos.get("entry_price", price)) or price)
    if entry <= 0:
        return None, None

    pct     = ((price - entry) / entry) * 100
    peak    = float(pos.get("peak_price", entry))
    age_s   = position_age_seconds(pos)

    # 1. Stop loss (hard floor) — tighter in defensive mode
    _sl = (regime or {}).get("stop_loss", STOP_LOSS)
    if pct <= _sl:
        return "SL", f"stop_loss pct={pct:.1f}%"

    # 2. Hard rotation (intermediate loss exit)
    _hard_rotation = (regime or {}).get("hard_rotation", HARD_ROTATION)
    if pct <= _hard_rotation:
        return "ROTATE", f"hard_rotation pct={pct:.1f}%"

    # 3. Full take profit
    if pct >= TAKE_PROFIT_FULL and not pos.get("tp_hit"):
        return "TP", f"take_profit pct={pct:.1f}%"

    # 4. Partial TP (handled separately — caller checks partial_tp_hit)
    if pct >= PARTIAL_TP_TRIGGER and not pos.get("partial_tp_hit"):
        return "PARTIAL_TP", f"partial_tp pct={pct:.1f}%"

    # 5. Trailing stop
    #    Arms at TRAILING_STOP_ARM gain; gives back at most TRAILING_STOP_GIVE from peak.
    #    After partial TP, remaining 75% also uses trailing stop once armed.
    trail_armed = pos.get("partial_tp_hit") or pct >= TRAILING_STOP_ARM
    if trail_armed and peak > 0:
        dd_from_peak = ((price / peak) - 1.0) * 100
        if dd_from_peak <= -TRAILING_STOP_GIVE:
            return "TRAIL", f"trailing_stop pct={pct:.1f}% peak_dd={dd_from_peak:.1f}%"

    # 6. Stale rotation
    _stale_t = (regime or {}).get("stale_time", STALE_ROTATION_TIME)
    if age_s >= _stale_t and abs(pct) < STALE_ROTATION_MOVE:
        return "ROTATE", f"stale_rotation age={age_s/3600:.1f}h pct={pct:.1f}%"

    return None, None


# ── Main loop ──────────────────────────────────────────────────────────────────

log("Taco Autonomous Trader STARTED — 24/7 full auto v2")
log(f"CONFIG ACTIVE: min_score={MOMENTUM_SCORE_MIN:.0f} min_liq={MIN_LIQUIDITY:.0f} min_vol={MIN_VOLUME_24H:.0f} h1_max={CHANGE_1H_MAX:.0f} sl={STOP_LOSS:.0f} rotate={HARD_ROTATION:.0f} reentry={REENTRY_COOLDOWN_SECONDS}s")
tg("🌮 Taco Autonomous Trader STARTED\n24/7 full auto | v2 | config + scoring engine active")

cycle = 0
while True:
    cycle += 1
    log(f"── Cycle {cycle} ──")
    positions = load_positions()

    # ── Drawdown / pause check ─────────────────────────────────────────────
    try:
        from portfolio import check_drawdown, _load_portfolio, _save_portfolio, snapshot, is_paused
        _portfolio = _load_portfolio()
        _snap = snapshot()
        _total_usd = _snap.get("total_usd", None)
        if _total_usd is None:
            log("⚠️ snapshot() returned no total_usd, using fallback $100")
            _total_usd = 100.0
        _portfolio = check_drawdown(_portfolio, _total_usd)
        _save_portfolio(_portfolio)
        _paused = is_paused(_portfolio)
        _pause_reason = f"capital=${_total_usd:.2f}" if _paused else ""
        if _paused:
            log(f"⛔ DRAWDOWN PAUSE: {_pause_reason} — skipping cycle {cycle}")
            time.sleep(10 if positions else CHECK_INTERVAL)
            continue
    except Exception as _e:
        log(f"Portfolio check error: {_e}")

    try:
        sol_bal = get_sol_balance()
        regime  = regime_settings()
        log(
            f"SOL balance: {sol_bal:.4f} | regime={regime['mode']} "
            f"win_rate={regime['win_rate']:.1%} (last {regime['sample_count']} trades)"
        )
    except Exception as e:
        log(f"Balance error: {e}")
        sol_bal = 0.0
        regime  = {
            "mode": "normal", "min_score": MOMENTUM_SCORE_MIN,
            "max_buy": HIGH_CONVICTION_SOL, "max_positions": MAX_OPEN_POSITIONS,
            "stats": {"avg_pct": 0, "wins": 0, "losses": 0},
        }

    # ── Monitor existing positions ─────────────────────────────────────────────
    to_remove: list = []
    changed   = False

    for mint, pos in list(positions.items()):
        try:
            price = get_price(mint)
            if price <= 0:
                log(f"No price for {pos.get('sym', mint[:6])} — skipping")
                continue

            entry  = float(pos.get("entry", pos.get("entry_price", price)) or price)
            pct    = ((price - entry) / entry) * 100 if entry > 0 else 0.0
            sym    = pos.get("sym", mint[:6])
            peak   = max(float(pos.get("peak_price", entry)), price)
            pos["peak_price"] = peak

            dd_from_peak = ((price / peak) - 1.0) * 100 if peak else 0.0
            log(f"  {sym}: ${price:.8f} ({pct:+.1f}%) peak_dd={dd_from_peak:.1f}%")

            exit_type, reason = evaluate_exit(pos, price, mint, regime=regime)
            hold_s = position_age_seconds(pos)

            if exit_type == "PARTIAL_TP":
                out = execute_sell(mint, PARTIAL_TP_FRACTION)
                log(f"  Sell result: {out[:150]}")
                if "Signature" in out:
                    pos["partial_tp_hit"] = True
                    changed = True
                    log_exit(
                        exit_type="PARTIAL_TP", sym=sym, mint=mint,
                        entry_price=entry, exit_price=price,
                        pnl_percent=pct,
                        hold_duration_seconds=hold_s,
                        token_address=mint,
                        fraction=PARTIAL_TP_FRACTION,
                    )
                    log(f"  ✅ Partial TP: {sym} +{pct:.0f}%")

            elif exit_type == "TP":
                out = execute_sell(mint, 0.5)
                log(f"  Sell result: {out[:150]}")
                if "Signature" in out:
                    pos["tp_hit"]     = True
                    pos["entry"]      = price
                    pos["peak_price"] = price
                    changed = True
                    log_exit(
                        exit_type="TP", sym=sym, mint=mint,
                        entry_price=entry, exit_price=price,
                        pnl_percent=pct,
                        hold_duration_seconds=hold_s,
                        token_address=mint,
                    )
                    log(f"  ✅ TP: {sym} +{pct:.0f}%")

            elif exit_type in {"SL", "ROTATE", "TRAIL"}:
                out = execute_sell(mint)
                log(f"  Sell result: {out[:150]}")
                if "Signature" in out or "No tokens" in out:
                    log_exit(
                        exit_type=exit_type, sym=sym, mint=mint,
                        entry_price=entry, exit_price=price,
                        pnl_percent=pct,
                        hold_duration_seconds=hold_s,
                        token_address=mint,
                        reason=reason,
                        peak=peak,
                    )
                    record_bad_exit(mint, sym, pct)
                    record_reentry_cooldown(mint)
                    to_remove.append(mint)
                    log(f"  {'🛑' if exit_type=='SL' else '📉' if exit_type=='TRAIL' else '🔄'} {exit_type}: {sym} {pct:+.0f}%  {reason}")

            positions[mint] = pos
        except Exception as e:
            log(f"  Monitor error {pos.get('sym', '?')}: {e}")

    for m in to_remove:
        positions.pop(m, None)
        changed = True
    if changed or to_remove:
        save_positions(positions)

    # ── Scan / buy logic ───────────────────────────────────────────────────────
    open_slots = regime["max_positions"] - len(positions)

    if open_slots > 0 and sol_bal >= DEFAULT_RISK_SOL + 0.01:
        # Flush any queued signals first
        still_queued: list = []
        for qc in list(_signal_queue):
            if qc.get("addr") in positions or is_blacklisted(qc["addr"]) or in_rebuy_cooldown(qc["addr"]):
                continue  # stale
            if open_slots <= 0:
                still_queued.append(qc)
                continue
            buy_sol = buy_size_for_candidate(qc, sol_bal, regime)
            if buy_sol < DEFAULT_RISK_SOL:
                continue
            log(f"  [QUEUE] Buying {qc['sym']}: score={qc['score']:.1f} size={buy_sol:.3f} SOL")
            out = execute_buy(qc["addr"], buy_sol)
            log(f"  Buy result: {out[:200]}")
            if "Signature" in out:
                positions[qc["addr"]] = {
                    "sym":           qc["sym"],
                    "entry":         qc["price"],
                    "sol_spent":     buy_sol,
                    "bought_at":     time.time(),
                    "tp_hit":        False,
                    "partial_tp_hit": False,
                    "score":         qc.get("score", 0),
                    "peak_price":    qc["price"],
                    "liq":           qc.get("liq", 0),
                    "vol":           qc.get("vol", 0),
                }
                save_positions(positions)
                log_trade({
                    "event":     "BUY",
                    "sym":       qc["sym"],
                    "mint":      qc["addr"],
                    "entry":     qc["price"],
                    "sol":       buy_sol,
                    "score":     qc.get("score", 0),
                    "vol_accel": qc.get("vol_accel", 0),
                    "source":    "queue",
                })
                log(f"  ✅ Bought (from queue) {qc['sym']} @ ${qc['price']:.8f}")
                open_slots -= 1
        _signal_queue.clear()
        _signal_queue.extend(still_queued)

        # Fresh scan
        try:
            log("Scanning for opportunities...")
            raw_candidates = scan_opportunities(set(positions.keys()))
            candidates = [
                c for c in raw_candidates
                if c.get("score", 0) >= regime["min_score"]
                and not in_rebuy_cooldown(c["addr"])
                and not is_blacklisted(c["addr"])
            ]
            log(f"Found {len(candidates)} qualifying candidates (raw={len(raw_candidates)})")

            for c in candidates:
                if open_slots <= 0:
                    # Queue instead of dropping
                    if c["addr"] not in [q["addr"] for q in _signal_queue]:
                        _signal_queue.append(c)
                        log(f"  📋 Queued {c['sym']} score={c['score']:.1f} (no open slot)")
                    continue

                buy_sol = buy_size_for_candidate(c, sol_bal, regime)
                if buy_sol < DEFAULT_RISK_SOL:
                    continue

                high_conv = (
                    c.get("score", 0) >= HIGH_CONVICTION_SCORE
                    and c.get("vol_accel", 0) >= HIGH_CONVICTION_VOL_ACCEL
                )
                log(
                    f"  Buying {c['sym']}: score={c['score']:.1f} h1={c['h1']:+.1f}% "
                    f"liq=${c['liq']:,.0f} vol_accel={c.get('vol_accel',0):.2f}x "
                    f"size={buy_sol:.3f} SOL {'[HIGH CONV]' if high_conv else ''}"
                )
                out = execute_buy(c["addr"], buy_sol)
                log(f"  Buy result: {out[:200]}")
                if "Signature" in out:
                    positions[c["addr"]] = {
                        "sym":            c["sym"],
                        "entry":          c["price"],
                        "sol_spent":      buy_sol,
                        "bought_at":      time.time(),
                        "tp_hit":         False,
                        "partial_tp_hit": False,
                        "score":          c.get("score", 0),
                        "peak_price":     c["price"],
                        "liq":            c.get("liq", 0),
                        "vol":            c.get("vol", 0),
                    }
                    save_positions(positions)
                    log_trade({
                        "event":     "BUY",
                        "sym":       c["sym"],
                        "mint":      c["addr"],
                        "entry":     c["price"],
                        "sol":       buy_sol,
                        "score":     c.get("score", 0),
                        "vol_accel": c.get("vol_accel", 0),
                        "high_conviction": high_conv,
                    })
                    log(f"  ✅ Bought {c['sym']} @ ${c['price']:.8f}")
                    open_slots -= 1

        except Exception as e:
            log(f"Scan/buy error: {e}")

    else:
        if open_slots <= 0:
            log(f"Max positions ({regime['max_positions']}) reached — holding (queue size={len(_signal_queue)})")
        elif sol_bal < DEFAULT_RISK_SOL + 0.01:
            log(f"Insufficient SOL ({sol_bal:.4f}) — waiting")

    # ── Periodic status report (every 20 cycles) ───────────────────────────────
    if cycle % 20 == 0:
        snap    = pnl_snapshot(positions, sol_bal)
        pos_str = "\n".join(
            [f"• {p.get('sym','?')}: entry=${p.get('entry',p.get('entry_price',0)):.8f}"
             for p in positions.values()]
        ) or "None"
        log(
            f"STATUS — regime={regime['mode']} | win_rate={regime['win_rate']:.1%} "
            f"| realized_avg={snap['realized_avg_pct']:.1f}% | open_pnl={snap['open_pnl']:.4f} SOL "
            f"| wallet_est={snap['wallet_est']:.4f} SOL\n{pos_str}"
        )

    sleep_s = 10 if positions else CHECK_INTERVAL
    log(f"Sleeping {sleep_s}s...")
    time.sleep(sleep_s)
