import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(__dirname, '..', '..', 'data');

// Ensure data directory exists
if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
}

/**
 * Read JSON state file
 * @param {string} filename - File name (e.g., "positions.json")
 * @param {*} defaultValue - Default value if file doesn't exist
 * @returns {*} Parsed JSON data
 */
export function readState(filename, defaultValue = {}) {
    const filePath = path.join(DATA_DIR, filename);
    try {
        if (fs.existsSync(filePath)) {
            const data = fs.readFileSync(filePath, 'utf-8');
            return JSON.parse(data);
        }
    } catch (err) {
        console.error(`Error reading state file ${filename}:`, err.message);
    }
    return defaultValue;
}

/**
 * Write JSON state file (atomic write via temp file)
 * @param {string} filename - File name (e.g., "positions.json")
 * @param {*} data - Data to write
 */
export function writeState(filename, data) {
    const filePath = path.join(DATA_DIR, filename);
    const tempPath = filePath + '.tmp';
    try {
        fs.writeFileSync(tempPath, JSON.stringify(data, null, 2), 'utf-8');
        fs.renameSync(tempPath, filePath);
    } catch (err) {
        console.error(`Error writing state file ${filename}:`, err.message);
        // Clean up temp file if rename failed
        try { fs.unlinkSync(tempPath); } catch (_) { }
    }
}
