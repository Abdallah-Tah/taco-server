# TTS & Response Mode Rules — Formal Policy

## Hard Execution Constraints

These are **not preferences**. They are execution constraints that override all defaults, model suggestions, and convenience.

---

### 1. Response Mode Matrix

| Inbound Type | TTS Setting | Output | TTS Tool |
|---|---|---|---|
| Text | ON | Text only | **FORBIDDEN** |
| Text | OFF | Text only | **FORBIDDEN** |
| Voice | ON | Voice only | **ALLOWED** |
| Voice | OFF | Text only (explain TTS off) | N/A (disabled) |
| Explicit "reply by voice" | ON | Voice only | **ALLOWED** |
| Explicit "reply by voice" | OFF | Text only (explain TTS off) | N/A (disabled) |

### 2. Text Input Rules

- Output text only. Period.
- Never call the `tts` tool for text input.
- Never produce both text and voice in the same turn unless explicitly requested.
- Never request, suggest, trigger, or attach audio for text input.
- If a text reply is being prepared, `tts` is prohibited.
- If uncertain, text only.

### 3. Voice Input Rules

- Output voice only (when TTS is ON).
- Match reply voice to input language:
  - Arabic voice note → ar-SA-HamedNeural
  - English voice note → en-US-DavisNeural
  - French voice note → fr-FR-HenriNeural
- If TTS is OFF, reply with text and note TTS is disabled.

### 4. Tool Enforcement

- The `tts` tool is **disallowed** unless:
  - The current inbound message is a voice message, OR
  - The user explicitly requests a voice reply.
- Availability of TTS does **not** grant permission to use it.
- Any default, automatic, or model-suggested TTS behavior must be **ignored** unless explicitly allowed by the current message.

### 5. TTS System Command (user-facing)

| Command | Action |
|---|---|
| `/tts on` | Enable TTS capability system-wide |
| `/tts off` | Disable TTS capability system-wide |
| `/tts status` | Show current TTS settings |
| `/tts provider` | Show/set voice provider |
| `/tts limit` | Set max characters for TTS |
| `/tts summary` | Toggle AI summary for long texts |
| `/tts audio` | Generate TTS from custom text |
| `/tts help` | Show usage guide |

The `/tts` command controls **capability**. These rules control **permission**. Both must align for voice output to occur.

---

### 6. Voices

- English: en-US-DavisNeural (edge-tts)
- Arabic: ar-SA-HamedNeural
- French: fr-FR-HenriNeural

---

*Last updated: 2026-04-02*
*Authorized by: Master (Abdallah)*
