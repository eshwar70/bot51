# 🏆 Winner of 51: `A19 [g]` (`Chennai_WideRunner_32 [g]`)

The **Undefeated Champion Bot (`A19 [g]` / `Chennai_WideRunner_32 [g]`)** from the 51-bot Master Cross-Tournament (`50-0-0`, `150 / 150` points across all `1,275` pairwise head-to-head matchups).

This directory is completely **self-contained and standalone**. All execution code, strategy logic, market adapters, configurations, and historical/live trading results are included directly within this folder.

---

## 🚀 Quickstart: How to Run

### Prerequisites
- **Python 3.9+** (Pure standard library — zero external `pip` packages required; see `requirements.txt`).
- **Upstox API v2 Access Token** (`UPSTOX_ACCESS_TOKEN` environment variable or enter when prompted; see `.env.example`).

```bash
git clone https://github.com/eshwar70/bot51.git
cd bot51
```

### 1. One-Click Live Practice Trading
Run live paper trading against the live NSE market stream via Upstox API:
```bash
# Easy launcher:
python3 run_live.py

# Or on Mac / Linux:
./Run_Bot.sh

# Or on Windows:
Run_Bot.bat
```
*(Prompts for `UPSTOX_ACCESS_TOKEN` if not set in your environment).*

### 2. Full Bot CLI (`winner_51_bot.py`)
```bash
# Run live paper trading with customized initial wallet:
python3 winner_51_bot.py run --wallet 50000

# View bot strategy parameters, VIX regimes, and trailing stop ladder:
python3 winner_51_bot.py status

# Replay any recorded snapshots:
python3 winner_51_bot.py replay --input results/nov_13_2026_live/snapshots.jsonl
```

### 3. Run Self-Test Suite
```bash
python3 test_bot.py
```

---

## 🤖 Standalone Championship Bots Included

This repository contains both championship trading bots packaged as standalone, self-contained folders with their own runtime, configs, historical results, and CLI runners:

### 1. [bot_25_chand_4l_f14k_l2500_t70_tr52](./bot_25_chand_4l_f14k_l2500_t70_tr52/)
- **Strategy**: Rung 25 — Chandelier 4L F14k L2500 T70% (Grand Master Champion)
- **Benchmark**: Net Profit +₹103,813.88 (+207.63%), 66.7% Win Rate, Profit Factor 2.238.
- **Parameters**: 4-lot dynamic scaling, ₹14,000 risk floor, ₹2,500 profit lock, 70% target, 5.2% Chandelier trailing stop.
- **Run**:
  ```bash
  cd bot_25_chand_4l_f14k_l2500_t70_tr52
  python3 run.py --wallet 50000
  ```

### 2. [Chandelier Runner Tgt50% Lock Rs2200](./Chandelier%20Runner%20Tgt50%25%20Lock%20Rs2200/) (alias: `chandelier_runner_tgt50_lock_rs2200`)
- **Strategy**: Chandelier Runner Tgt50% Lock Rs2200 (bot_01_chandelier_run_t50_l2200)
- **Benchmark**: Net Profit +₹66,851.51 (+133.70%), 51/54 active days (94.4% coverage), Profit Factor 2.179.
- **Parameters**: 5m Supertrend momentum runner, 50% target, ₹2,200 profit lock, 2-lot cap, ₹30,000 risk floor.
- **Run**:
  ```bash
  cd "Chandelier Runner Tgt50% Lock Rs2200"
  python3 run.py --wallet 50000
  ```


---

## 🏛️ Core Strategy Architecture

1. **Dual VIX Regime Engine:**
   - **Low-VIX Regime (`VIX < 14.0`):**
     - Target Delta: `0.65` (Tight ITM sniper)
     - Max trades: `1` trade per day | Max hold: `35` minutes | Stop: `7.5%` | Target: `14.5%`
     - Breakeven lock: `+4.5% -> +2.5%` | Trailing: `+6.5% / 2.8%`
     - Early Stall Guard (`EARLY_STALL_EXIT` cuts stagnant trades after 8–11 minutes).
   - **Normal / High-VIX Regime (`VIX >= 14.0`):**
     - Target Delta: `0.60` (Delta band `0.40 - 0.65`)
     - Max trades: `4` trades per day | Max lots: `2` (50k) / `4` (100k)
     - Initial Stop Loss: `-10.0%` | Profit Target: `+32.0%`
     - Trailing Activation: `+13.0%` with `5.0%` trail distance.

2. **Asymmetric 4-Tier Step-Trailing Ladder:**
   As option premium expands into profit, the stop loss ratchets automatically to lock gains:
   - **Level 1 (`+8.5%` gain):** Stop locks at **`+3.5%`**
   - **Level 2 (`+15.0%` gain):** Stop locks at **`+9.0%`**
   - **Level 3 (`+22.0%` gain):** Stop locks at **`+15.0%`**
   - **Level 4 (`+28.0%` gain):** Stop locks at **`+21.5%`**

3. **Active Setups:**
   - **`1M_MOMENTUM`:** Fast multi-timeframe directional momentum breakout.
   - **`FAST_TICK_BREAKOUT`:** Rapid tick velocity thrust confirming spot volume.
   - **`MORNING_TWAP_BRK`:** Morning TWAP band breakout (`09:42 – 11:50 IST`).
   - **`G_FLAG_BRK`:** Bull/Bear flag continuation breakout (`Jakeschincariol/day-trader-skill`).

4. **Risk Shields:**
   - `lunch_shield_enabled: true` (`12:15 – 13:05 IST` choppy-tape gate).
   - `loss_streak_single_lot: true` (Cuts lot size to 1 lot after any loss).
   - `capital_floor: ₹30,000` + `safety_buffer: ₹2,000` (Hard stop halt).

---

## 📈 Verified Performance Track Record

| Session / Period | Wallet | Trades (`W/L`, `WR%`) | Profit Factor | Net P&L (`Ret%`) | Outcome |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Tournament Period (`Sep 1 – Oct 30, 2026`)** | ₹50,000 | `62` (`41W/21L`, `66.1%`) | **`2.85`** | **`+₹53,479.57 (+106.96%)`** | **Tournament Champion** 🏆 |
| **August 2026 (`21` Days Continuous)** | ₹100,000 | `21` (`10W/11L`, `47.6%`) | **`1.28`** | **`+₹3,423.21 (+3.42%)`** | Profitable |
| **Sep – Nov 2026 (`48` Days Continuous)** | ₹100,000 | `75` (`49W/26L`, `65.3%`) | **`2.48`** | **`+₹132,569.42 (+132.57%)`** | Multi-Month Winner |
| **March 2026 (`19` Days Continuous)** | ₹100,000 | `40` (`24W/16L`, `60.0%`) | **`1.30`** | **`+₹10,043.72 (+10.04%)`** | Profitable |
| **April 2026 (`20` Days Continuous)** | ₹100,000 | `44` (`28W/16L`, `63.6%`) | **`1.63`** | **`+₹19,790.40 (+19.79%)`** | Profitable |
| **Combined Mar – Apr 2026 (`39` Days)** | ₹100,000 | `79` (`47W/32L`, `59.5%`) | **`1.60`** | **`+₹46,315.00 (+46.31%)`** | Substantial Gain |
| **Nov 11, 2026 (Live Recorded Replay)** | ₹100,000 | `2` (`0W/2L`, `0.0%`) | `0.00` | **`-₹2,273.72 (-2.27%)`** | Strict Stop Discipline |
| **Nov 12, 2026 (Live Replay)** | ₹100,000 | `2` (`2W/0L`, `100.0%`) | `∞` | **`+₹2,684.00 (+2.68%)`** | Perfect Day |
| **Nov 13, 2026 (Live Practice Session)** | ₹50,000 | `2` (`1W/1L`, `50.0%`) | **`1.82`** | **`+₹1,386.93 (+2.77%)`** | **Beat 15 Iter Winner** 🏆 |
| **Nov 16, 2026 (Full-Day Practice)** | ₹50,000 | `0` (`0W/0L`) | `N/A` | **`₹0.00 (0.00% DD)`** | 100% Capital Preserved |
| **Nov 17, 2026 (Live Practice Session)** | ₹50,000 | `4` (`3W/1L`, `75.0%`) | **`2.15`** | **`+₹2,212.67 (+4.43%)`** | **Beat 15 Iter Winner** 🏆 |
| **Nov 18, 2026 (Live Practice Session)** | ₹50,000 | `3` (`1W/2L`, `33.3%`) | `0.15` | **`-₹3,281.27 (-6.56%)`** | Controlled Risk / 1-Lot Discipline |
| **Nov 20, 2026 (Live Practice Session)** | ₹50,000 | `3` (`3W/0L`, `100.0%`) | **`∞`** | **`+₹2,211.45 (+4.42%)`** | **Perfect Day / 100% Win Rate** 🏆 |

---

## 📁 Directory Structure & File Map

```
winner of 51/  (also symlinked as "winner 51")
├── winner_51_bot.py              # Main standalone executable CLI bot
├── run_live.py                   # One-click live practice trading script
├── Run_Bot.sh                    # Mac/Linux launcher script
├── Run_Bot.bat                   # Windows launcher script
├── test_bot.py                   # Unit test and self-test verification suite
├── fast_practice.py              # Champion execution engine & FastUpstox adapter
├── core.py                       # Standalone core trading primitives & math
├── market.py                     # Market data and breadth calculation helpers
├── strategies.py                 # Technical indicator calculations
├── engine.py                     # PaperEngine base class
├── nifty_membership.json         # NIFTY 50 constituents key mappings
├── config_50k.json               # ₹50,000 initial wallet configuration
├── config_100k.json              # ₹100,000 initial wallet configuration
├── config_fast_practice_50k.json # (Compatibility config alias)
├── config_fast_practice_100k.json# (Compatibility config alias)
└── results/                      # Complete organized trade logs & summaries
    ├── aug_2026/                 # August 2026 results (+₹3,423.21)
    ├── sep_nov_2026/             # Sep-Nov 2026 results (+₹132,569.42)
    ├── mar_apr_2026/             # March & April 2026 comparison (+₹46,315.00)
    ├── nov_11_2026/              # Nov 11 results
    ├── nov_12_2026/              # Nov 12 results (+₹2,684.00)
    ├── nov_13_2026_live/         # Nov 13 live practice (+₹1,386.93)
    ├── nov_16_2026_full_day/     # Nov 16 full-day replay (₹0.00 / 0% DD)
    ├── nov_17_2026_live/         # Nov 17 live practice (+₹2,212.67)
    ├── nov_18_2026_live/         # Nov 18 live practice (-₹3,281.27)
    └── nov_20_2026_live/         # Nov 20 live practice (+₹2,211.45, 100% WR)
```
