# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

### TTS / Voice

- Main English voice for Taco: **en-US-DavisNeural** (edge-tts)
- Arabic voice: **ar-SA-HamedNeural**
- French voice: **fr-FR-HenriNeural**
- Reply mode rule:
  - User sends text -> reply with text
  - User sends voice -> reply with voice
  - Do not turn text replies into voice unless explicitly asked
- Language rule for voice replies:
  - Detect the language from the transcript
  - Reply in the same language
  - Pick the matching configured voice when available
- STT: faster-whisper (local, free, no API key)
- Model: "tiny" (fast, Pi-friendly)

### Wallets

- Polymarket wallet: `0x1a4c163a134D7154ebD5f7359919F9c439424f00`
- Solana wallet: `J6nK35ud8u6hzqDxuEtVWPsAMzv2v7H6stsxXW2rsnuH`

Add whatever helps you do your job. This is your cheat sheet.

### Ollama
- Provider host: https://ollama.com
- Default model: qwen3.5:cloud
- Note: cloud models require OLLAMA_API_KEY set in ~/.config/openclaw/secrets.env

### /report Command Format
When Abdallah asks for /report, always use **live API data** and this exact template:

```
📊 TRADING REPORT
Generated: {datetime UTC}

💰 Portfolio: ${total} | Free: ${usdc} | Positions: ${open_val}

BTC-15m:
  Resolved: {total} | W:{wins} L:{losses} | PnL: ${pnl}

ETH-15m:
  Resolved: {total} | W:{wins} L:{losses} | PnL: ${pnl}

Today:
  BTC: {n} trades | W:{w} | ${pnl}
  ETH: {n} trades | W:{w} | ${pnl}

Net today: ${net} | 🟢/🟡/🔴 status
```

**Data sources:**
- Wallet (free USDC): `scripts/polymarket_executor.py balance` → divide raw by 1e6
- Open positions (live value): `https://data-api.polymarket.com/positions?user=0x1a4c163a134D7154ebD5f7359919F9c439424f00`
- Trade stats: `~/.openclaw/workspace/trading/journal.db`

**Rules:**
- Default = current day stats
- "from X to Y" = apply date range to Today section
- Always pull live — never use cached numbers

**Additional rule:** Only display engines that are currently running (check `ps aux | grep polymarket_<engine>`). If an engine is not running, omit it from the report entirely.

### Career-Ops / Resume Generator
- Repo: `~/Documents/career-ops/`
- Profile: `config/profile.yml` — Abdallah's career data (single source of truth)
- CV source: `cv.md` — full CV in markdown
- Resume template: `resume_lumion.html` — ATS-optimized, Space Grotesk + DM Sans, blue (#2563eb) accent
- PDF output: Playwright (`npx playwright`) converts HTML → PDF
- How to run the AI advisor: `cd ~/Documents/career-ops && ollama run gemma4:31b-cloud`
- Claude Code CLI installed: `/usr/bin/claude` v2.1.83 (needs ANTHROPIC_API_KEY to use)
- Ollama CLI: `ollama run gemma4:31b-cloud` (free, cloud-based Gemma model)
- When Master says "run Claude" he means: `ollama run gemma4:31b-cloud`
- Workflow: Fetch job URL → evaluate fit → tailor CV → generate HTML → Playwright PDF → send via Telegram
