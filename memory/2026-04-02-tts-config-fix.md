# Session: 2026-04-02 - TTS Config Fix & Setup

## What happened
- Config file got corrupted during TTS setup
- Fixed config manually — restored clean openclaw.json
- TTS configured to use Microsoft Edge TTS (en-US-AndrewNeural)
- Gateway restarted successfully to load new TTS settings

## Alexa Integration (set up this day)
- Alexa skill located at: `~/.openclaw/workspace/alexa_taco_skill/` (bridge + lambda + skill-package)
- Alexa remote control: `~/.openclaw/workspace/alexa-remote-control/` (announcement scripts)
- Alexa API endpoint: `https://alexa.v-carte.pro/alexa/query` (POST only)
- Skill is minimal — responds to trading queries only (report, engine status, BTC, ETH)
- Custom announcement support not yet implemented
- Auth stored in `~/.openclaw/workspace/.alexa_auth.json` and `~/.openclaw/workspace/alexa-remote-control/alexa_token.json`

## TTS voices configured
- English: en-US-AndrewNeural (Microsoft Edge TTS)
- Arabic: ar-SA-HamedNeural
- French: fr-FR-HenriNeural
- STT: faster-whisper (local, model: tiny)
