#!/usr/bin/env python3
"""Local STT using faster-whisper (CTranslate2, no API key needed)."""
import sys
from faster_whisper import WhisperModel

model_size = "small"  # much better accuracy, handles Arabic/English/French
model = WhisperModel(model_size, device="cpu", compute_type="int8")

audio_path = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
segments, info = model.transcribe(audio_path, beam_size=5, language=None, task="transcribe")
text = " ".join(seg.text.strip() for seg in segments).strip()
print(text)
