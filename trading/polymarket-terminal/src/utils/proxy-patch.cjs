/**
 * proxy-patch.cjs
 *
 * Patches the Node.js https module to route Polymarket traffic through a proxy.
 * This needs to be the VERY FIRST import in the application.
 */

const PROXY_URL = process.env.PROXY_URL || '';

if (PROXY_URL) {
    const https = require('https');
    const { HttpsProxyAgent } = require('https-proxy-agent');

    const agent = new HttpsProxyAgent(PROXY_URL);

    const POLY_DOMAINS = [
        'polymarket.com',
        'clob.polymarket.com',
        'gamma-api.polymarket.com',
        'data-api.polymarket.com',
    ];

    const shouldProxy = (hostname) => {
        if (!hostname) return false;
        return POLY_DOMAINS.some(d => hostname === d || hostname.endsWith('.' + d));
    };

    const originalRequest = https.request;

    https.request = function(...args) {
        let url;
        if (typeof args[0] === 'string') {
            url = args[0];
        } else if (args[0] && args[0].hostname) {
            url = args[0].hostname;
        }

        if (url && shouldProxy(url)) {
            if (typeof args[0] === 'object') {
                args[0].agent = agent;
            } else if (typeof args[0] === 'string') {
                // Axios might pass string, convert to options object
                const parsed = new URL(args[0]);
                args[0] = {
                    protocol: parsed.protocol,
                    hostname: parsed.hostname,
                    port: parsed.port,
                    path: parsed.pathname + parsed.search,
                    agent: agent,
                };
            }
        }

        return originalRequest.apply(this, args);
    };

    console.log('[proxy-patch] HTTPS patched for Polymarket');
}
