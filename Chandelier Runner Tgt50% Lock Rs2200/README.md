# Chandelier Runner Tgt50% Lock Rs2200

**Bot ID**: `bot_01_chandelier_run_t50_l2200`  
**Strategy Name**: Chandelier Runner Tgt50% Lock Rs2200  
**Family**: Chandelier Wide-Target  
**Previous Championship Rank**: **#1 Overall Champion** (Top 3 Finalist)

---

## 🏆 Performance Summary (Sep 1 – Nov 18, 2026 | 54 NSE Sessions)

| Metric | Score | Baseline (Winner 51) | Outperformance |
|---|---|---|---|
| **Initial Wallet** | ₹50,000.00 | ₹50,000.00 | — |
| **Total Net Profit** | **+₹66,851.51** | +₹16,888.54 | **+₹49,962.97 (+296%)** |
| **Total Return** | **+133.70%** | +33.78% | **+99.92%** |
| **Profit Factor** | **2.179** | 1.258 | **+0.921** |
| **Max Drawdown** | ₹14,439.32 | ₹17,636.06 | Controlled Risk |
| **Calmar Ratio** | **4.630** | 0.958 | **+3.672** |
| **Closed Trades** | 95 | 93 | Average > 1.7/day |
| **Win Rate** | **66.32%** (63W / 32L) | 53.76% | High Consistency |
| **Active Days** | 51 / 54 (94.4%) | 51 / 54 | High Market Coverage |

---

## 🚀 Quick Start Guide

This directory is **100% self-contained**. You can copy this folder to any project, server, or live environment.

### 1. Check Bot Status / Config
```bash
python3 run.py status
# or
python3 bot.py status
```

### 2. Live Paper Trading with Upstox API
Export your active Upstox access token and run:
```bash
export UPSTOX_ACCESS_TOKEN="<your_jwt_access_token>"
python3 run.py run --wallet 50000
```

### 3. Historical Replay
```bash
python3 run.py replay --days 5
```

---

## ⚙️ Strategy Core Architecture

- **Signal Engine**: Supertrend & Kaufman Efficiency dual-momentum filter on 5-minute index bars with Donchian channel breakout confirmation and Nifty 50 constituent delta breadth filter.
- **Position Sizing**: 2 lots, capital floor ₹30,000.
- **Profit Target**: 50% target fraction with ₹2,200 daily profit lock.
- **Trailing Stop**: 5.5% trailing stop-loss with 5-stage multi-tier step trailing profit lock:
  - Level 1: +8.5% gain locks +3.8%
  - Level 2: +15.0% gain locks +9.5%
  - Level 3: +23.0% gain locks +16.5%
  - Level 4: +32.0% gain locks +25.0%
  - Level 5: +42.0% gain locks +34.0%
- **Causality & Slippage**: 2-step signal-to-fill lag, real Upstox bid-ask spread quotes, and statutory exchange transaction charges & STT.
