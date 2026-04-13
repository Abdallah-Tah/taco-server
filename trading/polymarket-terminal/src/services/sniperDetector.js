/**
 * sniperDetector.js
 * Detects upcoming 5-minute markets for configured assets (ETH, SOL, XRP, …)
 * using deterministic slug construction — same logic as mmDetector but for
 * multiple assets simultaneously.
 *
 * Slug format: {asset}-updown-5m-{eventStartTimestamp}
 * e.g.  eth-updown-5m-1771790700
 *       sol-updown-5m-1771790700
 *       xrp-updown-5m-1771790700
 *
 * NEVER enters the currently active market — always the NEXT upcoming slot.
 */

import config from '../config/index.js';
import logger from '../utils/logger.js';
import { isAssetInSession, getNextSessionInfo } from './schedule.js';
import { proxyFetch } from '../utils/proxy.js';

const SLOT_SEC = 5 * 60; // 300 seconds

let pollTimer = null;
let onMarketCb = null;
const seenKeys = new Set(); // `${asset}-${slotTimestamp}` already handled

// ── Slot helpers ──────────────────────────────────────────────────────────────

function currentSlot() {
    return Math.floor(Date.now() / 1000 / SLOT_SEC) * SLOT_SEC;
}

function nextSlot() {
    return currentSlot() + SLOT_SEC;
}

// ── Gamma API fetch ───────────────────────────────────────────────────────────

async function fetchBySlug(asset, slotTimestamp) {
    const slug = `${asset}-updown-5m-${slotTimestamp}`;
    try {
        const resp = await proxyFetch(`${config.gammaHost}/markets/slug/${slug}`);
        if (!resp.ok) return null;
        const data = await resp.json();
        return data?.conditionId ? data : null;
    } catch {
        return null;
    }
}

// ── Market data extraction ────────────────────────────────────────────────────

function extractMarketData(market, asset) {
    const conditionId = market.conditionId || market.condition_id || '';
    if (!conditionId) return null;

    let tokenIds = market.clobTokenIds ?? market.clob_token_ids;
    if (typeof tokenIds === 'string') {
        try { tokenIds = JSON.parse(tokenIds); } catch { tokenIds = null; }
    }

    let yesTokenId, noTokenId;
    if (Array.isArray(tokenIds) && tokenIds.length >= 2) {
        [yesTokenId, noTokenId] = tokenIds;
    } else if (Array.isArray(market.tokens) && market.tokens.length >= 2) {
        yesTokenId = market.tokens[0]?.token_id ?? market.tokens[0]?.tokenId;
        noTokenId = market.tokens[1]?.token_id ?? market.tokens[1]?.tokenId;
    }

    if (!yesTokenId || !noTokenId) return null;

    return {
        asset,
        conditionId,
        question: market.question || market.title || '',
        endTime: market.endDate || market.end_date_iso || market.endDateIso,
        eventStartTime: market.eventStartTime || market.event_start_time,
        yesTokenId: String(yesTokenId),
        noTokenId: String(noTokenId),
        negRisk: market.negRisk ?? market.neg_risk ?? false,
        tickSize: String(market.orderPriceMinTickSize ?? market.minimum_tick_size ?? '0.01'),
    };
}

// ── Schedule an asset slot ────────────────────────────────────────────────────

async function scheduleAsset(asset, slotTimestamp, isCurrent = false) {
    const key = `${asset}-${slotTimestamp}`;
    if (seenKeys.has(key)) return;

    const market = await fetchBySlug(asset, slotTimestamp);
    if (!market) return; // not in API yet, poll will retry

    const data = extractMarketData(market, asset);
    if (!data) {
        logger.warn(`SNIPER: skipping ${asset} slot ${slotTimestamp} — missing token IDs`);
        seenKeys.add(key);
        return;
    }

    seenKeys.add(key);

    if (isCurrent) {
        // Current slot: only place orders if there's at least 30 seconds of market left
        const endAt = data.endTime ? new Date(data.endTime).getTime() : (slotTimestamp + SLOT_SEC) * 1000;
        const secsLeft = Math.round((endAt - Date.now()) / 1000);
        if (secsLeft < 30) {
            logger.info(`SNIPER: ${asset.toUpperCase()} current market closing soon (${secsLeft}s) — skipping`);
            return;
        }
        logger.success(`SNIPER: ${asset.toUpperCase()} current market active (${secsLeft}s left) — placing orders now`);
    } else {
        // Next slot: market hasn't opened yet
        const openAt = data.eventStartTime ? new Date(data.eventStartTime).getTime() : slotTimestamp * 1000;
        const secsUntilOpen = Math.round((openAt - Date.now()) / 1000);
        logger.success(`SNIPER: ${asset.toUpperCase()} found "${data.question.slice(0, 40)}"${secsUntilOpen > 0 ? ` — ${secsUntilOpen}s before open` : ''}`);
    }

    if (onMarketCb) onMarketCb(data);
}

// ── Poll ──────────────────────────────────────────────────────────────────────

async function poll() {
    try {
        const curr = currentSlot();
        const next = nextSlot();

        // Filter assets by trading session schedule
        const activeAssets = config.sniperAssets.filter((asset) => {
            if (!isAssetInSession(asset)) {
                const nextInfo = getNextSessionInfo(asset);
                const key = `skip-${asset}-${Math.floor(Date.now() / 60000)}`; // log once per minute
                if (!seenKeys.has(key)) {
                    seenKeys.add(key);
                    logger.info(`SNIPER: ${asset.toUpperCase()} outside session window${nextInfo ? ` — next in ${nextInfo}` : ''}`);
                }
                return false;
            }
            return true;
        });

        if (activeAssets.length === 0) return;

        // Check current active market AND the upcoming next one, in parallel for each asset
        await Promise.all(activeAssets.flatMap((asset) => [
            scheduleAsset(asset, curr, true),  // current market (if still has time left)
            scheduleAsset(asset, next, false), // next upcoming market
        ]));
    } catch (err) {
        logger.error('SNIPER detector poll error:', err.message);
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

export function startSniperDetector(onNewMarket) {
    onMarketCb = onNewMarket;
    seenKeys.clear();

    poll();
    pollTimer = setInterval(poll, config.mmPollInterval);

    const ns = nextSlot();
    const secsUntil = ns - Math.floor(Date.now() / 1000);
    logger.info(`SNIPER detector started — assets: ${config.sniperAssets.join(', ').toUpperCase()}`);
    logger.info(`Next slot: *-updown-5m-${ns} (opens in ${secsUntil}s)`);
    const prices = config.sniperTierPrices;
    logger.info(`Order: 3-tier ${prices[0]}c/${prices[1]}c/${prices[2]}c (max ${config.sniperMaxShares} shares per side)`);
}

export function stopSniperDetector() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}
