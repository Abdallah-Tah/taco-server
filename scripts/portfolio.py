#!/usr/bin/env python3
"""
scripts/portfolio.py — Real-time capital tracker across all engines.

Tracks:
- Solana wallet balance + open position estimated value
- Polymarket USDC cash + open position value
- Combined capital, drawdown, milestones, pause state

Usage:
  python3 portfolio.py status          # Print current portfolio status
  python3 portfolio.py update          # Refresh capital snapshot to disk
  python3 portfolio.py override-resume # Manually lift a drawdown pause
"""
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    MONTHLY_DRAWDOWN_LIMIT,
    PAUSE_THRESHOLD_USD,
    PAUSE_DURATION_HOURS,
    MILESTONE_AMOUNTS,
    DEFAULT_RISK_SOL,
)

# Insert trading root so sibling packages can be imported when run as script
sys.path.insert(0, str(Path(__file__).parent.parent))
from journal_old_package.db import init_db
from journal_old_package.analytics import check_milestones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TRADING_DIR     = Path(__file__).parent.parent
PORTFOLIO_FILE  = TRADING_DIR / ".portfolio.json"
SOL_POSITIONS   = TRADING_DIR / ".positions.json"
POLY_POSITIONS  = TRADING_DIR / ".poly_positions.json"
SECRETS_FILE    = Path.home() / ".config" / "openclaw" / "secrets.env"

TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
WALLET    = "J6nK35ud8u6hzqDxuEtVWPsAMzv2v7H6stsxXW2rsnuH"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_secrets() -> dict:
    data = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def _load_portfolio() -> dict:
    if PORTFOLIO_FILE.exists():
        try:
            return json.loads(PORTFOLIO_FILE.read_text())
        except Exception:
            pass
    return {
        "start_ts":          datetime.now(timezone.utc).isoformat(),
        "start_capital_usd": None,
        "pause_until_ts":    None,
        "paused_reason":     "",
        "last_update":       None,
        "monthly_high_usd":  None,
        "monthly_start_ts":  datetime.now(timezone.utc).isoformat(),
    }


def _save_portfolio(p: dict) -> None:
    p["last_update"] = datetime.now(timezone.utc).isoformat()
    PORTFOLIO_FILE.write_text(json.dumps(p, indent=2))


# ── Capital fetchers ───────────────────────────────────────────────────────────

def get_sol_balance_usd() -> tuple[float, float]:
    """Returns (sol_balance, sol_price_usd)."""
    import requests
    try:
        r = requests.post(
            "https://api.mainnet-beta.solana.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]},
            timeout=8,
        )
        sol = r.json()["result"]["value"] / 1e9
    except Exception as e:
        logger.warning("SOL balance fetch failed: %s", e)
        sol = 0.0

    try:
        r2 = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=8,
        )
        sol_usd = float(r2.json().get("solana", {}).get("usd", 0) or 0)
    except Exception:
        sol_usd = 150.0  # fallback estimate

    return sol, sol_usd


def get_sol_positions_usd(sol_usd: float) -> float:
    """Estimate USD value of open Solana positions."""
    import requests
    if not SOL_POSITIONS.exists():
        return 0.0
    try:
        positions = json.loads(SOL_POSITIONS.read_text())
    except Exception:
        return 0.0

    total = 0.0
    for mint, pos in positions.items():
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=6,
            )
            pairs = r.json().get("pairs", [])
            price_usd = float(pairs[0].get("priceUsd", 0) or 0) if pairs else 0.0
            entry = float(pos.get("entry", pos.get("entry_price", 0)) or 0)
            sol_spent = float(pos.get("sol_spent", 0) or 0)
            if entry > 0 and price_usd > 0:
                est_sol = sol_spent * (price_usd / entry)
                total += est_sol * sol_usd
            else:
                total += sol_spent * sol_usd
        except Exception:
            sol_spent = float(pos.get("sol_spent", 0) or 0)
            total += sol_spent * sol_usd

    return total


def get_poly_capital_usd() -> tuple[float, float]:
    """Returns (poly_cash_usd, poly_positions_usd)."""
    import requests
    try:
        import httpx
        from py_clob_client.http_helpers import helpers as _h
        _h._http_client = httpx.Client(proxy="socks5://127.0.0.1:9050", http2=True)
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        s = _load_secrets()
        creds = ApiCreds(
            api_key=s["POLYMARKET_API_KEY"],
            api_secret=s["POLYMARKET_API_SECRET"],
            api_passphrase=s["POLYMARKET_PASSPHRASE"],
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=s["POLYMARKET_PRIVATE_KEY"],
            signature_type=0,
            funder=s["POLYMARKET_FUNDER"],
            creds=creds,
        )
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        cash = int(bal.get("balance", 0)) / 1e6
    except Exception as e:
        logger.warning("Poly balance fetch failed: %s", e)
        cash = 0.0

    # Estimate open position value
    pos_value = 0.0
    if POLY_POSITIONS.exists():
        try:
            positions = json.loads(POLY_POSITIONS.read_text())
            tor = requests.Session()
            tor.proxies = TOR_PROXY
            for token_id, pos in positions.items():
                try:
                    r = tor.get(
                        "https://clob.polymarket.com/price",
                        params={"token_id": token_id, "side": "sell"},
                        timeout=8,
                    )
                    cur = float(r.json().get("price", 0) or 0)
                    pos_value += cur * float(pos.get("amount", 0) or 0)
                except Exception:
                    pos_value += float(pos.get("avg_price", 0) or 0) * float(pos.get("amount", 0) or 0)
        except Exception:
            pass

    return cash, pos_value


# ── Drawdown / pause logic ────────────────────────────────────────────────────

def is_paused(portfolio: dict) -> bool:
    pause_until = portfolio.get("pause_until_ts")
    if not pause_until:
        return False
    try:
        until = datetime.fromisoformat(pause_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def check_drawdown(portfolio: dict, current_usd: float) -> dict:
    """
    Pause ONLY if capital drops below PAUSE_THRESHOLD_USD.
    Returns updated portfolio dict.
    """
    # Reset monthly high at start of month
    monthly_start = portfolio.get("monthly_start_ts", "")
    try:
        ms = datetime.fromisoformat(monthly_start)
        now = datetime.now(timezone.utc)
        if (now.year, now.month) != (ms.year, ms.month):
            portfolio["monthly_start_ts"] = now.replace(day=1).isoformat()
            portfolio["monthly_high_usd"] = current_usd
    except Exception:
        portfolio["monthly_start_ts"] = datetime.now(timezone.utc).replace(day=1).isoformat()
        portfolio["monthly_high_usd"] = current_usd

    # Update monthly high
    monthly_high = float(portfolio.get("monthly_high_usd") or current_usd)
    if current_usd > monthly_high:
        portfolio["monthly_high_usd"] = current_usd
        monthly_high = current_usd

    # Clear stale capital-floor pause after recovery
    paused_reason = str(portfolio.get("paused_reason") or "")
    if current_usd >= PAUSE_THRESHOLD_USD and paused_reason.startswith("capital_below_floor"):
        portfolio.pop("pause_until_ts", None)
        portfolio.pop("paused_reason", None)

    # Check absolute floor only
    if current_usd < PAUSE_THRESHOLD_USD:
        if not is_paused(portfolio):
            from datetime import timedelta
            pause_until = (datetime.now(timezone.utc) + timedelta(hours=PAUSE_DURATION_HOURS)).isoformat()
            portfolio["pause_until_ts"] = pause_until
            portfolio["paused_reason"]  = f"capital_below_floor: ${current_usd:.2f} < ${PAUSE_THRESHOLD_USD}"
            logger.warning(
                "🛑 DRAWDOWN HALT: capital $%.2f < floor $%.2f — paused until %s",
                current_usd, PAUSE_THRESHOLD_USD, pause_until,
            )

    return portfolio


# ── Snapshot ───────────────────────────────────────────────────────────────────

def snapshot() -> dict:
    """
    Fetch all capital, compute totals, check drawdown/milestones.
    Returns full snapshot dict.
    """
    portfolio = _load_portfolio()

    sol_bal, sol_usd_price = get_sol_balance_usd()
    sol_pos_usd            = get_sol_positions_usd(sol_usd_price)
    poly_cash, poly_pos    = get_poly_capital_usd()

    sol_wallet_usd = sol_bal * sol_usd_price
    total_usd      = sol_wallet_usd + sol_pos_usd + poly_cash + poly_pos

    # Set start capital on first run
    if portfolio.get("start_capital_usd") is None:
        portfolio["start_capital_usd"] = total_usd
        logger.info("Portfolio start capital set: $%.2f", total_usd)

    start_capital = float(portfolio["start_capital_usd"])
    portfolio     = check_drawdown(portfolio, total_usd)

    # Check milestones
    newly_hit = check_milestones(total_usd, portfolio.get("start_ts"))
    if newly_hit:
        for m in newly_hit:
            logger.info("🏆 MILESTONE: $%.2f", m)

    snap = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "sol_balance":      round(sol_bal, 6),
        "sol_usd_price":    round(sol_usd_price, 2),
        "sol_wallet_usd":   round(sol_wallet_usd, 2),
        "sol_positions_usd": round(sol_pos_usd, 2),
        "poly_cash_usd":    round(poly_cash, 2),
        "poly_positions_usd": round(poly_pos, 2),
        "total_usd":        round(total_usd, 2),
        "start_capital_usd": round(start_capital, 2),
        "pnl_usd":          round(total_usd - start_capital, 2),
        "pnl_pct":          round((total_usd - start_capital) / start_capital * 100 if start_capital else 0, 2),
        "paused":           is_paused(portfolio),
        "pause_reason":     portfolio.get("paused_reason", ""),
        "next_milestone":   next((m for m in sorted(MILESTONE_AMOUNTS) if m > total_usd), None),
        "newly_hit_milestones": newly_hit,
    }

    _save_portfolio(portfolio)
    return snap


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "status"

    init_db()

    if cmd in ("status", "update"):
        snap = snapshot()
        print("=" * 50)
        print("PORTFOLIO SNAPSHOT")
        print("=" * 50)
        print(f"  Total capital:  ${snap['total_usd']:.2f}")
        print(f"  SOL wallet:     ${snap['sol_wallet_usd']:.2f} ({snap['sol_balance']:.4f} SOL @ ${snap['sol_usd_price']:.0f})")
        print(f"  SOL positions:  ${snap['sol_positions_usd']:.2f}")
        print(f"  Poly cash:      ${snap['poly_cash_usd']:.2f}")
        print(f"  Poly positions: ${snap['poly_positions_usd']:.2f}")
        print(f"  Start capital:  ${snap['start_capital_usd']:.2f}")
        print(f"  PnL:            ${snap['pnl_usd']:+.2f} ({snap['pnl_pct']:+.1f}%)")
        if snap["paused"]:
            print(f"  ⛔ PAUSED: {snap['pause_reason']}")
        if snap["next_milestone"]:
            print(f"  Next milestone: ${snap['next_milestone']:.0f}")

    elif cmd == "override-resume":
        portfolio = _load_portfolio()
        portfolio["pause_until_ts"] = None
        portfolio["paused_reason"]  = ""
        _save_portfolio(portfolio)
        print("✅ Drawdown pause lifted manually.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
