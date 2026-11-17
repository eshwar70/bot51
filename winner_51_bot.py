#!/usr/bin/env python3
"""Winner of 51 (A19 [g] — Chennai_WideRunner_32 [g]) Standalone Practice Trading Bot.

PRACTICE MODE ONLY (Paper trading on real Upstox market data — Zero real money / No live broker orders).
"""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, date, timedelta
import getpass
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import zoneinfo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ANSI Color Codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"

BG_GREEN = "\033[42;30m"
BG_RED = "\033[41;37m"
BG_YELLOW = "\033[43;30m"
BG_BLUE = "\033[44;37m"
BG_CYAN = "\033[46;30m"

DIR_PATH = Path(__file__).resolve().parent
MEMBERSHIP_PATH = DIR_PATH / "nifty_membership.json"

# Import local modules
from core import Blocked, atomic, emit, stamp
from fast_practice import FastUpstox, QuickPracticeEngine, load_fast_config

running = True


def handle_sigint(signum, frame):
    global running
    print(f"\n{YELLOW}⚠️ Signal {signum} received. Stopping cleanly...{RESET}", flush=True)
    running = False


signal.signal(signal.SIGINT, handle_sigint)
signal.signal(signal.SIGTERM, handle_sigint)


def ensure_token() -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        if not sys.stdin.isatty():
            raise Blocked("UPSTOX_ACCESS_TOKEN environment variable is missing.")
        token = getpass.getpass("Enter Upstox access token (hidden): ").strip()
        if not token:
            raise Blocked("No access token provided.")
        os.environ["UPSTOX_ACCESS_TOKEN"] = token
    return token


def print_banner(c: dict, folder: Path):
    print(f"{BOLD}{BG_CYAN}                                                                                {RESET}")
    print(f"{BOLD}{BG_CYAN}  🏆 WINNER OF 51 — CHENNAI_WIDERUNNER_32 [g] (PRACTICE MODE)                  {RESET}")
    print(f"{BOLD}{BG_CYAN}                                                                                {RESET}")
    print(f"Strategy: {CYAN}{BOLD}Chennai_WideRunner_32 [g]{RESET} (A19 [g] Champion)")
    print(f"Initial Wallet: {GREEN}₹{c['initial_wallet']:,.2f}{RESET} | Capital Floor: {RED}₹{c['capital_floor']:,.2f}{RESET}")
    print(f"Max Trades/Day: {c.get('max_trades_day', 4)} | Max Lots: {c.get('max_lots', 2)} | Poll Cadence: {c.get('poll_seconds', 2.5)}s")
    print(f"Output Directory: {folder}")
    print(f"Stop Control: Touch {folder / 'STOP'} or Ctrl+C to terminate cleanly\n", flush=True)


def run_live(c: dict, folder: Path):
    global running
    ensure_token()
    folder.mkdir(parents=True, exist_ok=True)
    atomic(folder / "effective_config.json", c)

    print_banner(c, folder)

    if not MEMBERSHIP_PATH.exists():
        # Fall back to root membership if needed
        root_mem = DIR_PATH.parent / "nifty_membership.json"
        if root_mem.exists():
            import shutil
            shutil.copyfile(root_mem, MEMBERSHIP_PATH)

    api = FastUpstox(MEMBERSHIP_PATH)
    engine = QuickPracticeEngine(c, folder)

    # Pre-market wait loop
    print(f"{YELLOW}⏳ Checking NSE market status...{RESET}", flush=True)
    while running:
        if (folder / "STOP").exists():
            print(f"{YELLOW}STOP file detected. Exiting.{RESET}")
            return
        now_dt = datetime.now(IST)
        try:
            st = api.get("/v2/market/status/NSE").get("status")
            if st == "NORMAL_OPEN":
                print(f"\n{BG_GREEN}{BOLD} 🟢 NSE MARKET IS NORMAL_OPEN! ({now_dt.strftime('%H:%M:%S')} IST) — STARTING PRACTICE TRADING! {RESET}\n", flush=True)
                break
            else:
                if now_dt.second % 15 == 0:
                    print(f"[{now_dt.strftime('%H:%M:%S')} IST] ⏳ Market Status: {YELLOW}{st}{RESET} | Awaiting NORMAL_OPEN (09:15:00 IST)...", flush=True)
        except Exception as e:
            print(f"[{now_dt.strftime('%H:%M:%S')} IST] ⚠️ Market check retry: {e}", flush=True)
        time.sleep(4.0)

    if not running:
        return

    poll_sec = float(c.get("poll_seconds", 2.5))
    prev_pos = copy.deepcopy(engine.s.get("position"))
    prev_trades_len = len(engine.s.get("trades", []))
    prev_stop = prev_pos.get("stop") if prev_pos else None
    last_scoreboard_time = 0.0
    last_heartbeat_time = 0.0

    print(f"{GREEN}✓ Live market stream active. Evaluation running every {poll_sec}s...{RESET}\n", flush=True)

    while running:
        if (folder / "STOP").exists() and not engine.s.get("position"):
            print(f"\n{YELLOW}STOP file detected and no open position. Clean shutdown.{RESET}")
            break

        now_dt = datetime.now(IST)
        now_str = now_dt.strftime("%H:%M:%S")

        if now_str >= "15:30:00":
            print(f"\n{BG_RED}{BOLD} 🏁 15:30:00 IST REACHED — MARKET CLOSED. FINALIZING SESSION. {RESET}\n")
            break

        pinned = {engine.s["position"]["key"]} if engine.s.get("position") else None

        try:
            snap = api.snapshot(pinned_keys=pinned)
            now_stamp = stamp(snap["timestamp"])
            res = engine.step(snap, now_stamp)
            emit(folder / "snapshots.jsonl", snap)
        except Exception as exc:
            msg = str(exc)
            if any(k in msg for k in ("Data collection too slow", "Stale or future snapshot", "HTTP 429", "HTTP 5", "ConnectionResetError", "timed out")):
                api._status_cache = None
                api._bars_cache = None
                api._chain_cache = None
                time.sleep(10.0 if "429" in msg else 3.0)
                continue
            raise

        curr_pos = engine.s.get("position")
        curr_trades_len = len(engine.s.get("trades", []))

        # ENTRY EVENT
        if curr_pos and not prev_pos:
            dec = res.get("last_decision") or {}
            print(f"\n{BG_GREEN}{BOLD} 🟢 [TRADE ENTRY] WINNER OF 51 {RESET}", flush=True)
            print(f"{CYAN}{BOLD}   Time: {now_str} IST | Strike: {curr_pos['strike']:.0f} {curr_pos['kind']} | Lots: {curr_pos['quantity']//c.get('lot_size', 65)} ({curr_pos['quantity']} qty)")
            print(f"   Entry: ₹{curr_pos['entry']:.2f} | Outlay: ₹{curr_pos['quantity']*curr_pos['entry']:,.2f}")
            print(f"   Initial Stop: ₹{curr_pos['stop']:.2f} (-{c.get('stop_fraction', 0.10)*100:.1f}%) | Target: ₹{curr_pos['target']:.2f} (+{c.get('target_fraction', 0.32)*100:.1f}%)")
            print(f"   Setup Trigger: {dec.get('setup_type')}{RESET}\n", flush=True)

        # EXIT EVENT
        if curr_trades_len > prev_trades_len:
            last_t = engine.s["trades"][-1]
            pnl = last_t["net_pnl"]
            if pnl >= 0:
                print(f"\n{BG_GREEN}{BOLD} 🎯🏆 [PROFIT EXIT / TARGET HIT] {RESET}", flush=True)
                print(f"{GREEN}{BOLD}   Exit Time: {last_t['exited'][11:19]} IST | Strike: {last_t['key']}")
                print(f"   Entry: ₹{last_t['entry']:.2f} ──> Exit: ₹{last_t['exit']:.2f} (Qty: {last_t['quantity']})")
                print(f"   Gross P&L: ₹{last_t['quantity']*(last_t['exit']-last_t['entry']):+,.2f} | Fees: ₹{last_t['charges']:.2f} | NET: ₹{pnl:+,.2f}")
                print(f"   Reason: {last_t['reason']} | Updated Wallet: ₹{engine.s['equity']:,.2f}{RESET}\n", flush=True)
            else:
                print(f"\n{BG_RED}{BOLD} 🛑⚠️ [STOP LOSS EXIT] {RESET}", flush=True)
                print(f"{RED}{BOLD}   Exit Time: {last_t['exited'][11:19]} IST | Strike: {last_t['key']}")
                print(f"   Entry: ₹{last_t['entry']:.2f} ──> Exit: ₹{last_t['exit']:.2f} (Qty: {last_t['quantity']})")
                print(f"   Net Loss: ₹{pnl:+,.2f} (Fees: ₹{last_t['charges']:.2f})")
                print(f"   Reason: {last_t['reason']} | Updated Wallet: ₹{engine.s['equity']:,.2f}{RESET}\n", flush=True)

        # TRAIL RATCHET EVENT
        if curr_pos and prev_pos and prev_stop is not None and curr_pos["stop"] > prev_stop:
            print(f"{CYAN}⚡ [TRAIL RATCHET] Stop Loss raised from ₹{prev_stop:.2f} ──> ₹{curr_pos['stop']:.2f}{RESET}", flush=True)

        # ACTIVE POSITION TICK
        if curr_pos:
            opt_q = next((o for o in snap.get("options", []) if o.get("key") == curr_pos.get("key")), None)
            live_bid = (opt_q.get("bid") if opt_q else None) or curr_pos["entry"]
            mtm_v = (live_bid - curr_pos["entry"]) * curr_pos["quantity"]
            mtm_p = ((live_bid / curr_pos["entry"]) - 1.0) * 100.0 if curr_pos["entry"] > 0 else 0.0
            c_pnl = GREEN if mtm_v >= 0 else RED
            s_pnl = "▲" if mtm_v >= 0 else "▼"
            print(
                f"{BG_YELLOW}{BOLD} 🟡 ACTIVE TRADE {RESET} {curr_pos['strike']:.0f} {curr_pos['kind']} ({curr_pos['quantity']} qty) | "
                f"Entry: ₹{curr_pos['entry']:.2f} | Bid: ₹{live_bid:.2f} | "
                f"MTM: {c_pnl}{BOLD}₹{mtm_v:+,.2f} ({s_pnl}{mtm_p:+.2f}%){RESET} | "
                f"SL: ₹{curr_pos['stop']:.2f} | TP: ₹{curr_pos['target']:.2f}",
                flush=True,
            )

        now_mono = time.monotonic()
        if now_mono - last_scoreboard_time >= 30.0:
            spot_val = snap.get("spot_live", {}).get("ltp") or snap["bars"][-1]["close"]
            vix_val = snap.get("vix", {}).get("value") or 0.0
            pnl_tot = engine.s["equity"] - c["initial_wallet"]
            pnl_c = GREEN if pnl_tot >= 0 else RED
            t_list = engine.s.get("trades", [])
            w_c = sum(1 for t in t_list if t["net_pnl"] > 0)
            l_c = sum(1 for t in t_list if t["net_pnl"] < 0)
            print(
                f"{BOLD}──────────────────────────────────────────────────────────────────────────────────{RESET}\n"
                f" ⏰ {now_str} IST | 📈 NIFTY: {spot_val:,.2f} | ⚡ India VIX: {vix_val:.2f} | "
                f"Wallet: ₹{engine.s['cash']:,.2f} | Equity: ₹{engine.s['equity']:,.2f} | Day P&L: {pnl_c}{BOLD}₹{pnl_tot:+,.2f} ({pnl_tot/c['initial_wallet']*100:+.2f}%){RESET} | "
                f"Trades: {len(t_list)} ({w_c}W / {l_c}L)\n"
                f"{BOLD}──────────────────────────────────────────────────────────────────────────────────{RESET}",
                flush=True,
            )
            last_scoreboard_time = now_mono
            last_heartbeat_time = now_mono
        elif not curr_pos and now_mono - last_heartbeat_time >= 10.0:
            spot_val = snap.get("spot_live", {}).get("ltp") or snap["bars"][-1]["close"]
            vix_val = snap.get("vix", {}).get("value") or 0.0
            print(f"{DIM}[{now_str} IST] ⚪ Scanning market... NIFTY: {spot_val:,.2f} | VIX: {vix_val:.2f} | Equity: ₹{engine.s['equity']:,.2f}{RESET}", flush=True)
            last_heartbeat_time = now_mono

        prev_pos = copy.deepcopy(curr_pos)
        prev_trades_len = curr_trades_len
        prev_stop = curr_pos["stop"] if curr_pos else None

        engine.reports()
        time.sleep(poll_sec)

    rep = engine.reports()
    pnl = engine.s["equity"] - c["initial_wallet"]
    print(f"\n{BOLD}{BG_CYAN}══════════════════════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{BG_CYAN}  🏁 FINAL AUDIT SUMMARY — WINNER OF 51                                           {RESET}")
    print(f"{BOLD}{BG_CYAN}══════════════════════════════════════════════════════════════════════════════════{RESET}")
    print(f"Ending Equity: ₹{engine.s['equity']:,.2f} | Net P&L: ₹{pnl:+,.2f} ({(pnl/c['initial_wallet'])*100:+.2f}%)")
    trades = engine.s.get("trades", [])
    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    losses = sum(1 for t in trades if t["net_pnl"] < 0)
    print(f"Total Trades: {len(trades)} ({wins}W / {losses}L, Win Rate: {(wins/len(trades)*100 if trades else 0):.1f}%) | Max DD: ₹{engine.s.get('max_drawdown', 0.0):,.2f}")
    print(f"Audit files saved in: {folder}\n", flush=True)


def run_replay(c: dict, folder: Path, input_path: str):
    folder.mkdir(parents=True, exist_ok=True)
    atomic(folder / "effective_config.json", c)
    engine = QuickPracticeEngine(c, folder)
    print_banner(c, folder)
    print(f"Replaying snapshots from: {input_path}...")
    count = 0
    with Path(input_path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            snap = json.loads(line)
            now_dt = stamp(snap["timestamp"])
            engine.step(snap, now_dt)
            count += 1
    rep = engine.reports()
    pnl = engine.s["equity"] - c["initial_wallet"]
    print(f"✓ Replayed {count} snapshots.")
    print(f"Ending Equity: ₹{engine.s['equity']:,.2f} | Net P&L: ₹{pnl:+,.2f} ({(pnl/c['initial_wallet'])*100:+.2f}%)")
    trades = engine.s.get("trades", [])
    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    losses = sum(1 for t in trades if t["net_pnl"] < 0)
    print(f"Trades: {len(trades)} ({wins}W / {losses}L) | Max DD: ₹{engine.s.get('max_drawdown', 0.0):,.2f}")


def show_status(c: dict):
    print(json.dumps(
        {
            "bot_name": "Winner of 51 (A19 [g] — Chennai_WideRunner_32 [g])",
            "mode": "PRACTICE_ONLY",
            "initial_wallet": c["initial_wallet"],
            "capital_floor": c["capital_floor"],
            "buffered_entry_halt": c["capital_floor"] + c["safety_buffer"],
            "max_trades_day": c.get("max_trades_day", 4),
            "max_lots": c.get("max_lots", 2),
            "setups": [
                "1M_MOMENTUM (Fast momentum break)",
                "FAST_TICK_BREAKOUT (Sub-minute spot tick thrust)",
                "MORNING_TWAP_BRK (09:42-11:50 morning expansion)",
                "G_FLAG_BRK (Consolidation flag breakout)",
            ],
            "step_trailing_stop_levels": c.get("step_trail_levels"),
            "regimes": {
                "mid_high_vix": "Max 4 trades/day, Target Delta 0.60, Max 2 lots, Step trailing ladder",
                "low_vix (<14.0)": "Max 1 trade/day, Target Delta 0.65, Max 1 lot, 35m max hold",
            },
            "status": "VALIDATED_CHAMPION_OF_51",
        },
        indent=2,
    ))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["run", "replay", "status"], default="run", nargs="?")
    p.add_argument("--config", default="config_fast_practice_50k.json")
    p.add_argument("--wallet", type=float, default=None)
    p.add_argument("--folder", default="run_session")
    p.add_argument("--input", default=None)
    a = p.parse_args()

    cfg_file = DIR_PATH / a.config if not Path(a.config).is_absolute() else Path(a.config)
    c = load_fast_config(cfg_file)
    if a.wallet is not None:
        c["initial_wallet"] = float(a.wallet)

    if a.command == "status":
        show_status(c)
        return

    folder = Path(a.folder) if Path(a.folder).is_absolute() else DIR_PATH / a.folder

    if a.command == "replay":
        if not a.input:
            raise Blocked("Replay requires --input snapshots.jsonl")
        run_replay(c, folder, a.input)
        return

    run_live(c, folder)


if __name__ == "__main__":
    main()
