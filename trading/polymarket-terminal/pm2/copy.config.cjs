/**
 * PM2 config — Copy Trade Bot
 *
 * Usage (from project root):
 *   pm2 start pm2/copy.config.cjs            # live trading
 *   pm2 start pm2/copy.config.cjs --env sim  # simulation / dry-run
 *
 *   pm2 logs polymarket-copy
 *   pm2 restart polymarket-copy
 *   pm2 stop polymarket-copy
 *   pm2 delete polymarket-copy
 */
const path = require('path');
const root  = path.join(__dirname, '..');

module.exports = {
    apps: [
        {
            name:        'polymarket-copy',
            script:      path.join(root, 'src/bot.js'),
            interpreter: 'node',

            // Live trading (default)
            env: {
                NODE_ENV: 'production',
                DRY_RUN:  'false',
            },

            // Simulation: pm2 start pm2/copy.config.cjs --env sim
            env_sim: {
                NODE_ENV: 'production',
                DRY_RUN:  'true',
            },

            out_file:        path.join(root, 'logs/copy-out.log'),
            error_file:      path.join(root, 'logs/copy-error.log'),
            log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
            merge_logs:      true,

            restart_delay:      5000,
            max_restarts:       10,
            min_uptime:         '10s',
            max_memory_restart: '256M',
            stop_exit_codes:    [0],
        },
    ],
};
