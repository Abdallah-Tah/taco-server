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


def log_redeem_to_journal(title, condition_id, value, tx_hash, status):
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    tid = f"redeem_{condition_id}_{int(time.time())}"
    now = datetime.now(timezone.utc).isoformat()
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
            status, 0, 'normal', f'condition={condition_id} tx={tx_hash}'
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
        nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(funder))

        for condition_id, p in grouped.items():
            title = p.get('title', '')
            value = float(p.get('currentValue', 0) or 0)
            try:
                tx = contract.functions.redeemPositions(
                    USDC_E,
                    ZERO32,
                    Web3.to_bytes(hexstr=condition_id),
                    [1, 2],
                ).build_transaction({
                    'from': Web3.to_checksum_address(funder),
                    'chainId': CHAIN_ID,
                    'nonce': nonce,
                })
                gas = w3.eth.estimate_gas(tx)
                tx['gas'] = int(gas * 1.2)
                tx['maxFeePerGas'] = w3.eth.gas_price
                tx['maxPriorityFeePerGas'] = min(w3.to_wei(40, 'gwei'), tx['maxFeePerGas'])
                signed = acct.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                status = 'redeemed' if receipt.status == 1 else 'redeem_failed'
                log_redeem_to_journal(title, condition_id, value, tx_hash, status)
                summary.append({
                    'title': title,
                    'conditionId': condition_id,
                    'value': value,
                    'txHash': tx_hash,
                    'status': status,
                })
                nonce += 1
            except Exception as e:
                summary.append({
                    'title': title,
                    'conditionId': condition_id,
                    'value': value,
                    'status': 'error',
                    'error': str(e),
                })

        print(json.dumps(summary, indent=2) if json_mode else '\n'.join(json.dumps(x) for x in summary))
    finally:
        release_lock()


if __name__ == '__main__':
    main()
