"""Shared collateral balance + allowance helpers."""

from __future__ import annotations

import time

from web3 import Web3

from .client import AssetType, BalanceAllowanceParams, load_secrets, resolve_clob_identity
from .errors import ALLOWANCE_LOOKUP_ERROR, ALLOWANCE_OK, CLOB_ALLOWANCE_MISMATCH, INSUFFICIENT_BALANCE, MISSING_ALLOWANCE

USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e (legacy)
PUSD_POLYGON = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"  # Polymarket pUSD (V2 collateral)
POLYGON_RPC_FALLBACKS = [
    "https://polygon.publicnode.com",
    "https://polygon-bor.publicnode.com",
    "https://polygon.drpc.org",
]
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"},
]


def _coerce_int(raw) -> int:
    try:
        return int(raw)
    except Exception:
        try:
            return int(float(raw))
        except Exception:
            return 0


def snapshot_collateral_account_state(client, *, refresh: bool = False, refresh_sleep_sec: float = 1.0) -> dict:
    """Return collateral balance + allowance state for the current authenticated wallet."""
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

    def _fetch():
        resp = client.get_balance_allowance(params)
        allowances = {
            spender: _coerce_int(raw)
            for spender, raw in (resp.get("allowances") or {}).items()
        }
        return {
            "balance": _coerce_int(resp.get("balance") or 0),
            "allowances": allowances,
            "raw": resp,
        }

    try:
        snapshot = _fetch()
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "reason": ALLOWANCE_LOOKUP_ERROR,
            "balance": 0,
            "allowances": {},
            "refresh_error": None,
            "refreshed": False,
        }

    refresh_error = None
    refreshed = False
    if refresh:
        try:
            client.update_balance_allowance(params)
            refreshed = True
            time.sleep(refresh_sleep_sec)
        except Exception as e:
            refresh_error = str(e)
        else:
            try:
                snapshot = _fetch()
            except Exception as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "reason": ALLOWANCE_LOOKUP_ERROR,
                    "balance": snapshot.get("balance", 0),
                    "allowances": snapshot.get("allowances", {}),
                    "refresh_error": refresh_error,
                    "refreshed": refreshed,
                }

    snapshot.update({
        "ok": True,
        "error": None,
        "reason": ALLOWANCE_OK,
        "refresh_error": refresh_error,
        "refreshed": refreshed,
    })
    return snapshot


def _onchain_collateral_fallback(spenders: dict[str, int], funder: str | None = None) -> dict | None:
    """Query on-chain USDC balance/allowance directly when CLOB cache looks stale."""
    secrets = load_secrets()
    resolved_funder, _ = resolve_clob_identity(secrets)
    funder = funder or resolved_funder or secrets.get("POLYMARKET_FUNDER")
    if not funder:
        return None
    funder = Web3.to_checksum_address(funder)
    spender_addrs = list(spenders.keys())
    # Always query on-chain balance even if no spenders — CLOB may be stale
    spender_addrs = spender_addrs or ["0xE111180000d2663C0091e4f400237545B87B996B"]

    for rpc in POLYGON_RPC_FALLBACKS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if not w3.is_connected():
                continue
            # Check pUSD (V2 collateral) first, then fall back to USDC.e (legacy)
            collateral_token = PUSD_POLYGON
            collateral_label = "pUSD"
            token_contract = w3.eth.contract(address=Web3.to_checksum_address(collateral_token), abi=ERC20_ABI)
            balance = int(token_contract.functions.balanceOf(funder).call())
            if balance == 0:
                # Fall back to USDC.e for legacy V1 compatibility
                usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_POLYGON), abi=ERC20_ABI)
                usdc_bal = int(usdc_contract.functions.balanceOf(funder).call())
                if usdc_bal > 0:
                    balance = usdc_bal
                    collateral_label = "USDC.e"
                    token_contract = usdc_contract
            allowances = {
                spender: int(token_contract.functions.allowance(funder, Web3.to_checksum_address(spender)).call())
                for spender in spender_addrs
            }
            return {
                "balance": balance,
                "allowances": allowances,
                "rpc": rpc,
                "source": f"onchain_fallback_{collateral_label}",
            }
        except Exception:
            continue
    return None


def ensure_collateral_allowance(client, required_usdc: float = 0.0, *, refresh: bool = True) -> tuple[bool, dict]:
    """Verify that collateral balance and allowance can fund a BUY order."""
    required_wei = int(max(required_usdc, 0.0) * 1_000_000)
    state = snapshot_collateral_account_state(client, refresh=refresh)
    detail = {
        "balance": state.get("balance", 0),
        "allowances": state.get("allowances", {}),
        "required": required_wei,
        "refresh_error": state.get("refresh_error"),
        "refreshed": state.get("refreshed", False),
        "source": "clob",
    }
    if not state.get("ok"):
        detail.update({
            "reason": ALLOWANCE_LOOKUP_ERROR,
            "error": state.get("error"),
        })
        return False, detail

    # Check balance: CLOB first, then on-chain fallback if stale.
    secrets = load_secrets()
    # Check both the EOA (FUNDER) and the CLOB proxy (CLOB_FUNDER) on-chain.
    # With a Gnosis Safe proxy, the CLOB_FUNDER holds the actual USDC.e balance.
    eoa_funder = secrets.get("POLYMARKET_FUNDER")
    clob_funder = secrets.get("POLYMARKET_CLOB_FUNDER")
    balance_ok = detail["balance"] >= required_wei

    if not balance_ok:
        # CLOB balance is below required — try on-chain before rejecting.
        # Try CLOB_FUNDER first (proxy wallet holds the real balance),
        # then fall back to EOA FUNDER.
        checked_funder = clob_funder or eoa_funder
        fallback = _onchain_collateral_fallback(detail["allowances"], funder=checked_funder)
        if fallback and fallback["balance"] >= required_wei:
            detail["onchain_balance"] = fallback["balance"]
            detail["onchain_allowances"] = fallback["allowances"]
            detail["onchain_rpc"] = fallback["rpc"]
            balance_ok = True
            detail["source"] = "onchain_fallback"
        else:
            # Try EOA funder as secondary
            if clob_funder and eoa_funder and eoa_funder != clob_funder:
                fallback = _onchain_collateral_fallback(detail["allowances"], funder=eoa_funder)
                if fallback:
                    detail.setdefault("onchain_balance", fallback["balance"])
                    detail.setdefault("onchain_allowances", fallback["allowances"])
                    detail.setdefault("onchain_rpc", fallback["rpc"])
                    if fallback["balance"] >= required_wei:
                        balance_ok = True
                        detail["source"] = "onchain_fallback_eoa"
            if not balance_ok:
                detail["reason"] = INSUFFICIENT_BALANCE
                return False, detail

    if balance_ok and max(detail["allowances"].values(), default=0) >= required_wei:
        detail["reason"] = ALLOWANCE_OK
        return True, detail

    # Allowance check: CLOB first, then on-chain fallback
    # Try CLOB_FUNDER (proxy) first, then EOA FUNDER
    allowance_funder = clob_funder or eoa_funder
    fallback = _onchain_collateral_fallback(detail["allowances"], funder=allowance_funder)
    if not fallback and balance_ok:
        # Already had on-chain fallback from balance check? Try it once more
        builder_funder = getattr(getattr(client, "builder", None), "funder", None)
        fallback = _onchain_collateral_fallback(detail["allowances"], funder=builder_funder)
    if fallback:
        detail["onchain_balance"] = detail.get("onchain_balance", fallback["balance"])
        detail["onchain_allowances"] = fallback["allowances"]
        detail["onchain_rpc"] = detail.get("onchain_rpc", fallback["rpc"])
        if max(fallback["allowances"].values(), default=0) >= required_wei:
            # On-chain balance + allowances are sufficient, but CLOB cache is stale.
            # Allow the trade — Polymarket CLOB will read on-chain state when placing the order.
            detail["reason"] = CLOB_ALLOWANCE_MISMATCH
            return True, detail

    detail["reason"] = MISSING_ALLOWANCE
    return False, detail
