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

- Main English voice for Taco: the current OpenClaw/TTS English voice used in the intro sample approved on 2026-03-25
- Arabic voice previously preferred for Taco: **Hamed**
- Voice interaction preference: if user sends voice, reply with voice in the same language when possible

### Wallets

- Polymarket wallet: `0x1a4c163a134D7154ebD5f7359919F9c439424f00`
- Solana wallet: `J6nK35ud8u6hzqDxuEtVWPsAMzv2v7H6stsxXW2rsnuH`

Add whatever helps you do your job. This is your cheat sheet.

### Ollama
- Provider host: https://ollama.com
- Default model: qwen3.5:cloud
- Note: cloud models require OLLAMA_API_KEY set in ~/.config/openclaw/secrets.env
