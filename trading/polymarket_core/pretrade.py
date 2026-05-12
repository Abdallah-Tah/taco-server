"""Shared Polymarket pre-trade checks."""

from __future__ import annotations

from .account_state import ensure_collateral_allowance
from .errors import ALLOWANCE_LOOKUP_ERROR, ALLOWANCE_OK, CLOB_ALLOWANCE_MISMATCH, INSUFFICIENT_BALANCE, MISSING_ALLOWANCE

_REASON_TEXT = {
    ALLOWANCE_OK: "collateral allowance is healthy",
    ALLOWANCE_LOOKUP_ERROR: "failed to load balance/allowance state",
    CLOB_ALLOWANCE_MISMATCH: "on-chain allowance exists but Polymarket CLOB still reports zero; live order path is not ready",
    INSUFFICIENT_BALANCE: "wallet balance is below required buy notional",
    MISSING_ALLOWANCE: "wallet balance exists but required collateral allowance is missing",
}


def pre_trade_check_buy(client, required_usdc: float, *, refresh_allowance: bool = True, metadata: dict | None = None) -> dict:
    """Run the minimum shared BUY pre-trade gate for Polymarket bots."""
    ok, detail = ensure_collateral_allowance(client, required_usdc, refresh=refresh_allowance)
    reason_code = detail.get("reason") or (ALLOWANCE_OK if ok else MISSING_ALLOWANCE)
    return {
        "ok": ok,
        "reason_code": reason_code,
        "reason": _REASON_TEXT.get(reason_code, reason_code.replace("_", " ").lower()),
        "severity": "info" if ok else "block",
        "required_usdc": required_usdc,
        "account_state": {
            "balance": detail.get("balance"),
            "allowances": detail.get("allowances"),
            "required": detail.get("required"),
            "refresh_error": detail.get("refresh_error"),
            "refreshed": detail.get("refreshed"),
        },
        "checks": [
            {
                "name": "collateral_allowance",
                "ok": ok,
                "reason_code": reason_code,
                "detail": detail,
            }
        ],
        "metadata": metadata or {},
    }
