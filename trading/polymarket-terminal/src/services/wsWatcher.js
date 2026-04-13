import WebSocket from 'ws';
import config from '../config/index.js';
import logger from '../utils/logger.js';
import { readState, writeState } from '../utils/state.js';

const RTDS_WS_URL = 'wss://ws-live-data.polymarket.com';
const PING_INTERVAL_MS = 5000;
const INITIAL_RECONNECT_DELAY = 2000;
const MAX_RECONNECT_DELAY = 30000;
const PROCESSED_FILE = 'processed_trades.json';

let ws = null;
let pingTimer = null;
let reconnectTimer = null;
let reconnectDelay = INITIAL_RECONNECT_DELAY;
let tradeHandler = null;
let isShuttingDown = false;

function getProcessedIds() {
    return readState(PROCESSED_FILE, { tradeIds: [] });
}

function markProcessed(tradeId) {
    const data = getProcessedIds();
    if (data.tradeIds.includes(tradeId)) return false;
    data.tradeIds.push(tradeId);
    if (data.tradeIds.length > 500) {
        data.tradeIds = data.tradeIds.slice(-500);
    }
    writeState(PROCESSED_FILE, data);
    return true;
}

function handleMessage(rawData) {
    let msg;
    try {
        msg = JSON.parse(rawData.toString());
    } catch {
        const text = rawData.toString().trim();
        if (text === 'ping') {
            ws?.send('pong');
        }
        return;
    }

    // Handle ping/heartbeat
    if (msg.type === 'ping' || msg === 'ping') {
        ws?.send('pong');
        return;
    }

    // Only process activity trade events
    if (msg.topic !== 'activity') return;

    const payload = msg.payload;
    if (!payload) return;

    // Filter by target trader's address (case-insensitive)
    const traderAddr = config.traderAddress.toLowerCase();
    const proxyWallet = (payload.proxyWallet || payload.proxy_wallet || '').toLowerCase();

    if (!proxyWallet || proxyWallet !== traderAddr) return;

    // Build trade ID
    const tradeId = payload.transactionHash || payload.transaction_hash ||
        `${payload.timestamp}_${payload.asset}`;

    // Deduplication
    if (!markProcessed(tradeId)) {
        logger.watch(`Duplicate trade skipped: ${tradeId}`);
        return;
    }

    // Parse trade type
    const type = (payload.side || '').toUpperCase();
    if (!['BUY', 'SELL'].includes(type)) {
        logger.warn(`Unknown trade side: ${payload.side}`);
        return;
    }

    const tokenId = payload.asset || '';
    if (!tokenId) {
        logger.warn(`Trade missing asset/tokenId: ${tradeId}`);
        return;
    }

    const trade = {
        id: tradeId,
        type,
        tokenId,
        conditionId: payload.conditionId || payload.condition_id || '',
        market: payload.title || payload.name || '',
        price: parseFloat(payload.price || '0'),
        size: parseFloat(payload.size || '0'),
        side: type,
        timestamp: payload.timestamp || new Date().toISOString(),
        outcome: payload.outcome || '',
        proxyWalletAddress: payload.proxyWallet || '',
    };

    logger.watch(`Trade detected! ${type} - ${trade.market || trade.tokenId}`);
    logger.watch(`  Size: ${trade.size} shares @ $${trade.price}`);

    if (tradeHandler) {
        tradeHandler(trade).catch((err) => {
            logger.error(`Error handling trade: ${err.message}`);
        });
    }
}

function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send('ping');
        }
    }, PING_INTERVAL_MS);
}

function stopPing() {
    if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
    }
}

function cleanup(reconnect = true) {
    stopPing();
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws) {
        ws.removeAllListeners();
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
            ws.terminate();
        }
        ws = null;
    }
    if (reconnect && !isShuttingDown) {
        scheduleReconnect();
    }
}

function scheduleReconnect() {
    logger.info(`Reconnecting in ${reconnectDelay / 1000}s...`);
    reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        connect();
    }, reconnectDelay);
}

function connect() {
    if (isShuttingDown) return;

    logger.info('Connecting to Polymarket RTDS WebSocket...');
    ws = new WebSocket(RTDS_WS_URL);

    ws.on('open', () => {
        logger.success('WebSocket connected! Subscribing to activity feed...');
        logger.watch(`Watching trader: ${config.traderAddress}`);
        reconnectDelay = INITIAL_RECONNECT_DELAY;

        ws.send(JSON.stringify({
            action: 'subscribe',
            subscriptions: [{
                topic: 'activity',
                type: 'trades',
            }],
        }));

        startPing();
    });

    ws.on('message', (data) => {
        handleMessage(data);
    });

    ws.on('ping', () => {
        ws?.pong();
    });

    ws.on('close', (code, reason) => {
        const reasonStr = reason ? reason.toString() : 'no reason';
        logger.warn(`WebSocket closed (${code}): ${reasonStr}`);
        cleanup(true);
    });

    ws.on('error', (err) => {
        logger.error(`WebSocket error: ${err.message}`);
        cleanup(true);
    });
}

/**
 * Start the real-time WebSocket watcher
 * @param {Function} onTrade - async function called when trader makes a trade
 */
export function startWsWatcher(onTrade) {
    tradeHandler = onTrade;
    isShuttingDown = false;
    reconnectDelay = INITIAL_RECONNECT_DELAY;
    connect();
}

/**
 * Stop the WebSocket watcher
 */
export function stopWsWatcher() {
    isShuttingDown = true;
    cleanup(false);
    logger.info('WebSocket watcher stopped');
}
