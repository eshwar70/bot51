#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

echo "=================================================================="
echo " 🏆 WINNER OF 51 — CHENNAI_WIDERUNNER_32 [g]"
echo " Initial Wallet : Rs 50,000.00 (Practice Mode)"
echo " Stop Control   : Touch live_session_run/STOP to exit"
echo "=================================================================="

exec python3 run_live.py
