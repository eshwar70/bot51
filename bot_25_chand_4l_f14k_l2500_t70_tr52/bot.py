#!/usr/bin/env python3
"""Rung 25 — Chandelier 4L F14k L2500 T70% (bot_25_chand_4l_f14k_l2500_t70_tr52) — Standalone Live Upstox Paper Trading Bot.

Rung #25 in Tournament of 25.
Qualification Benchmark (Sep 1 - Nov 18, 2026 | 54 NSE Sessions | Rs 50,000 Starting Wallet):
  - Net Profit    : Rs +103,813.88 (+207.63%)
  - Step Gap      : Rs +3,613.91 above previous competitor (Requirement: >= +Rs 3,000.00)
  - Trades / WR   : 93 trades (62W / 31L, 66.7%)
  - Profit Factor : 2.238 | Max Drawdown: Rs 19,395.31
  - Active Days   : 51/54 (1.72 trades/day)

Usage:
  UPSTOX_ACCESS_TOKEN="<token>" python3 bot_25_chand_4l_f14k_l2500_t70_tr52.py run --wallet 50000
  python3 bot_25_chand_4l_f14k_l2500_t70_tr52.py status
  python3 bot_25_chand_4l_f14k_l2500_t70_tr52.py replay --days 5
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot_runtime import run_bot_cli

BOT_ID = "bot_25_chand_4l_f14k_l2500_t70_tr52"
BOT_NAME = "Rung 25 — Chandelier 4L F14k L2500 T70%"
FAMILY = "Chandelier Compounding 4L"
DESCRIPTION = "Chandelier Compounding 4L with floor Rs 14000, lock Rs 2500, target 70%, trail 5.2%."

CONFIG = json.loads('''{
    "initial_wallet": 50000.0,
    "capital_floor": 14000.0,
    "safety_buffer": 2000.0,
    "planned_risk": 3000.0,
    "daily_loss_limit": 4000.0,
    "daily_profit_lock": 2500.0,
    "dynamic_risk_scaling": true,
    "max_lots": 4,
    "loss_streak_single_lot": true,
    "loss_streak_lot_cap": 1,
    "target_delta": 0.6,
    "delta_min": 0.4,
    "delta_max": 0.68,
    "max_trades_day": 4,
    "max_loss_streak": 2,
    "stop_fraction": 0.1,
    "target_fraction": 0.7,
    "breakeven_activation_fraction": 0.065,
    "breakeven_lock_fraction": 0.028,
    "trail_activation_fraction": 0.13,
    "trail_fraction": 0.052,
    "step_trail_levels": [
        [
            0.085,
            0.038
        ],
        [
            0.15,
            0.095
        ],
        [
            0.23,
            0.165
        ],
        [
            0.32,
            0.25
        ],
        [
            0.42,
            0.34
        ]
    ],
    "entry_start_clock": "09:20",
    "entry_end_clock": "14:00",
    "force_exit_clock": "15:15",
    "cooldown_minutes": 5,
    "direction_cooldown_minutes": 15,
    "max_hold_minutes": 65,
    "max_premium_loss_fraction": 0.48,
    "vix_min": 14.0,
    "vix_max": 35.0,
    "early_stall_exit": false,
    "early_stall_max_vix": 14.0,
    "stall_minutes": 11.0,
    "low_vix_regime": {
        "enabled": true,
        "vix_min": 10.0,
        "max_lots": 1,
        "target_delta": 0.65,
        "delta_max": 0.76,
        "entry_end_clock": "13:30",
        "max_trades_day": 1,
        "max_hold_minutes": 35,
        "stop_fraction": 0.075,
        "target_fraction": 0.145,
        "breakeven_activation_fraction": 0.045,
        "breakeven_lock_fraction": 0.025,
        "trail_activation_fraction": 0.065,
        "trail_fraction": 0.028,
        "max_premium_loss_fraction": 0.45,
        "early_stall_exit": true,
        "stall_minutes": 11.0,
        "step_trail_levels": [
            [
                0.085,
                0.035
            ],
            [
                0.15,
                0.09
            ],
            [
                0.22,
                0.15
            ],
            [
                0.28,
                0.215
            ]
        ]
    }
}''')

def evaluate_signal(feat: dict, state: dict, cfg: dict):
    if feat["bars_5m_count"] < 10:
        return 0, "NONE", None
    p = feat["price"]
    br = feat["delta_breadth_dir"] if feat["delta_breadth_dir"] else feat["breadth_dir"]
    if feat["st5m_dir"] == 1 and feat["trend_5m"] == 1 and feat["eff_5m"] >= 0.25 and feat["esep_5m"] >= 0.35:
        if p > feat["e9"] > feat["e21"] and (p > feat["don10_hi"] or feat["roc3"] > 0.0005) and br != -1 and feat["rsi14"] <= 76.0:
            return 1, "CHAND_L", None
    if feat["st5m_dir"] == -1 and feat["trend_5m"] == -1 and feat["eff_5m"] >= 0.25 and feat["esep_5m"] >= 0.35:
        if p < feat["e9"] < feat["e21"] and (p < feat["don10_lo"] or feat["roc3"] < -0.0005) and br != 1 and feat["rsi14"] >= 24.0:
            return -1, "CHAND_S", None
    return 0, "NONE", None


def evaluate_exit(pos: dict, option: dict, feat: dict, cfg: dict):
    from bot_runtime import stamp
    bid = float(option["bid"])
    entry = float(pos["entry"])
    if entry <= 0:
        return None
    ret = (bid - entry) / entry
    held = (feat["now"] - stamp(pos["entered"])).total_seconds() / 60.0
    pos_dir = 1 if pos["kind"] == "CE" else -1
    if held >= 12.0 and ret <= -0.055 and feat["trend_5m"] == -pos_dir and feat["st5m_dir"] == -pos_dir:
        return "HTF_REVERSAL_GUARD"
    return None


if __name__ == "__main__":
    run_bot_cli(BOT_ID, BOT_NAME, DESCRIPTION, CONFIG, evaluate_signal, evaluate_exit)
