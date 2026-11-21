# Championship Winning Trading Bot

**Bot ID**: `bot_25_chand_4l_f14k_l2500_t70_tr52`  
**Strategy Name**: Rung 25 — Chandelier 4L F14k L2500 T70%  
**Family**: Chandelier Compounding 4L  
**Championship Rank**: **#1 of 29 Competitors** (Undefeated Champion)

---

## 🏆 Performance Summary (Sep 1 – Nov 18, 2026 | 54 NSE Sessions)

| Metric | Champion Score | Baseline (Winner 51) | Outperformance |
|---|---|---|---|
| **Initial Wallet** | ₹50,000.00 | ₹50,000.00 | — |
| **Total Net Profit** | **₹+103,813.88** | +₹16,888.54 | **₹+86,925.34** |
| **Total Return** | **+207.63%** | +33.78% | **+173.85%** |
| **Tournament Record** | **28W - 0D - 0L** | 0W - 0D - 28L | **84 pts** |
| **Profit Factor** | **2.238** | 1.258 | **+0.980** |
| **Max Drawdown** | ₹19,395.31 | ₹5,520.00 | Controlled Risk |
| **Calmar Ratio** | **5.35** | 3.06 | Superior Risk-Adjusted |
| **Closed Trades** | 93 | 93 | Average > 1.0/day |
| **Win Rate** | 66.7% | 48.4% | High Edge |

---

## 🚀 Quick Start Guide

This directory is **100% self-contained**. You can copy this folder to any project, server, or live environment.

### 1. Check Bot Status / Config
```bash
python3 run.py status
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
- **Risk Management**: Dynamic compounding position sizing with capital floor protection, strict daily profit lock, trailing stop-loss, and multi-tier step trailing profit lock.
- **Causality & Slippage**: 2-step signal-to-fill lag, real Upstox bid-ask spread quotes, and statutory exchange transaction charges & STT.
