#!/bin/bash
set -u

ROOT="/home/abdaltm86/.openclaw/workspace/trading"
VENV="$ROOT/.polymarket-venv/bin/python3"
RECON="$ROOT/scripts/polymarket_reconcile.py"
TMP_JSON="/tmp/hourly_15m_recon.json"
TMP_BAL_PY="/tmp/hourly_poly_balance.py"

"$VENV" "$RECON" --json --no-sync > "$TMP_JSON" 2>/dev/null || echo '{}' > "$TMP_JSON"

metrics=$(python3 - <<'PY'
import json
from pathlib import Path
p=Path('/tmp/hourly_15m_recon.json')
try:
    data=json.loads(p.read_text())
except Exception:
    data={}
by=data.get('posted_stats', {})
btc=by.get('btc15m', {})
eth=by.get('eth15m', {})
pending=data.get('summary', {}).get('pending', 0)
print('|'.join([
    str(btc.get('posted_orders', 0)),
    str(btc.get('filled_orders', 0)),
    f"{(float(btc.get('filled_orders', 0)) / float(btc.get('posted_orders', 1)) * 100.0) if float(btc.get('posted_orders', 0) or 0) else 0.0:.1f}",
    str(eth.get('posted_orders', 0)),
    str(eth.get('filled_orders', 0)),
    f"{(float(eth.get('filled_orders', 0)) / float(eth.get('posted_orders', 1)) * 100.0) if float(eth.get('posted_orders', 0) or 0) else 0.0:.1f}",
    str(pending),
]))
PY
)

IFS='|' read -r btc_posted btc_filled btc_fill_rate eth_posted eth_filled eth_fill_rate pending <<< "$metrics"

cat > "$TMP_BAL_PY" <<'PY'
from pathlib import Path
import httpx
from py_clob_client.http_helpers import helpers as _clob_helpers
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
_clob_helpers._http_client = httpx.Client(proxy='socks5://127.0.0.1:9050', http2=True)
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
def load_secrets():
    data={}
    for line in SECRETS.read_text().splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1)
            data[k.strip()]=v.strip().strip('"').strip("'")
    return data
s=load_secrets()
creds=ApiCreds(api_key=s['POLYMARKET_API_KEY'], api_secret=s['POLYMARKET_API_SECRET'], api_passphrase=s['POLYMARKET_PASSPHRASE'])
client=ClobClient(host='https://clob.polymarket.com', chain_id=137, key=s['POLYMARKET_PRIVATE_KEY'], signature_type=0, funder=s['POLYMARKET_FUNDER'], creds=creds)
bal=client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"${int(bal['balance'])/1e6:.2f}")
PY

bal=$($VENV "$TMP_BAL_PY" 2>/dev/null || echo 'error')

printf '[HOURLY] BTC posted: %s, filled: %s (fill rate %s%%) | ETH posted: %s, filled: %s (fill rate %s%%) | pending/open: %s | wallet: %s\n' \
  "$btc_posted" "$btc_filled" "$btc_fill_rate" "$eth_posted" "$eth_filled" "$eth_fill_rate" "$pending" "$bal"
