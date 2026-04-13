import config from '../config/index.js';
import logger from '../utils/logger.js';
import { readState, writeState } from '../utils/state.js';
import { proxyFetch } from '../utils/proxy.js';

const PROCESSED_FILE = 'processed_trades.json';

/**
 * Fetch trader's recent activity from Data API
 * @returns {Array} List of trade activities
 */
async function fetchTraderActivity() {
    const url = `${config.dataHost}/activity?user=${config.traderAddress}`;
    try {
        const response = await proxyFetch(url);
        if (!response.ok) {
            throw new Error(`Data API returned ${response.status}`);
        }
        return await response.json();
    } catch (err) {
        logger.error('Failed to fetch trader activity:', err.message);
        return [];
    }
}

/**
 * Get list of already processed trade IDs
 */
function getProcessedTrades() {
    return readState(PROCESSED_FILE, { tradeIds: [] });
}

/**
 * Mark a trade as processed
 */
function markTradeProcessed(tradeId) {
    const data = getProcessedTrades();
    data.tradeIds.push(tradeId);
    // Keep only last 500 trade IDs to prevent unbounded growth
    if (data.tradeIds.length > 500) {
        data.tradeIds = data.tradeIds.slice(-500);
    }
    writeState(PROCESSED_FILE, data);
}

/**
 * Check for new trades from the watched trader
 * @returns {Array} New trades to process: { id, type, tokenId, conditionId, market, price, size, timestamp, side }
 */
export async function checkNewTrades() {
    const activities = await fetchTraderActivity();
    const processed = getProcessedTrades();

    if (!Array.isArray(activities) || activities.length === 0) {
        return [];
    }

    const newTrades = [];

    for (const activity of activities) {
        // Data API: type = "TRADE" always, direction is in "side" (BUY / SELL)
        // Unique dedup key: txHash + asset + side (one tx can have multiple token trades)
        const tradeId = activity.transactionHash
            ? `${activity.transactionHash}_${activity.asset}_${activity.side}`
            : `${activity.timestamp}_${activity.asset}_${activity.side}`;

        if (processed.tradeIds.includes(tradeId)) {
            continue;
        }

        // Only process TRADE type with BUY or SELL side
        const actType = (activity.type || '').toUpperCase();
        const side = (activity.side || '').toUpperCase();

        if (actType !== 'TRADE' || !['BUY', 'SELL'].includes(side)) {
            markTradeProcessed(tradeId);
            continue;
        }

        const trade = {
            id: tradeId,
            type: side,                                                    // BUY or SELL
            tokenId: activity.asset || '',
            conditionId: activity.conditionId || '',
            market: activity.title || activity.question || '',
            price: parseFloat(activity.price || '0'),
            size: parseFloat(activity.usdcSize || '0'),                   // USDC value
            shares: parseFloat(activity.size || '0'),                   // token shares
            side,
            outcome: activity.outcome || '',
            outcomeIndex: activity.outcomeIndex ?? null,
            timestamp: activity.timestamp || Date.now() / 1000,
            txHash: activity.transactionHash || '',
        };

        if (!trade.tokenId) {
            logger.warn(`Skipping trade without tokenId: ${tradeId}`);
            markTradeProcessed(tradeId);
            continue;
        }

        newTrades.push(trade);
    }

    return newTrades;
}


/**
 * Mark trade as processed after handling
 */
export { markTradeProcessed };

/**
 * Fetch market info from Gamma API by condition ID
 */
export async function fetchMarketInfo(conditionId) {
    try {
        const url = `${config.gammaHost}/markets?condition_id=${conditionId}`;
        const response = await proxyFetch(url);
        if (!response.ok) return null;
        const markets = await response.json();
        return markets && markets.length > 0 ? markets[0] : null;
    } catch (err) {
        logger.error('Failed to fetch market info:', err.message);
        return null;
    }
}

/**
 * Fetch market info by token ID (CLOB token)
 */
export async function fetchMarketByTokenId(tokenId) {
    try {
        const url = `${config.gammaHost}/markets?clob_token_ids=${tokenId}`;
        const response = await proxyFetch(url);
        if (!response.ok) return null;
        const markets = await response.json();
        return markets && markets.length > 0 ? markets[0] : null;
    } catch (err) {
        logger.error('Failed to fetch market by tokenId:', err.message);
        return null;
    }
}
