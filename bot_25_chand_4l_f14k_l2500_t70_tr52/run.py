#!/usr/bin/env python3
"""Champion Trading Bot CLI Runner.

Self-contained executable for running the tournament champion bot in paper trading,
live Upstox execution, status inspection, or historical replay.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot
from bot_runtime import run_bot_cli

def main():
    parser = argparse.ArgumentParser(description="Champion Trading Bot CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Run command
    run_p = subparsers.add_parser("run", help="Start trading session")
    run_p.add_argument("--wallet", type=float, default=50000.0, help="Initial wallet amount (default: 50000)")
    run_p.add_argument("--poll-interval", type=float, default=2.0, help="Quote polling interval in seconds")

    # Status command
    stat_p = subparsers.add_parser("status", help="Show current state, wallet, and open positions")

    # Replay command
    rep_p = subparsers.add_parser("replay", help="Replay recent trading sessions")
    rep_p.add_argument("--days", type=int, default=5, help="Number of past sessions to replay")

    args = parser.parse_args()

    # Pass through to runtime
    sys.argv = [sys.argv[0]] + (sys.argv[1:] if len(sys.argv) > 1 else ["status"])
    run_bot_cli(bot.BOT_ID, bot.BOT_NAME, bot.DESCRIPTION, bot.CONFIG, bot.evaluate_signal, bot.evaluate_exit)

if __name__ == "__main__":
    main()
