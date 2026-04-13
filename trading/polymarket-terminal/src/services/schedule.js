/**
 * schedule.js
 * Trading session schedule for the sniper bot.
 * Reads schedule from .env via config (SNIPER_SCHEDULE_*).
 *
 * All times are in UTC+8 and converted to UTC internally.
 * Assets outside their session window are skipped by the sniper detector.
 *
 * .env format per asset:
 *   SNIPER_SCHEDULE_BTC=19:40-22:40,03:40-06:10
 *   SNIPER_SCHEDULE_ETH=11:40-15:40,16:40-19:40
 */

import config from '../config/index.js';
import logger from '../utils/logger.js';

// UTC+8 offset in hours
const UTC8_OFFSET = 8;

/**
 * Parse schedule string from .env.
 * Format: "HH:MM-HH:MM,HH:MM-HH:MM"
 * Returns: [{ startUtc8, endUtc8, startMin, endMin }]
 */
function parseScheduleString(str) {
    if (!str || !str.trim()) return null;

    const sessions = [];
    const parts = str.split(',').map((s) => s.trim()).filter(Boolean);

    for (const part of parts) {
        const match = part.match(/^(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})$/);
        if (!match) {
            logger.warn(`SCHEDULE: invalid session format "${part}" — expected HH:MM-HH:MM`);
            continue;
        }
        const [, startUtc8, endUtc8] = match;
        sessions.push({
            startUtc8,
            endUtc8,
            startMin: utc8ToUtcMinutes(startUtc8),
            endMin: utc8ToUtcMinutes(endUtc8),
        });
    }

    return sessions.length > 0 ? sessions : null;
}

/**
 * Convert HH:MM in UTC+8 to minutes-since-midnight in UTC.
 * Result is always in [0, 1440).
 */
function utc8ToUtcMinutes(hhmm) {
    const [h, m] = hhmm.split(':').map(Number);
    let totalMin = (h * 60 + m) - (UTC8_OFFSET * 60);
    if (totalMin < 0) totalMin += 1440;
    if (totalMin >= 1440) totalMin -= 1440;
    return totalMin;
}

// Build schedule from config (reads SNIPER_SCHEDULE_* from .env)
const SCHEDULE = {};
const SCHEDULE_DISPLAY = {};

for (const [asset, raw] of Object.entries(config.sniperSchedule || {})) {
    const sessions = parseScheduleString(raw);
    if (sessions) {
        SCHEDULE[asset] = sessions;
        SCHEDULE_DISPLAY[asset] = sessions.map((s) => ({
            startUtc8: s.startUtc8,
            endUtc8: s.endUtc8,
        }));
    }
}

/**
 * Get current time as minutes since midnight UTC.
 */
function nowMinutesUTC() {
    const d = new Date();
    return d.getUTCHours() * 60 + d.getUTCMinutes();
}

/**
 * Check if `nowMin` falls within range [start, end).
 * Handles overnight wrap (e.g. 22:00–02:00).
 */
function inRange(nowMin, startMin, endMin) {
    if (startMin <= endMin) {
        return nowMin >= startMin && nowMin < endMin;
    } else {
        return nowMin >= startMin || nowMin < endMin;
    }
}

/**
 * Check if an asset is currently within its trading session.
 * Returns true if asset has no schedule (always active).
 */
export function isAssetInSession(asset) {
    const sessions = SCHEDULE[asset.toLowerCase()];
    if (!sessions) return true; // no schedule = always active

    const now = nowMinutesUTC();
    return sessions.some((s) => inRange(now, s.startMin, s.endMin));
}

/**
 * Get human-readable time until the next session opens.
 * Returns string like "2h 15m" or null if currently in session.
 */
export function getNextSessionInfo(asset) {
    const sessions = SCHEDULE[asset.toLowerCase()];
    if (!sessions) return null;

    const now = nowMinutesUTC();
    if (sessions.some((s) => inRange(now, s.startMin, s.endMin))) return null;

    let minWait = Infinity;
    for (const s of sessions) {
        let wait = s.startMin - now;
        if (wait <= 0) wait += 1440;
        if (wait < minWait) minWait = wait;
    }

    if (minWait === Infinity) return null;

    const hours = Math.floor(minWait / 60);
    const mins = minWait % 60;
    if (hours > 0) return `${hours}h ${mins}m`;
    return `${mins}m`;
}

/**
 * Get the schedule for display (UTC+8 strings).
 */
export function getSchedule() {
    return SCHEDULE_DISPLAY;
}
