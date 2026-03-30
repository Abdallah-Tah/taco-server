#!/usr/bin/env python3
"""Shared runtime path resolution for merged workspace scripts."""
from __future__ import annotations

from pathlib import Path
import sys


SCRIPT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_ROOT.parent
TRADING_ROOT = WORKSPACE_ROOT / "trading"
SYSTEM_PYTHON = Path("/usr/bin/python3")


def resolve_runtime_python() -> Path:
    """Return the single interpreter path used for subprocess launches."""
    venv_python = WORKSPACE_ROOT / ".polymarket-venv" / "bin" / "python3"
    if venv_python.exists():
        return venv_python
    if SYSTEM_PYTHON.exists():
        return SYSTEM_PYTHON
    current_python = Path(sys.executable) if sys.executable else None
    if current_python and current_python.exists():
        return current_python
    return SYSTEM_PYTHON
