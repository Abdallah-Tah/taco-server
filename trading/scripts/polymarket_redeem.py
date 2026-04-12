#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
LOCK_FILE = Path('/tmp/polymarket_redeem.lock')
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
JOURNAL_DB = ROOT / 'journal.db'
POSITIONS_API = 'https://data-api.polymarket.com/positions?user={user}'
POLYGON_RPCS = [
    'https://polygon-rpc.com',
    'https://rpc.ankr.com/polygon',
    'https://polygon-bor-rpc.publicnode.com',
    'https://polygon.drpc.org',
]
CHAIN_ID = 137
CTF_ADDRESS = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
USDC_E = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
ZERO32 = bytes.fromhex('00' * 32)

ERC20_ABI = [
    {
        'constant': True,
        'inputs': [{'name': '_owner', 'type': 'address'}],
        'name': 'balanceOf',
        'outputs': [{'name': 'balance', 'type': 'uint256'}],
        'payable': False,
        'stateMutability': 'view',
        'type': 'function',
    }
]

CTF_ABI = [
    {
        'inputs': [
            {'internalType': 'address', 'name': 'collateralToken', 'type': 'address'},
            {'internalType': 'bytes32', 'name': 'parentCollectionId', 'type': 'bytes32'},
            {'internalType': 'bytes32', 'name': 'conditionId', 'type': 'bytes32'},
            {'internalType': 'uint256[]', 'name': 'indexSets', 'type': 'uint256[]'},
        ],
        'name': 'redeemPositions',
        'outputs': [],
        'stateMutability': 'nonpayable',
        'type': 'function',
    }
]


def load_secrets():
    data = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def fetch_redeemables():
    s = load_secrets()
    user = s.get('POLYMARKET_FUNDER', '')
    if not user:
        return []
    r = requests.get(POSITIONS_API.format(user=user), timeout=20)
    positions = r.json() if r.status_code == 200 else []
    return [p for p in positions if p.get('redeemable') is True and p.get('conditionId')]


def _index_sets_for_position(position):
    """Use the held outcome side when we know it, which is cheaper than redeeming both sides."""
    try:
        outcome_index = int(position.get('outcomeIndex'))
        if outcome_index >= 0:
            return [1 << outcome_index]
    except Exception:
        pass
    return [1, 2]


def _fee_caps(w3):
    block = w3.eth.get_block('latest')
    base_fee = int(block.get('baseFeePerGas') or w3.eth.gas_price)
    priority_fee = max(w3.to_wei(25, 'gwei'), int(getattr(w3.eth, 'max_priority_fee', 0) or 0))
    max_fee = base_fee + priority_fee + w3.to_wei(1, 'gwei')
    return base_fee, priority_fee, max_fee


def _estimate_gas_with_balance_override(w3, tx, sender):
    """Estimate gas without the sender's current POL balance blocking estimation."""
    tx_for_rpc = {}
    for k, v in tx.items():
        if isinstance(v, bytes):
            tx_for_rpc[k] = '0x' + v.hex()
        elif isinstance(v, int):
            tx_for_rpc[k] = hex(v)
        else:
            tx_for_rpc[k] = v
    override = {sender: {'balance': hex(10**21)}}
    resp = w3.provider.make_request('eth_estimateGas', [tx_for_rpc, 'latest', override])
    if 'result' not in resp:
        raise RuntimeError(resp.get('error') or resp)
    return int(resp['result'], 16)


def _normalize_tx_hash(tx_hash):
    tx = str(tx_hash or '').strip().lower()
    if tx.startswith('0x'):
        tx = tx[2:]
    if not tx:
        return ''
    return f'0x{tx}'


def get_usdc_balance(w3, wallet_address):
    token = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    return int(token.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call())


def log_redeem_to_journal(title, condition_id, value, tx_hash, status):
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    tid = f"redeem_{condition_id}_{int(time.time())}"
    now = datetime.now(timezone.utc).isoformat()
    tx_canon = _normalize_tx_hash(tx_hash)
    c.execute(
        """
        INSERT INTO trades (
            engine, timestamp_open, timestamp_close, asset, category, direction,
            entry_price, exit_price, position_size, position_size_usd, pnl_absolute,
            pnl_percent, exit_type, hold_duration_seconds, regime, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            'polymarket_redeem', now, now, title[:200], 'redeem', 'REDEEM',
            0.0, 0.0, 0.0, 0.0, float(value or 0.0), 0.0,
            status, 0, 'normal', f'condition={condition_id} tx={tx_canon or tx_hash}'
        )
    )
    conn.commit()
    conn.close()


def get_web3():
    last_err = None
    for url in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 30}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                _ = w3.eth.chain_id
                return w3, url
        except Exception as e:
            last_err = e
    raise RuntimeError(f'No working Polygon RPC: {last_err}')


def acquire_lock():
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, ValueError, FileNotFoundError):
            try:
                LOCK_FILE.unlink()
            except:
                pass
            return acquire_lock()


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except:
        pass


def main():
    if not acquire_lock():
        print("Redeem script already running, exiting.")
        sys.exit(0)
    try:
        dry = '--dry-run' in sys.argv
        json_mode = '--json' in sys.argv
        redeemables = fetch_redeemables()
        grouped = {}
        for p in redeemables:
            grouped[p['conditionId']] = p

        summary = []
        if dry or not grouped:
            for condition_id, p in grouped.items():
                summary.append({
                    'title': p.get('title'),
                    'conditionId': condition_id,
                    'value': float(p.get('currentValue', 0) or 0),
                    'status': 'READY'
                })
            print(json.dumps(summary, indent=2) if json_mode else '\n'.join(f"READY | ${x['value']:.2f} | {x['title']}" for x in summary))
            return

        s = load_secrets()
        private_key = s.get('POLYMARKET_PRIVATE_KEY')
        funder = s.get('POLYMARKET_FUNDER')
        if not private_key or not funder:
            raise SystemExit('Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER')

        w3, rpc_url = get_web3()
        acct = w3.eth.account.from_key(private_key)
        contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        funder_checksum = Web3.to_checksum_address(funder)
        nonce = w3.eth.get_transaction_count(funder_checksum)

        ordered = sorted(
            grouped.items(),
            key=lambda item: float(item[1].get('currentValue', 0) or 0),
            reverse=True,
        )

        for condition_id, p in ordered:
            title = p.get('title', '')
            estimated_value = float(p.get('currentValue', 0) or 0)
            index_sets = _index_sets_for_position(p)
            try:
                _, priority_fee, max_fee = _fee_caps(w3)
                data = contract.functions.redeemPositions(
                    USDC_E,
                    ZERO32,
                    Web3.to_bytes(hexstr=condition_id),
                    index_sets,
                )._encode_transaction_data()
                tx = {
                    'from': funder_checksum,
                    'to': CTF_ADDRESS,
                    'data': data,
                    'value': 0,
                    'chainId': CHAIN_ID,
                    'nonce': nonce,
                    'maxFeePerGas': max_fee,
                    'maxPriorityFeePerGas': priority_fee,
                }
                gas_estimate = _estimate_gas_with_balance_override(w3, tx, funder_checksum)
                gas_limit = gas_estimate + max(2000, int(gas_estimate * 0.03))
                required_wei = gas_limit * max_fee
                available_wei = w3.eth.get_balance(funder_checksum)
                if available_wei < required_wei:
                    summary.append({
                        'title': title,
                        'conditionId': condition_id,
                        'value': estimated_value,
                        'status': 'insufficient_gas',
                        'indexSets': index_sets,
                        'requiredPol': float(w3.from_wei(required_wei, 'ether')),
                        'availablePol': float(w3.from_wei(available_wei, 'ether')),
                    })
                    continue
                tx['gas'] = gas_limit
                usdc_before = get_usdc_balance(w3, funder_checksum)
                signed = acct.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                usdc_after = get_usdc_balance(w3, funder_checksum)
                actual_value = max(0.0, (usdc_after - usdc_before) / 1_000_000)
                status = 'redeemed' if receipt.status == 1 else 'redeem_failed'
                log_redeem_to_journal(title, condition_id, actual_value, tx_hash, status)
                summary.append({
                    'title': title,
                    'conditionId': condition_id,
                    'value': actual_value,
                    'estimatedValue': estimated_value,
                    'indexSets': index_sets,
                    'txHash': tx_hash,
                    'status': status,
                })
                nonce += 1
            except Exception as e:
                summary.append({
                    'title': title,
                    'conditionId': condition_id,
                    'value': estimated_value,
                    'indexSets': index_sets,
                    'status': 'error',
                    'error': str(e),
                })

        print(json.dumps(summary, indent=2) if json_mode else '\n'.join(json.dumps(x) for x in summary))
    finally:
        release_lock()


if __name__ == '__main__':
    main()
