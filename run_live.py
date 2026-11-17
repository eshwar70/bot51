#!/usr/bin/env python3
"""One-click Live Practice Runner for Winner of 51.

Usage:
  python3 run_live.py
  (Prompts for Upstox access token if UPSTOX_ACCESS_TOKEN is not in environment)
"""
from pathlib import Path
import subprocess
import sys

DIR_PATH = Path(__file__).resolve().parent

if __name__ == "__main__":
    bot_script = DIR_PATH / "winner_51_bot.py"
    config_file = DIR_PATH / "config_fast_practice_50k.json"
    session_folder = DIR_PATH / "live_session_run"
    cmd = [
        sys.executable,
        str(bot_script),
        "run",
        "--config",
        str(config_file),
        "--folder",
        str(session_folder),
        "--wallet",
        "50000",
    ]
    sys.exit(subprocess.call(cmd))
