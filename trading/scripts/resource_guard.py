#!/usr/bin/env python3
"""
resource_guard.py — CPU/Memory guard for Ollama inference
Trading engines ALWAYS have priority. Only 1 Ollama inference at a time.
"""
import os
import threading
import time
from pathlib import Path

import psutil

CPU_THRESHOLD   = 70.0   # percent — block Ollama above this
MEM_THRESHOLD  = 80.0   # percent — block Ollama above this
POLL_INTERVAL  = 10     # seconds between system checks

_lock      = threading.Lock()
_inference  = False
_paused    = False
_callbacks  = []

_process_blacklist = [
    "polymarket_btc15m",
    "polymarket_eth15m",
    "polymarket_news_arb",
    "polymarket_whale_tracker",
    "taco_trader",
    "sniper.py",
    "journal.py",
    "portfolio.py",
    "polymarket_executor.py",
]


def _get_system_stats():
    """Return (cpu_percent, mem_percent, mem_available_gb)"""
    cpu  = psutil.cpu_percent(interval=1.0)
    mem  = psutil.virtual_memory()
    mem_avail = mem.available / (1024**3)
    return cpu, mem.percent, mem_avail


def _is_trading_active():
    """Check if any trading engine process is running"""
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = proc.info.get("name", "")
                cmd  = " ".join(proc.info.get("cmdline") or [])
                for blocked in _process_blacklist:
                    if blocked in cmd or blocked in name:
                        # Make sure it's actually running (not a zombie)
                        if proc.is_running() and not proc.status() == psutil.STATUS_ZOMBIE:
                            return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return False


def check(resource="cpu"):
    """
    Primary API — call this BEFORE any Ollama inference.
    Returns (allowed: bool, reason: str)
    """
    global _paused

    cpu, mem, _ = _get_system_stats()
    trading_active = _is_trading_active()

    if resource == "cpu":
        if cpu >= CPU_THRESHOLD:
            return False, f"CPU {cpu:.1f}% >= {CPU_THRESHOLD}% threshold"
    elif resource == "memory":
        if mem >= MEM_THRESHOLD:
            return False, f"Memory {mem:.1f}% >= {MEM_THRESHOLD}% threshold"
    elif resource == "all":
        if cpu >= CPU_THRESHOLD:
            return False, f"CPU {cpu:.1f}% >= {CPU_THRESHOLD}%"
        if mem >= MEM_THRESHOLD:
            return False, f"Memory {mem:.1f}% >= {MEM_THRESHOLD}%"

    # Trading always has priority — if trading is running, be conservative
    if trading_active and resource in ("cpu", "all"):
        # Only block if we're already high
        if cpu >= 60.0:
            return False, f"Trading active + CPU {cpu:.1f}% >= 60%"

    return True, "ok"


def acquire_inference_slot():
    """
    Call before starting Ollama inference.
    Blocks if another inference is in progress OR system is overloaded.
    Returns True when slot is acquired.
    """
    global _inference

    while True:
        with _lock:
            if _inference:
                # Another inference in progress — wait
                time.sleep(2)
                continue

            allowed, reason = check("all")
            if not allowed:
                time.sleep(5)
                continue

            _inference = True
            return True


def release_inference_slot():
    """Call after Ollama inference is done."""
    global _inference
    with _lock:
        _inference = False


def pause():
    """Block ALL Ollama inferences regardless of system load."""
    global _paused
    with _lock:
        _paused = True


def resume():
    global _paused
    with _lock:
        _paused = False


def get_status():
    """Return current guard status dict."""
    cpu, mem, mem_avail = _get_system_stats()
    trading = _is_trading_active()
    with _lock:
        return {
            "cpu_percent":    round(cpu, 1),
            "mem_percent":    round(mem, 1),
            "mem_avail_gb":  round(mem_avail, 2),
            "trading_active": trading,
            "inference_lock": _inference,
            "globally_paused": _paused,
            "cpu_threshold":  CPU_THRESHOLD,
            "mem_threshold":  MEM_THRESHOLD,
            "allowed":        check("all")[0],
        }


if __name__ == "__main__":
    import json, sys, pprint
    pprint.pprint(get_status())
