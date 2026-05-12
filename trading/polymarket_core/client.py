"""Shared Polymarket CLOB client construction and compatibility helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 0  # EOA default; override with POLYMARKET_CLOB_SIGNATURE_TYPE
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
SECRETS_FILE = Path.home() / ".config" / "openclaw" / "secrets.env"

SDK_VERSION = None
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds,
        OrderArgs,
        OrderType,
        BalanceAllowanceParams,
        AssetType,
        OrderPayload,
        OpenOrderParams,
        BuilderConfig,
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    try:
        from py_clob_client_v2.exceptions import PolyApiException
    except ImportError:
        PolyApiException = Exception
    from py_clob_client_v2.http_helpers import helpers as _clob_helpers
    SDK_VERSION = "v2"
except ImportError:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            ApiCreds,
            OrderArgs,
            OrderType,
            BalanceAllowanceParams,
            AssetType,
        )
        from py_clob_client.order_builder.constants import BUY, SELL
        try:
            from py_clob_client.exceptions import PolyApiException
        except ImportError:
            PolyApiException = Exception
        from py_clob_client.http_helpers import helpers as _clob_helpers
        OrderPayload = None
        OpenOrderParams = None
        BuilderConfig = None
        SDK_VERSION = "v1"
    except ImportError:
        print(
            "ERROR: neither py-clob-client-v2 nor py-clob-client installed. Run: pip install py-clob-client-v2",
            file=sys.stderr,
        )
        raise

# Patch the SDK's internal httpx client once so all CLOB traffic stays on the
# same Tor-routed transport the current Polymarket stack expects.
_clob_helpers._http_client = httpx.Client(proxy="socks5://127.0.0.1:9050", http2=True)


def load_secrets(secrets_file: Path | None = None) -> dict[str, str]:
    """Load Polymarket credentials from secrets.env."""
    secrets_path = Path(secrets_file or SECRETS_FILE)
    secrets: dict[str, str] = {}
    if secrets_path.exists():
        for line in secrets_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def resolve_clob_identity(secrets: dict[str, str] | None = None) -> tuple[str | None, int]:
    """Resolve the active CLOB funder/signature mode without disturbing base-wallet secrets."""
    secrets = secrets or {}
    funder = (
        os.environ.get("POLYMARKET_CLOB_FUNDER")
        or secrets.get("POLYMARKET_CLOB_FUNDER")
        or os.environ.get("POLYMARKET_FUNDER")
        or secrets.get("POLYMARKET_FUNDER")
    )
    raw_signature_type = (
        os.environ.get("POLYMARKET_CLOB_SIGNATURE_TYPE")
        or secrets.get("POLYMARKET_CLOB_SIGNATURE_TYPE")
    )
    try:
        signature_type = int(raw_signature_type) if raw_signature_type not in (None, "") else SIGNATURE_TYPE
    except Exception:
        signature_type = SIGNATURE_TYPE
    return funder, signature_type


def get_client(secrets_file: Path | None = None):
    """Create authenticated ClobClient routed through the shared Tor patch."""
    secrets = load_secrets(secrets_file=secrets_file)

    funder, signature_type = resolve_clob_identity(secrets)
    private_key = secrets.get("POLYMARKET_PRIVATE_KEY")
    api_key = secrets.get("POLYMARKET_API_KEY")
    api_secret = secrets.get("POLYMARKET_API_SECRET")
    passphrase = secrets.get("POLYMARKET_PASSPHRASE")

    if not all([funder, private_key, api_key, api_secret, passphrase]):
        print("ERROR: Missing Polymarket credentials in secrets.env", file=sys.stderr)
        sys.exit(1)

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    )

    kwargs = dict(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        signature_type=signature_type,
        funder=funder,
        creds=creds,
    )
    if SDK_VERSION == "v2" and BuilderConfig is not None:
        builder_code = secrets.get("POLYMARKET_BUILDER_CODE") or os.environ.get("POLYMARKET_BUILDER_CODE")
        builder_address = secrets.get("POLYMARKET_BUILDER_ADDRESS") or os.environ.get("POLYMARKET_BUILDER_ADDRESS") or ""
        if builder_code:
            kwargs["builder_config"] = BuilderConfig(
                builder_address=builder_address,
                builder_code=builder_code,
            )

    return ClobClient(**kwargs)


def list_open_orders(client):
    """SDK-agnostic open-order listing."""
    if hasattr(client, "get_open_orders"):
        try:
            return client.get_open_orders() or []
        except TypeError:
            return client.get_open_orders(None) or []
    return client.get_orders() or []


def cancel_one(client, order_id: str):
    """SDK-agnostic single-order cancel."""
    if SDK_VERSION == "v2" and OrderPayload is not None and hasattr(client, "cancel_order"):
        return client.cancel_order(OrderPayload(orderID=order_id))
    if hasattr(client, "cancel"):
        return client.cancel(order_id)
    if hasattr(client, "cancel_order"):
        return client.cancel_order(order_id)
    raise AttributeError("ClobClient has no cancel / cancel_order method")


def post_order_compat(client, signed, order_type, post_only: bool = False):
    """Handle the v1/v2 post_order signature rename."""
    try:
        return client.post_order(signed, order_type, post_only)
    except TypeError:
        try:
            return client.post_order(signed, orderType=order_type, post_only=post_only)
        except TypeError:
            return client.post_order(signed, order_type=order_type, post_only=post_only)
