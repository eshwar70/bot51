#!/usr/bin/env python3
"""Self-contained live Upstox practice runtime & causal strategy engine for Competition Bots.

PRACTICE MODE ONLY — Read-only Upstox market data endpoints only (zero live broker order endpoints).
Identical execution accounting, slippage (0.2%/side), fees (Rs 40/side), capital floor,
depth checks, and two-step causal signal->fill discipline as Winner of 51.
"""
from __future__ import annotations

import argparse
from collections import deque
import copy
import csv
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
INDEX = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"

# ANSI Colors for live CLI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BG_GREEN = "\033[42;30m"
BG_RED = "\033[41;37m"
BG_YELLOW = "\033[43;30m"
BG_CYAN = "\033[46;30m"


class Blocked(Exception):
    """Raised on invalid, stale, or incomplete market data."""


def stamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise Blocked("Timestamp must include timezone")
        return value.astimezone(IST)
    if isinstance(value, (int, float)) or str(value).isdigit():
        v = float(value)
        return datetime.fromtimestamp(v / 1000.0 if v > 1e11 else v, IST)
    raw = str(value).strip().replace("Z", "+00:00")
    raw = re.sub(r"\.(\d+)(?=[+-]|$)", lambda m: "." + (m.group(1) + "000000")[:6], raw)
    d = datetime.fromisoformat(raw)
    if d.tzinfo is None:
        raise Blocked("Timestamp must include a timezone")
    return d.astimezone(IST)


def finite(value: Any) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise Blocked("Non-finite numeric data")
    return v


def atomic(path: Path | str, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.flush()
    os.replace(temp, path)


def emit(path: Path | str, data: Any) -> None:
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, allow_nan=False) + "\n")


# ---------------------------------------------------------------------------
# Technical Indicator Library (Pure Python, Strictly Causal)
# ---------------------------------------------------------------------------

def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    res = values[0]
    for v in values[1:]:
        res += (v - res) * k
    return res


def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    res = values[0]
    for v in values[1:]:
        res += (v - res) * k
        out.append(res)
    return out


def sma(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    sub = values[-period:]
    return sum(sub) / len(sub)


def stdev(values: List[float], period: int) -> float:
    sub = values[-period:]
    if len(sub) < 2:
        return 0.0
    m = sum(sub) / len(sub)
    return math.sqrt(sum((x - m) ** 2 for x in sub) / len(sub))


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    diffs = [b - a for a, b in zip(values[-(period + 1) : -1], values[-period:])]
    gains = sum(max(d, 0.0) for d in diffs)
    losses = sum(max(-d, 0.0) for d in diffs)
    if gains == 0.0 and losses == 0.0:
        return 50.0
    if losses == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def macd_hist(values: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Tuple[float, float]:
    if len(values) < slow + 2:
        return 0.0, 0.0
    kf, ks = 2.0 / (fast + 1.0), 2.0 / (slow + 1.0)
    ef = es = values[0]
    for v in values[1 : slow - 1]:
        ef += (v - ef) * kf
        es += (v - es) * ks
    series = []
    for v in values[slow - 1 :]:
        ef += (v - ef) * kf
        es += (v - es) * ks
        series.append(ef - es)
    signal_line = ema(series, sig)
    return series[-1], series[-1] - signal_line


def atr_bars(bars: List[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    start = max(1, len(bars) - period)
    for i in range(start, len(bars)):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def adx_di(bars: List[dict], period: int = 14) -> Tuple[float, float, float]:
    """Wilder-like ADX, +DI, -DI from completed OHLC bars."""
    if len(bars) < period + 2:
        return 0.0, 0.0, 0.0
    sub = bars[-(period * 2 + 2) :]
    tr_l, pdm_l, mdm_l = [], [], []
    for i in range(1, len(sub)):
        h, l, pc = float(sub[i]["high"]), float(sub[i]["low"]), float(sub[i - 1]["close"])
        ph, pl = float(sub[i - 1]["high"]), float(sub[i - 1]["low"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        tr_l.append(tr)
        pdm_l.append(pdm)
        mdm_l.append(mdm)
    if len(tr_l) < period:
        return 0.0, 0.0, 0.0
    s_tr = sum(tr_l[:period])
    s_p = sum(pdm_l[:period])
    s_m = sum(mdm_l[:period])
    dx_vals = []
    pdi = mdi = 0.0
    for i in range(period, len(tr_l)):
        s_tr = s_tr - (s_tr / period) + tr_l[i]
        s_p = s_p - (s_p / period) + pdm_l[i]
        s_m = s_m - (s_m / period) + mdm_l[i]
        if s_tr > 1e-9:
            pdi = 100.0 * s_p / s_tr
            mdi = 100.0 * s_m / s_tr
            denom = pdi + mdi
            dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom > 1e-9 else 0.0)
    adx = sum(dx_vals[-period:]) / len(dx_vals[-period:]) if dx_vals else 0.0
    return adx, pdi, mdi


def supertrend_dir(bars: List[dict], period: int = 10, mult: float = 2.5) -> Tuple[int, float]:
    """Causal Supertrend direction (+1 / -1) and band level."""
    if len(bars) < period + 2:
        return 0, 0.0
    sub = bars[-(period * 3 + 5) :]
    trs = [float(sub[0]["high"]) - float(sub[0]["low"])]
    for i in range(1, len(sub)):
        h, l, pc = float(sub[i]["high"]), float(sub[i]["low"]), float(sub[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atrs = ema_series(trs, period)
    upper = lower = 0.0
    direction = 1
    for i in range(len(sub)):
        hl2 = 0.5 * (float(sub[i]["high"]) + float(sub[i]["low"]))
        c = float(sub[i]["close"])
        bu = hl2 + mult * atrs[i]
        bl = hl2 - mult * atrs[i]
        if i == 0:
            upper, lower = bu, bl
            continue
        pc = float(sub[i - 1]["close"])
        upper = bu if (bu < upper or pc > upper) else upper
        lower = bl if (bl > lower or pc < lower) else lower
        if direction == 1 and c < lower:
            direction = -1
        elif direction == -1 and c > upper:
            direction = 1
    return direction, (lower if direction == 1 else upper)


def efficiency_ratio(closes: List[float], window: int = 12) -> float:
    if len(closes) < window + 1:
        return 0.0
    net = abs(closes[-1] - closes[-1 - window])
    path = sum(abs(b - a) for a, b in zip(closes[-1 - window : -1], closes[-window:]))
    return net / path if path > 1e-9 else 0.0


def stochastic_k(bars: List[dict], period: int = 14) -> float:
    if len(bars) < period:
        return 50.0
    sub = bars[-period:]
    lo = min(float(b["low"]) for b in sub)
    hi = max(float(b["high"]) for b in sub)
    if hi - lo < 1e-9:
        return 50.0
    return 100.0 * (float(sub[-1]["close"]) - lo) / (hi - lo)


# ---------------------------------------------------------------------------
# Live Upstox Market Data Adapter (Read-Only GETs Only)
# ---------------------------------------------------------------------------

def _previous_close(q: dict) -> Optional[float]:
    last, nc = q.get("last_price"), q.get("net_change")
    if last is not None and nc is not None:
        try:
            return round(finite(last) - finite(nc), 4)
        except (ValueError, TypeError, Blocked):
            pass
    return q.get("ohlc", {}).get("close")


class LiveUpstoxAdapter:
    """Rate-budgeted live Upstox market-data adapter (read-only GETs only)."""

    ALLOWED_PREFIXES = (
        "/v2/market/status/",
        "/v2/option/contract",
        "/v2/option/chain",
        "/v2/market-quote/",
        "/v3/historical-candle/",
        "/v2/historical-candle/",
    )

    def __init__(self, membership_path: str | Path):
        self.token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not self.token:
            raise Blocked("UPSTOX_ACCESS_TOKEN environment variable is required.")
        self.membership = json.loads(Path(membership_path).read_text(encoding="utf-8"))
        keys = [m["key"] for m in self.membership["members"]]
        if len(keys) != 50:
            raise Blocked("Expected 50 NIFTY constituents in membership file")
        self.extra_quote_keys = [INDEX] + keys + [VIX_KEY]
        self._contracts_day: Optional[str] = None
        self._expiry: Optional[str] = None
        self._meta: Dict[str, dict] = {}
        self._status_cache: Optional[Tuple[float, str]] = None
        self._chain_cache: Optional[Tuple[float, list]] = None
        self._bars_cache: Optional[Tuple[float, list]] = None
        self._prior_bars_day: Optional[str] = None
        self._prior_bars: List[dict] = []
        self.live_ticks: List[dict] = []

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        if not any(str(path).startswith(p) for p in self.ALLOWED_PREFIXES):
            raise Blocked(f"Blocked endpoint {path}: read-only market data only")
        url = "https://api.upstox.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            raise Blocked(f"Upstox HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise Blocked("Upstox connection unavailable") from None
        if result.get("status") != "success":
            raise Blocked("Upstox returned non-success response")
        return result["data"]

    def candles_1m(self, start: Optional[str] = None, end: Optional[str] = None) -> List[dict]:
        key = urllib.parse.quote(INDEX, safe="")
        path = f"/v3/historical-candle/intraday/{key}/minutes/1"
        if start:
            path = f"/v3/historical-candle/{key}/minutes/1/{end}/{start}"
        raw = self.get(path)["candles"]
        return [
            dict(timestamp=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
            for r in sorted(raw, key=lambda r: stamp(r[0]))
        ]

    def _refresh_status(self, now_mono: float) -> str:
        if self._status_cache is None or (now_mono - self._status_cache[0]) >= 30.0:
            st = self.get("/v2/market/status/NSE")["status"]
            self._status_cache = (now_mono, st)
        return self._status_cache[1]

    def _refresh_contracts(self, today: date) -> Tuple[str, dict]:
        day_s = today.isoformat()
        if self._contracts_day != day_s or not self._expiry:
            contracts = self.get("/v2/option/contract", {"instrument_key": INDEX})
            expiries = sorted(
                {x["expiry"] for x in contracts if 2 <= (date.fromisoformat(x["expiry"]) - today).days <= 14}
            )
            if not expiries:
                raise Blocked("No suitable expiry 2-14 calendar days away")
            self._expiry = expiries[0]
            self._meta = {x["instrument_key"]: x for x in contracts if x["expiry"] == self._expiry}
            self._contracts_day = day_s
        return self._expiry, self._meta

    def _refresh_chain(self, now_mono: float, expiry: str) -> list:
        if self._chain_cache is None or (now_mono - self._chain_cache[0]) >= 10.0:
            chain = self.get("/v2/option/chain", {"instrument_key": INDEX, "expiry_date": expiry})
            self._chain_cache = (now_mono, chain)
        return self._chain_cache[1]

    def _refresh_bars(self, begin: datetime, now_mono: float) -> List[dict]:
        day_s = begin.date().isoformat()
        if self._prior_bars_day != day_s:
            self._prior_bars = self.candles_1m(
                (begin.date() - timedelta(days=5)).isoformat(),
                (begin.date() - timedelta(days=1)).isoformat(),
            )
            self._prior_bars_day = day_s
        need_fetch = self._bars_cache is None or (now_mono - self._bars_cache[0]) >= 10.0
        if not need_fetch and self._bars_cache and self._bars_cache[1]:
            last_end = stamp(self._bars_cache[1][-1]["timestamp"]) + timedelta(minutes=1)
            if (begin - last_end).total_seconds() >= 62.0:
                need_fetch = True
        if need_fetch:
            cur = self.candles_1m()
            first_cur = stamp(cur[0]["timestamp"]) if cur else None
            prior = [b for b in self._prior_bars if first_cur is None or stamp(b["timestamp"]) < first_cur]
            self._bars_cache = (now_mono, prior + cur)
        bars = [b for b in self._bars_cache[1] if stamp(b["timestamp"]) + timedelta(minutes=1) <= begin]
        return bars[-240:]

    def snapshot(self, pinned_keys: Optional[set] = None) -> dict:
        begin = datetime.now(IST)
        now_mono = time.monotonic()
        status = self._refresh_status(now_mono)
        if status != "NORMAL_OPEN":
            self._status_cache = None
            raise Blocked("Exchange not NORMAL_OPEN: " + status)
        expiry, meta = self._refresh_contracts(begin.date())
        chain = self._refresh_chain(now_mono, expiry)
        bars = self._refresh_bars(begin, now_mono)
        ref_spot = float(bars[-1]["close"]) if bars else None
        if self.live_ticks:
            ref_spot = float(self.live_ticks[-1]["ltp"])

        options = []
        pinned = pinned_keys or set()
        for row in chain:
            strike = float(row["strike_price"])
            if ref_spot is not None and abs(strike - ref_spot) > 1000.0:
                c_key = row.get("call_options", {}).get("instrument_key")
                p_key = row.get("put_options", {}).get("instrument_key")
                if c_key not in pinned and p_key not in pinned:
                    continue
            for side, kind in [("call_options", "CE"), ("put_options", "PE")]:
                raw = row.get(side, {})
                key = raw.get("instrument_key")
                if key not in meta:
                    continue
                md, g = raw.get("market_data", {}), raw.get("option_greeks", {})
                options.append(
                    dict(
                        key=key,
                        kind=kind,
                        expiry=expiry,
                        strike=strike,
                        lot=meta[key]["lot_size"],
                        bid=md.get("bid_price"),
                        ask=md.get("ask_price"),
                        bid_qty=md.get("bid_qty"),
                        ask_qty=md.get("ask_qty"),
                        oi=md.get("oi"),
                        volume=md.get("volume"),
                        greeks=g,
                    )
                )

        keys = list(dict.fromkeys([o["key"] for o in options] + list(self.extra_quote_keys)))
        quotes = {}
        for offset in range(0, len(keys), 400):
            quotes.update(
                self.get("/v2/market-quote/quotes", {"instrument_key": ",".join(keys[offset : offset + 400])})
            )
        bykey = {q["instrument_token"]: q for q in quotes.values()}
        for o in options:
            q = bykey.get(o["key"], {})
            o["quote_time"] = q.get("timestamp")
            depth = q.get("depth", {})
            buy, sell = depth.get("buy", []), depth.get("sell", [])
            if buy and sell:
                o.update(
                    bid=buy[0]["price"],
                    bid_qty=buy[0]["quantity"],
                    ask=sell[0]["price"],
                    ask_qty=sell[0]["quantity"],
                )

        bquotes = []
        for m in self.membership["members"]:
            q = bykey.get(m["key"])
            if not q:
                continue
            bquotes.append(
                dict(
                    key=m["key"],
                    timestamp=q.get("timestamp"),
                    last=q.get("last_price"),
                    previous_close=_previous_close(q),
                    day_open=q.get("ohlc", {}).get("open"),
                )
            )

        spot_q = bykey.get(INDEX, {})
        spot_ltp = spot_q.get("last_price")
        spot_ts = spot_q.get("timestamp")
        if spot_ltp is not None and spot_ts and (not self.live_ticks or spot_ts != self.live_ticks[-1]["timestamp"]):
            self.live_ticks.append(dict(timestamp=spot_ts, ltp=float(spot_ltp)))
            self.live_ticks = self.live_ticks[-120:]

        vix_q = bykey.get(VIX_KEY, {})
        end = datetime.now(IST)
        return dict(
            timestamp=end.isoformat(),
            collected_from=begin.isoformat(),
            source="upstox-fast-1m-v02",
            market_status=status,
            underlying=INDEX,
            bar_minutes=1,
            bars=bars,
            spot_live=dict(
                timestamp=spot_ts,
                ltp=spot_ltp,
                open=spot_q.get("ohlc", {}).get("open"),
                high=spot_q.get("ohlc", {}).get("high"),
                low=spot_q.get("ohlc", {}).get("low"),
            ),
            ticks=list(self.live_ticks[-30:]),
            options=options,
            breadth=dict(
                timestamp=end.isoformat(),
                universe="NIFTY50",
                expected=50,
                membership_as_of=self.membership["membership_as_of"],
                quotes=bquotes,
            ),
            vix=dict(timestamp=vix_q.get("timestamp"), value=vix_q.get("last_price")),
        )


# ---------------------------------------------------------------------------
# Shared Causal Validation & Option Quality Gates (Identical to Winner 51)
# ---------------------------------------------------------------------------

def _fresh(value: Any, now: datetime, seconds: float, skew: float = 2.0) -> bool:
    try:
        age = (now - stamp(value)).total_seconds()
        return -skew <= age <= seconds
    except (ValueError, TypeError, Blocked):
        return False


def option_ok(o: dict, now: datetime, c: dict, snap_ts_str: Optional[str] = None) -> bool:
    try:
        bid, ask = float(o["bid"]), float(o["ask"])
        if not (0 < bid <= ask) or (ask - bid) / ask > c["max_spread_fraction"]:
            return False
        qt = o.get("quote_time")
        if snap_ts_str is None or qt != snap_ts_str:
            skew = float(c.get("quote_clock_skew_seconds", 2.0))
            if not _fresh(qt, now, c["quote_max_age_seconds"], skew):
                return False
        dte = o.get("_dte")
        if dte is None:
            dte = (date.fromisoformat(o["expiry"]) - now.date()).days
        if not 2 <= dte <= 14:
            return False
        lot = o["lot"]
        if lot <= 0:
            return False
        if min(float(o["bid_qty"]), float(o["ask_qty"])) < lot:
            return False
        if float(o["oi"]) < c["min_oi"] or float(o["volume"]) < c["min_volume"]:
            return False
        g = o["greeks"]
        delta = float(g["delta"])
        if (o["kind"] == "CE" and delta <= 0) or (o["kind"] == "PE" and delta >= 0):
            return False
        return (
            c["delta_min"] <= abs(delta) <= c["delta_max"]
            and abs(float(g["theta"])) / ask <= c["max_theta_fraction"]
            and 0 <= float(g["gamma"]) * 100.0 <= c["max_gamma_move"]
            and c["min_iv"] <= float(g["iv"]) <= c["max_iv"]
            and 0 <= float(g["vega"]) * 5.0 / ask <= c["max_vega_shock_fraction"]
        )
    except (KeyError, TypeError, ValueError, Blocked):
        return False


def validate_snapshot(s: dict, now: datetime, c: dict) -> None:
    ts = stamp(s["timestamp"])
    if s.get("underlying") != INDEX or s.get("market_status") != "NORMAL_OPEN":
        raise Blocked("Wrong underlying or market closed")
    if not 0 <= (now - ts).total_seconds() <= c["snapshot_max_age_seconds"]:
        raise Blocked("Stale or future snapshot")
    if not 0 <= (ts - stamp(s["collected_from"])).total_seconds() <= c["snapshot_max_age_seconds"]:
        raise Blocked("Data collection too slow")
    bars = s["bars"]
    if len(bars) < 60:
        raise Blocked("Need at least 60 completed 1-minute candles")
    last_ts = stamp(bars[-1]["timestamp"])
    if last_ts + timedelta(minutes=1) > ts:
        raise Blocked("Incomplete 1-minute candle / future information")
    if (ts - last_ts - timedelta(minutes=1)).total_seconds() > c["bar_stale_seconds"]:
        raise Blocked("1-minute underlying candles are stale")


# ---------------------------------------------------------------------------
# Causal Feature Extractor (Computed Once Per Minute or Cached)
# ---------------------------------------------------------------------------

def extract_market_features(
    s: dict,
    now: datetime,
    c: dict,
    breadth_history: deque,
    day_state: dict,
) -> dict:
    """Compute strictly causal multi-timeframe features from completed bars + live snapshot."""
    bars = s["bars"]
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    opens = [float(b["open"]) for b in bars]
    price = closes[-1]
    prev = closes[-2]
    prev2 = closes[-3]

    spot_live = s.get("spot_live") or {}
    ref_spot = float(spot_live["ltp"]) if spot_live.get("ltp") is not None else price

    # 1-minute indicators
    e5 = ema(closes, 5)
    e8 = ema(closes, 8)
    e9 = ema(closes, 9)
    e13 = ema(closes, 13)
    e21 = ema(closes, 21)
    e34 = ema(closes, 34)
    e50 = ema(closes, 50)
    prev_e9 = ema(closes[:-1], 9)
    prev_e21 = ema(closes[:-1], 21)
    macd_val, m_hist = macd_hist(closes)
    _, prev_m_hist = macd_hist(closes[:-1])
    rsi14 = rsi(closes, 14)
    rsi7 = rsi(closes, 7)
    rsi14_prev = rsi(closes[:-1], 14)
    rsi14_prev2 = rsi(closes[:-2], 14)
    atr14_1m = atr_bars(bars, 14)
    roc3 = price / closes[-4] - 1.0
    roc5 = price / closes[-6] - 1.0
    roc10 = price / closes[-11] - 1.0
    roc15 = price / closes[-16] - 1.0
    eff10_1m = efficiency_ratio(closes, 10)
    eff20_1m = efficiency_ratio(closes, 20)
    stoch14 = stochastic_k(bars, 14)
    adx1m, pdi1m, mdi1m = adx_di(bars, 14)
    st1m_dir, st1m_lvl = supertrend_dir(bars, 10, 2.5)

    # Bollinger & Keltner on 1m (20 bars)
    bb_mid = sma(closes, 20)
    bb_sd = stdev(closes, 20)
    bb_up = bb_mid + 2.0 * bb_sd
    bb_dn = bb_mid - 2.0 * bb_sd
    bb_width = (4.0 * bb_sd / bb_mid) if bb_mid > 0 else 0.0
    kc_up = bb_mid + 1.5 * atr14_1m
    kc_dn = bb_mid - 1.5 * atr14_1m
    squeeze_on = (bb_up < kc_up) and (bb_dn > kc_dn)

    # Donchian channels on 1m
    don10_hi = max(highs[-11:-1])
    don10_lo = min(lows[-11:-1])
    don20_hi = max(highs[-21:-1])
    don20_lo = min(lows[-21:-1])
    don5_hi = max(highs[-6:-1])
    don5_lo = min(lows[-6:-1])

    # Aggregate strictly completed 5-minute candles
    grouped_5m: Dict[Tuple[str, int], List[dict]] = {}
    grouped_15m: Dict[Tuple[str, int], List[dict]] = {}
    day_prefix = now.date().isoformat()
    today_bars = []
    prior_bars = []
    for b in bars:
        ts_s = b["timestamp"]
        d_s = ts_s[:10]
        hh = int(ts_s[11:13])
        mm = int(ts_s[14:16])
        mins = hh * 60 + mm
        grouped_5m.setdefault((d_s, mins // 5), []).append(b)
        grouped_15m.setdefault((d_s, mins // 15), []).append(b)
        if d_s == day_prefix:
            today_bars.append(b)
        else:
            prior_bars.append(b)

    bars_5m = [
        dict(
            open=float(bs[0]["open"]),
            high=max(float(x["high"]) for x in bs),
            low=min(float(x["low"]) for x in bs),
            close=float(bs[-1]["close"]),
        )
        for bk in sorted(grouped_5m)
        for bs in (grouped_5m[bk],)
        if len(bs) == 5
    ]
    bars_15m = [
        dict(
            open=float(bs[0]["open"]),
            high=max(float(x["high"]) for x in bs),
            low=min(float(x["low"]) for x in bs),
            close=float(bs[-1]["close"]),
        )
        for bk in sorted(grouped_15m)
        for bs in (grouped_15m[bk],)
        if len(bs) == 15
    ]

    # 5-minute indicators
    trend_5m = 0
    trend_5m_fast = 0
    ema5_5m = ema9_5m = ema13_5m = ema21_5m = price
    eff_5m = esep_5m = esep_5m_fast = atr_5m = 0.0
    rsi_5m = 50.0
    adx_5m = pdi_5m = mdi_5m = 0.0
    st5m_dir = 0
    macd5_val = macd5_hist = 0.0
    if len(bars_5m) >= 10:
        c5 = [x["close"] for x in bars_5m]
        ema5_5m = ema(c5, 5)
        ema9_5m = ema(c5, min(9, len(c5)))
        ema13_5m = ema(c5, min(13, len(c5)))
        ema21_5m = ema(c5, min(21, len(c5)))
        trend_5m = 1 if c5[-1] > ema9_5m > ema21_5m else -1 if c5[-1] < ema9_5m < ema21_5m else 0
        trend_5m_fast = 1 if c5[-1] > ema5_5m > ema13_5m else -1 if c5[-1] < ema5_5m < ema13_5m else 0
        eff_5m = efficiency_ratio(c5, min(12, len(c5) - 1))
        atr_5m = atr_bars(bars_5m, 14)
        esep_5m = abs(ema9_5m - ema21_5m) / atr_5m if atr_5m > 0 else 0.0
        esep_5m_fast = abs(ema5_5m - ema13_5m) / atr_5m if atr_5m > 0 else 0.0
        rsi_5m = rsi(c5, min(14, len(c5) - 1))
        adx_5m, pdi_5m, mdi_5m = adx_di(bars_5m, min(14, max(5, len(bars_5m) // 3)))
        st5m_dir, _ = supertrend_dir(bars_5m, min(10, max(5, len(bars_5m) // 3)), 2.2)
        if len(c5) >= 28:
            macd5_val, macd5_hist = macd_hist(c5)

    # 15-minute bias
    bias_15m = 0
    if len(bars_15m) >= 4:
        c15 = [x["close"] for x in bars_15m]
        e4_15 = ema(c15, min(4, len(c15)))
        e10_15 = ema(c15, min(10, len(c15)))
        bias_15m = 1 if c15[-1] > e4_15 > e10_15 else -1 if c15[-1] < e4_15 < e10_15 else 0

    # Whole-session causal TWAP, ORB (15m and 30m), Day High/Low, Prev Day High/Low/Close
    if day_state.get("date") != day_prefix:
        day_state.clear()
        day_state["date"] = day_prefix
        day_state["seen_bars"] = set()
        day_state["tp_sum"] = 0.0
        day_state["tp_cnt"] = 0
        day_state["day_open"] = float(today_bars[0]["open"]) if today_bars else price
        day_state["day_high"] = -1e9
        day_state["day_low"] = 1e9
        day_state["orb15_hi"] = -1e9
        day_state["orb15_lo"] = 1e9
        day_state["orb15_cnt"] = 0
        day_state["orb30_hi"] = -1e9
        day_state["orb30_lo"] = 1e9
        day_state["orb30_cnt"] = 0
        # Prior session OHLC from prior_bars
        if prior_bars:
            pdate = prior_bars[-1]["timestamp"][:10]
            pb = [x for x in prior_bars if x["timestamp"][:10] == pdate]
            day_state["prev_high"] = max(float(x["high"]) for x in pb)
            day_state["prev_low"] = min(float(x["low"]) for x in pb)
            day_state["prev_close"] = float(pb[-1]["close"])
        else:
            day_state["prev_high"] = None
            day_state["prev_low"] = None
            day_state["prev_close"] = None

    for b in today_bars:
        bts = b["timestamp"]
        if bts not in day_state["seen_bars"]:
            day_state["seen_bars"].add(bts)
            bh, bl, bc = float(b["high"]), float(b["low"]), float(b["close"])
            day_state["tp_sum"] += (bh + bl + bc) / 3.0
            day_state["tp_cnt"] += 1
            day_state["day_high"] = max(day_state["day_high"], bh)
            day_state["day_low"] = min(day_state["day_low"], bl)
            hhmm = bts[11:16]
            if "09:15" <= hhmm < "09:30":
                day_state["orb15_hi"] = max(day_state["orb15_hi"], bh)
                day_state["orb15_lo"] = min(day_state["orb15_lo"], bl)
                day_state["orb15_cnt"] += 1
            if "09:15" <= hhmm < "09:45":
                day_state["orb30_hi"] = max(day_state["orb30_hi"], bh)
                day_state["orb30_lo"] = min(day_state["orb30_lo"], bl)
                day_state["orb30_cnt"] += 1

    twap_day = (day_state["tp_sum"] / day_state["tp_cnt"]) if day_state["tp_cnt"] > 0 else price
    day_open = day_state["day_open"]
    day_ret = (price - day_open) / max(1.0, day_open)
    gap_pct = (
        (day_open - day_state["prev_close"]) / day_state["prev_close"]
        if day_state.get("prev_close")
        else 0.0
    )

    # Breadth & rolling 15-25m delta breadth
    skew = float(c.get("quote_clock_skew_seconds", 2.0))
    b_obj = s.get("breadth") or {}
    b_rows = b_obj.get("quotes") or []
    adv = dec = tot = 0
    cur_map: Dict[str, float] = {}
    for q in b_rows:
        if not _fresh(q.get("timestamp"), now, c["quote_max_age_seconds"], skew):
            continue
        last = float(q["last"])
        ref = q.get("day_open")
        if ref is None or float(ref) <= 0:
            ref = q.get("previous_close")
        if ref is not None and float(ref) > 0 and last > 0:
            tot += 1
            if last > float(ref):
                adv += 1
            elif last < float(ref):
                dec += 1
            cur_map[q["key"]] = last

    breadth_ok = tot >= c["breadth_min_names"]
    adv_pct = (adv / tot) if tot > 0 else 0.5
    dec_pct = (dec / tot) if tot > 0 else 0.5
    breadth_dir = 1 if (breadth_ok and adv_pct >= c["breadth_agreement"]) else (-1 if (breadth_ok and dec_pct >= c["breadth_agreement"]) else 0)

    if breadth_history and (breadth_history[-1][0].date() != now.date() or now < breadth_history[-1][0]):
        breadth_history.clear()
    if len(cur_map) >= c["breadth_min_names"] and (
        not breadth_history or (now - breadth_history[-1][0]).total_seconds() >= 55.0
    ):
        breadth_history.append((now, cur_map))

    delta_adv = delta_dec = 0.5
    delta_breadth_dir = 0
    for t_old, mp in breadth_history:
        age = (now - t_old).total_seconds()
        if 900.0 <= age <= 1500.0:
            common = [k for k in cur_map if k in mp and mp[k] > 0]
            if len(common) >= c["breadth_min_names"]:
                delta_adv = sum(1 for k in common if cur_map[k] > mp[k]) / len(common)
                delta_dec = sum(1 for k in common if cur_map[k] < mp[k]) / len(common)
                thr = float(c.get("delta_breadth_agreement", 0.56))
                delta_breadth_dir = 1 if delta_adv >= thr else (-1 if delta_dec >= thr else 0)
            break

    # Near-ATM PCR & IV skew from option chain
    paired: Dict[Tuple[str, float], dict] = {}
    ce_ivs, pe_ivs = [], []
    for o in s.get("options") or []:
        if not _fresh(o.get("quote_time"), now, c["quote_max_age_seconds"], skew):
            continue
        k = float(o["strike"])
        if abs(k - ref_spot) > 500.0:
            continue
        oi = float(o.get("oi") or 0.0)
        if oi >= 0:
            paired.setdefault((o["expiry"], k), {})[o["kind"]] = oi
        iv_v = (o.get("greeks") or {}).get("iv")
        if iv_v is not None and abs(k - ref_spot) <= 150.0:
            if o["kind"] == "CE":
                ce_ivs.append(float(iv_v))
            else:
                pe_ivs.append(float(iv_v))

    complete = [(k, v) for k, v in paired.items() if "CE" in v and "PE" in v]
    pcr = 1.0
    pcr_dir = 0
    if len(complete) >= 5:
        c_oi = sum(v["CE"] for _, v in complete)
        p_oi = sum(v["PE"] for _, v in complete)
        if c_oi > 0:
            pcr = p_oi / c_oi
            pcr_dir = 1 if 1.02 <= pcr <= 2.50 else (-1 if 0.35 <= pcr <= 0.98 else 0)
    iv_skew = (sum(pe_ivs) / len(pe_ivs) - sum(ce_ivs) / len(ce_ivs)) if (ce_ivs and pe_ivs) else 0.0

    # VIX
    vix_obj = s.get("vix") or {}
    vix_fresh = _fresh(vix_obj.get("timestamp"), now, c["quote_max_age_seconds"], skew)
    vix_val = float(vix_obj["value"]) if (vix_fresh and vix_obj.get("value") is not None) else 0.0

    # Intraday 1m bar gap check
    has_gap = False
    for a, b in zip(bars[-30:], bars[-29:]):
        ta, tb = stamp(a["timestamp"]), stamp(b["timestamp"])
        if ta.date() == tb.date() and tb - ta != timedelta(minutes=1):
            has_gap = True
            break

    # Sub-minute tick thrust
    ticks = [
        t for t in (s.get("ticks") or [])
        if _fresh(t.get("timestamp"), now, 40.0, skew)
    ]
    tick_dir = 0
    tick_delta = 0.0
    if len(ticks) >= 3:
        span = (stamp(ticks[-1]["timestamp"]) - stamp(ticks[0]["timestamp"])).total_seconds()
        if span >= 4.0:
            tick_delta = float(ticks[-1]["ltp"]) - float(ticks[0]["ltp"])
            last_step = float(ticks[-1]["ltp"]) - float(ticks[-2]["ltp"])
            if abs(last_step) <= 25.0 and abs(tick_delta) <= 45.0:
                if tick_delta >= 5.0 and last_step >= 0:
                    tick_dir = 1
                elif tick_delta <= -5.0 and last_step <= 0:
                    tick_dir = -1

    # Flag metrics
    imp10 = (closes[-6] - closes[-16]) / max(1.0, closes[-16]) if len(closes) >= 20 else 0.0
    cons5 = (max(closes[-6:-1]) - min(closes[-6:-1])) / max(1.0, closes[-6]) if len(closes) >= 10 else 1.0

    # Candle body/wick ratios on latest 1m bar
    last_b = bars[-1]
    lb_o, lb_h, lb_l, lb_c = float(last_b["open"]), float(last_b["high"]), float(last_b["low"]), float(last_b["close"])
    lb_rng = max(0.05, lb_h - lb_l)
    body_ratio = (lb_c - lb_o) / lb_rng

    return dict(
        now=now,
        clock=now.strftime("%H:%M"),
        price=price,
        prev=prev,
        prev2=prev2,
        ref_spot=ref_spot,
        e5=e5, e8=e8, e9=e9, e13=e13, e21=e21, e34=e34, e50=e50,
        prev_e9=prev_e9, prev_e21=prev_e21,
        macd=macd_val, macd_hist=m_hist, prev_macd_hist=prev_m_hist,
        rsi14=rsi14, rsi7=rsi7, rsi14_prev=rsi14_prev, rsi14_prev2=rsi14_prev2,
        atr1m=atr14_1m,
        roc3=roc3, roc5=roc5, roc10=roc10, roc15=roc15,
        eff10_1m=eff10_1m, eff20_1m=eff20_1m,
        stoch14=stoch14,
        adx1m=adx1m, pdi1m=pdi1m, mdi1m=mdi1m,
        st1m_dir=st1m_dir, st1m_lvl=st1m_lvl,
        bb_mid=bb_mid, bb_sd=bb_sd, bb_up=bb_up, bb_dn=bb_dn, bb_width=bb_width,
        kc_up=kc_up, kc_dn=kc_dn, squeeze_on=squeeze_on,
        don5_hi=don5_hi, don5_lo=don5_lo,
        don10_hi=don10_hi, don10_lo=don10_lo,
        don20_hi=don20_hi, don20_lo=don20_lo,
        bars_5m_count=len(bars_5m),
        trend_5m=trend_5m, trend_5m_fast=trend_5m_fast,
        ema5_5m=ema5_5m, ema9_5m=ema9_5m, ema13_5m=ema13_5m, ema21_5m=ema21_5m,
        eff_5m=eff_5m, esep_5m=esep_5m, esep_5m_fast=esep_5m_fast, atr_5m=atr_5m,
        rsi_5m=rsi_5m, adx_5m=adx_5m, pdi_5m=pdi_5m, mdi_5m=mdi_5m,
        st5m_dir=st5m_dir, macd5=macd5_val, macd5_hist=macd5_hist,
        bias_15m=bias_15m,
        twap_day=twap_day, day_open=day_open, day_ret=day_ret, gap_pct=gap_pct,
        day_high=day_state["day_high"], day_low=day_state["day_low"],
        orb15_ready=day_state["orb15_cnt"] >= 15,
        orb15_hi=day_state["orb15_hi"], orb15_lo=day_state["orb15_lo"],
        orb30_ready=day_state["orb30_cnt"] >= 30,
        orb30_hi=day_state["orb30_hi"], orb30_lo=day_state["orb30_lo"],
        prev_high=day_state["prev_high"], prev_low=day_state["prev_low"], prev_close=day_state["prev_close"],
        breadth_ok=breadth_ok, adv_pct=adv_pct, dec_pct=dec_pct, breadth_dir=breadth_dir,
        delta_adv=delta_adv, delta_dec=delta_dec, delta_breadth_dir=delta_breadth_dir,
        pcr=pcr, pcr_dir=pcr_dir, iv_skew=iv_skew,
        vix_fresh=vix_fresh, vix=vix_val,
        has_gap=has_gap,
        tick_dir=tick_dir, tick_delta=tick_delta,
        imp10=imp10, cons5=cons5,
        body_ratio=body_ratio,
        closes=closes,
        bars_5m=bars_5m,
    )


# ---------------------------------------------------------------------------
# Fair-Competition Paper Execution Engine (Identical Cost/Fill Rules to W51)
# ---------------------------------------------------------------------------

BASE_BOT_CONFIG: Dict[str, Any] = {
    "initial_wallet": 50000.0,
    "capital_floor": 30000.0,
    "safety_buffer": 2000.0,
    "planned_risk": 3000.0,
    "daily_loss_limit": 4500.0,
    "daily_profit_lock": 12000.0,
    "dynamic_risk_scaling": True,
    "max_lots": 3,
    "loss_streak_single_lot": True,
    "loss_streak_lot_cap": 1,
    "target_delta": 0.55,
    "max_trades_day": 5,
    "max_loss_streak": 3,
    "stop_fraction": 0.12,
    "target_fraction": 0.35,
    "breakeven_activation_fraction": 0.08,
    "breakeven_lock_fraction": 0.03,
    "trail_activation_fraction": 0.14,
    "trail_fraction": 0.06,
    "step_trail_levels": [
        [0.10, 0.04],
        [0.18, 0.11],
        [0.26, 0.18],
        [0.35, 0.26],
    ],
    "slippage_fraction": 0.002,
    "fee_per_side": 40.0,
    "max_spread_fraction": 0.02,
    "min_oi": 10000,
    "min_volume": 10000,
    "delta_min": 0.35,
    "delta_max": 0.72,
    "max_theta_fraction": 0.20,
    "max_gamma_move": 0.35,
    "max_iv": 45.0,
    "min_iv": 5.0,
    "max_vega_shock_fraction": 1.0,
    "quote_max_age_seconds": 30,
    "snapshot_max_age_seconds": 45,
    "bar_stale_seconds": 120,
    "quote_clock_skew_seconds": 2.0,
    "breadth_min_names": 45,
    "breadth_agreement": 0.55,
    "delta_breadth_agreement": 0.56,
    "vix_min": 9.0,
    "vix_max": 35.0,
    "entry_start_clock": "09:20",
    "entry_end_clock": "14:40",
    "force_exit_clock": "15:15",
    "pending_ttl_seconds": 30,
    "cooldown_minutes": 4,
    "direction_cooldown_minutes": 10,
    "max_hold_minutes": 75,
    "max_premium_loss_fraction": 0.65,
    "poll_seconds": 2.5,
    "blackout_dates": [],
    "event_windows": [],
    "early_stall_exit": False,
    "early_stall_max_vix": 0.0,
    "stall_minutes": 11.0,
    "low_vix_regime": None,
}


class CompetitionBotEngine:
    """Strictly causal live/replay paper trading engine matching Winner 51 execution rules."""

    def __init__(
        self,
        bot_id: str,
        bot_name: str,
        config: Dict[str, Any],
        signal_fn: Callable[[dict, dict, dict], Tuple[int, str, Optional[dict]]],
        exit_fn: Optional[Callable[[dict, dict, dict, dict], Optional[str]]] = None,
        folder: Optional[str | Path] = None,
        persist: bool = True,
    ):
        self.bot_id = bot_id
        self.bot_name = bot_name
        self._base_c = {**BASE_BOT_CONFIG, **copy.deepcopy(config)}
        lv = self._base_c.get("low_vix_regime")
        if isinstance(lv, dict) and lv.get("enabled"):
            self._low_vix_c = {
                **self._base_c,
                **{k: v for k, v in lv.items() if k != "enabled"},
                "low_vix_regime": None,
            }
        else:
            self._low_vix_c = None
        self.c = self._base_c
        self.signal_fn = signal_fn
        self.exit_fn = exit_fn
        self.folder = Path(folder) if folder else None
        self.persist = bool(persist and self.folder is not None)
        if self.persist and self.folder is not None:
            self.folder.mkdir(parents=True, exist_ok=True)

        w0 = float(self._base_c["initial_wallet"])
        self.s: Dict[str, Any] = dict(
            initial_wallet=w0,
            cash=w0,
            equity=w0,
            peak_equity=w0,
            max_drawdown=0.0,
            position=None,
            pending=None,
            day=None,
            day_start=w0,
            trades_today=0,
            loss_streak=0,
            last_bar=None,
            last_snapshot=None,
            cooldown_until=None,
            last_loss_direction=None,
            direction_cooldown_until=None,
            capital_halted=False,
            trades=[],
            daily={},
            last_decision=None,
        )
        self._breadth_history: deque = deque(maxlen=40)
        self._day_state: dict = {}
        self._last_snap_dt: Optional[datetime] = None
        self._cooldown_dt: Optional[datetime] = None
        self._dir_cooldown_dt: Optional[datetime] = None

    def _select_regime(self, vix_val: float) -> dict:
        p = self.s.get("position")
        if p:
            if p.get("regime_profile") == "low_vix" and self._low_vix_c is not None:
                return self._low_vix_c
            return self._base_c
        if self._low_vix_c is not None and vix_val > 0 and vix_val < float(self._base_c["vix_min"]):
            return self._low_vix_c
        return self._base_c

    def effective_risk_limits(self) -> Tuple[float, float, float]:
        c, s = self.c, self.s
        if not c.get("dynamic_risk_scaling", True):
            return float(c["planned_risk"]), float(c["daily_loss_limit"]), float(c["daily_profit_lock"])
        init_w = max(1.0, float(c["initial_wallet"]))
        cash_scale = max(0.0, float(s["cash"])) / init_w
        day_scale = max(0.0, float(s["day_start"])) / init_w
        return (
            float(c["planned_risk"]) * cash_scale,
            float(c["daily_loss_limit"]) * day_scale,
            float(c["daily_profit_lock"]) * day_scale,
        )

    def budget(self, option: dict, stop_frac_override: Optional[float] = None) -> Tuple[int, float]:
        c, s = self.c, self.s
        entry = finite(option["ask"]) * (1.0 + c["slippage_fraction"])
        lot = int(option["lot"])
        if entry <= 0 or lot <= 0:
            return 0, entry
        sf = stop_frac_override if stop_frac_override is not None else c["stop_fraction"]
        stop_fill_est = max(0.05, entry * (1.0 - sf) * (1.0 - c["slippage_fraction"]))
        per_unit_risk = max(0.05, entry - stop_fill_est)
        fees = 2.0 * c["fee_per_side"]
        eff_planned, eff_daily_loss, _ = self.effective_risk_limits()

        headroom = s["cash"] - c["capital_floor"] - c["safety_buffer"]
        wallet_cap = s["cash"] * c["max_premium_loss_fraction"]
        max_deployable = min(headroom, wallet_cap, s["cash"])
        daily_loss_rem = min(eff_daily_loss, eff_daily_loss + s["cash"] - s["day_start"])
        risk_budget = min(eff_planned, daily_loss_rem)

        lots_cash = math.floor((max_deployable - fees) / (entry * lot)) if max_deployable > fees else 0
        lots_risk = math.floor((risk_budget - fees) / (per_unit_risk * lot)) if risk_budget > fees else 0
        cap_lots = int(c["max_lots"])
        if c.get("loss_streak_single_lot") and s.get("loss_streak", 0) >= 1:
            cap_lots = min(cap_lots, int(c.get("loss_streak_lot_cap", 1)))
        lots = max(0, min(cap_lots, lots_cash, lots_risk))
        return lots * lot, entry

    def _close(self, bid: float, now: datetime, reason: str) -> None:
        c, s = self.c, self.s
        p = s["position"]
        fill = max(0.0, bid * (1.0 - c["slippage_fraction"]))
        s["cash"] += fill * p["quantity"] - c["fee_per_side"]
        pnl = (fill - p["entry"]) * p["quantity"] - 2.0 * c["fee_per_side"]
        trade = dict(
            key=p["key"],
            kind=p["kind"],
            strike=p["strike"],
            setup=p.get("setup", ""),
            entered=p["entered"],
            exited=now.isoformat(),
            quantity=p["quantity"],
            entry=round(p["entry"], 4),
            exit=round(fill, 4),
            net_pnl=round(pnl, 2),
            charges=round(2.0 * c["fee_per_side"], 2),
            reason=reason,
        )
        s["trades"].append(trade)
        s["loss_streak"] = s["loss_streak"] + 1 if pnl < 0 else 0
        pos_dir = 1 if p["kind"] == "CE" else -1
        s["position"] = None
        cd_dt = now + timedelta(minutes=c["cooldown_minutes"])
        s["cooldown_until"] = cd_dt.isoformat()
        self._cooldown_dt = cd_dt
        if pnl < 0:
            s["last_loss_direction"] = pos_dir
            dcd_dt = now + timedelta(minutes=c["direction_cooldown_minutes"])
            s["direction_cooldown_until"] = dcd_dt.isoformat()
            self._dir_cooldown_dt = dcd_dt
        if self.persist and self.folder is not None:
            emit(self.folder / "audit.jsonl", dict(time=now.isoformat(), event="PAPER_EXIT", **trade))

    def step(self, snap: dict, now: Optional[datetime] = None, precomputed_features: Optional[dict] = None) -> dict:
        now = now or datetime.now(IST)
        validate_snapshot(snap, now, self._base_c)
        ts = stamp(snap["timestamp"])
        options = {o["key"]: o for o in snap["options"]}
        vix_val = float((snap.get("vix") or {}).get("value") or 0.0)
        if precomputed_features is not None:
            feat = precomputed_features
        else:
            feat = extract_market_features(snap, now, self._base_c, self._breadth_history, self._day_state)
        return self._step_core(
            snap_ts=snap["timestamp"],
            ts=ts,
            now=now,
            day=ts.date().isoformat(),
            clock=now.strftime("%H:%M"),
            vix_val=vix_val,
            options=options,
            feat=feat,
            fast_mode=False,
        )

    def fast_step(
        self,
        snap_ts: str,
        now: datetime,
        day: str,
        clock: str,
        vix_val: float,
        options: Dict[str, dict],
        feat: dict,
    ) -> None:
        """Ultra-fast replay step using pre-validated causal snapshots (100% identical execution math)."""
        self._step_core(
            snap_ts=snap_ts,
            ts=now,
            now=now,
            day=day,
            clock=clock,
            vix_val=vix_val,
            options=options,
            feat=feat,
            fast_mode=True,
        )

    def _step_core(
        self,
        snap_ts: str,
        ts: datetime,
        now: datetime,
        day: str,
        clock: str,
        vix_val: float,
        options: Dict[str, dict],
        feat: dict,
        fast_mode: bool = False,
    ) -> dict:
        self.c = self._select_regime(vix_val)
        c, s = self.c, self.s
        if self._last_snap_dt is not None and ts <= self._last_snap_dt:
            raise Blocked("Snapshot time must increase strictly")
        self._last_snap_dt = ts
        s["last_snapshot"] = snap_ts

        if s["day"] != day:
            if s["position"]:
                raise Blocked("Overnight paper position not allowed")
            s.update(
                day=day,
                day_start=s["cash"],
                trades_today=0,
                loss_streak=0,
                pending=None,
                last_loss_direction=None,
                direction_cooldown_until=None,
            )
            self._dir_cooldown_dt = None

        if day not in s["daily"]:
            s["daily"][day] = dict(
                date=day,
                start_equity=s["equity"],
                end_equity=s["equity"],
                snapshots=0,
            )
        daily = s["daily"][day]

        pending = s["pending"]
        if pending and (ts - pending["created_dt"]).total_seconds() > c["pending_ttl_seconds"]:
            s["pending"] = None

        skew = float(c.get("quote_clock_skew_seconds", 2.0))
        snap_ts_check = snap_ts if fast_mode else None

        # 1. Protective Exits FIRST
        p = s["position"]
        if p:
            o = options.get(p["key"])
            if not o:
                raise Blocked("Open position has no fresh quote")
            if not fast_mode and not _fresh(o.get("quote_time"), now, c["quote_max_age_seconds"], skew):
                raise Blocked("Open position has no fresh quote")
            bid = float(o["bid"])
            qty = p["quantity"]
            if bid <= 0 or float(o.get("bid_qty", 0)) < qty:
                raise Blocked("Open position lacks executable bid depth")

            _, eff_daily_loss, _ = self.effective_risk_limits()
            liq = s["cash"] + bid * (1.0 - c["slippage_fraction"]) * qty - c["fee_per_side"]
            risk_exit = (
                liq <= c["capital_floor"] + c["safety_buffer"]
                or s["day_start"] - liq >= eff_daily_loss
            )
            held_sec = (now - p["entered_dt"]).total_seconds()
            max_hold_min = p.get("max_hold_minutes", c["max_hold_minutes"])
            time_exit = held_sec >= max_hold_min * 60.0
            stop_file = (self.folder / "STOP").exists() if self.folder else False

            # Built-in early stall exit (when enabled in config or low-VIX regime)
            stall_exit = False
            stall_vix_cap = float(c.get("early_stall_max_vix", 0.0))
            stall_active = bool(c.get("early_stall_exit")) or (stall_vix_cap > 0.0 and 0 < vix_val < stall_vix_cap)
            if stall_active and held_sec >= 480.0 and p["entry"] > 0:
                mfe_frac = (p["high"] - p["entry"]) / p["entry"]
                cur_frac = (bid - p["entry"]) / p["entry"]
                if held_sec >= float(c.get("stall_minutes", 11.0)) * 60.0 and mfe_frac < 0.035 and cur_frac <= -0.025:
                    stall_exit = True
                elif cur_frac <= -0.035:
                    pos_dir = 1 if p["kind"] == "CE" else -1
                    if (pos_dir == 1 and feat["price"] < feat["e21"] and feat["macd_hist"] < 0) or (
                        pos_dir == -1 and feat["price"] > feat["e21"] and feat["macd_hist"] > 0
                    ):
                        stall_exit = True

            custom_exit = None
            if self.exit_fn is not None:
                custom_exit = self.exit_fn(p, o, feat, c)

            if bid <= p["stop"]:
                be_lvl = p["entry"] * (1.0 + p.get("be_lock", c["breakeven_lock_fraction"]) + 1e-5)
                reason = "TRAIL_STOP" if p["stop"] > be_lvl else ("BREAKEVEN_STOP" if p["stop"] >= p["entry"] else "STOP")
                self._close(bid, now, reason)
            elif bid >= p["target"]:
                self._close(bid, now, "TARGET")
            elif risk_exit:
                self._close(bid, now, "ACCOUNT_RISK_EXIT")
            elif stall_exit:
                self._close(bid, now, "EARLY_STALL_EXIT")
            elif custom_exit:
                self._close(bid, now, custom_exit)
            elif time_exit:
                self._close(bid, now, "MAX_HOLD_EXIT")
            elif clock >= c["force_exit_clock"] or stop_file:
                self._close(bid, now, "SESSION_OR_KILL")
            else:
                p["high"] = max(p["high"], bid)
                if p["high"] >= p["entry"] * (1.0 + p.get("be_act", c["breakeven_activation_fraction"])):
                    p["stop"] = max(p["stop"], p["entry"] * (1.0 + p.get("be_lock", c["breakeven_lock_fraction"])))
                if p["high"] >= p["entry"] * (1.0 + p.get("trail_act", c["trail_activation_fraction"])):
                    p["stop"] = max(p["stop"], p["high"] * (1.0 - p.get("trail_fr", c["trail_fraction"])))
                step_lvls = p.get("step_trail_levels", c.get("step_trail_levels")) or []
                if step_lvls and p["entry"] > 0:
                    gain_peak = (p["high"] - p["entry"]) / p["entry"]
                    for g_th, l_fr in step_lvls:
                        if gain_peak >= g_th:
                            p["stop"] = max(p["stop"], p["entry"] * (1.0 + l_fr))

        # 2. Evaluate Strategy Signal
        direction = 0
        setup_tag = "NONE"
        overrides: Optional[dict] = None
        if feat["vix_fresh"] and (c["vix_min"] <= feat["vix"] <= c["vix_max"]) and feat["breadth_ok"] and not feat["has_gap"]:
            direction, setup_tag, overrides = self.signal_fn(feat, s, c)

        if not fast_mode:
            s["last_decision"] = dict(direction=direction, setup=setup_tag, clock=clock, spot=feat["price"], vix=feat["vix"])

        # Cooldowns
        if s["capital_halted"]:
            direction = 0
        elif self._cooldown_dt is not None and ts < self._cooldown_dt:
            direction = 0
        elif (
            direction
            and s.get("last_loss_direction") == direction
            and self._dir_cooldown_dt is not None
            and ts < self._dir_cooldown_dt
        ):
            direction = 0

        # MTM equity
        p = s["position"]
        mtm = (
            max(0.0, float(options[p["key"]]["bid"])) * (1.0 - c["slippage_fraction"]) * p["quantity"] - c["fee_per_side"]
            if p
            else 0.0
        )
        equity = s["cash"] + mtm
        s["equity"] = equity
        if equity > s["peak_equity"]:
            s["peak_equity"] = equity
        dd = s["peak_equity"] - equity
        if dd > s["max_drawdown"]:
            s["max_drawdown"] = dd

        _, eff_daily_loss, eff_daily_profit = self.effective_risk_limits()
        stop_file = (self.folder / "STOP").exists() if self.folder else False
        halted = (
            s["capital_halted"]
            or equity <= c["capital_floor"] + c["safety_buffer"]
            or s["day_start"] - equity >= eff_daily_loss
            or s["cash"] - s["day_start"] >= eff_daily_profit
            or s["trades_today"] >= c["max_trades_day"]
            or s["loss_streak"] >= c["max_loss_streak"]
            or stop_file
            or day in c["blackout_dates"]
            or not (c["entry_start_clock"] <= clock < c["entry_end_clock"])
        )
        if halted:
            s["pending"] = None

        # 3. Two-Step Causal Fill (Pending on T -> Fill on T+1)
        pending = s["pending"]
        entered_now = False
        if pending and not s["position"] and not halted:
            s["pending"] = None
            o = options.get(pending["key"])
            ov = pending.get("overrides") or {}
            if o and direction == pending["direction"] and option_ok(o, now, c, snap_ts_check):
                sf = float(ov.get("stop_fraction", c["stop_fraction"]))
                tf = float(ov.get("target_fraction", c["target_fraction"]))
                qty, entry = self.budget(o, stop_frac_override=sf)
                if qty and min(o["ask_qty"], o["bid_qty"]) >= qty:
                    s["cash"] -= entry * qty + c["fee_per_side"]
                    s["position"] = dict(
                        key=o["key"],
                        kind=o["kind"],
                        strike=o["strike"],
                        setup=pending.get("setup", setup_tag),
                        quantity=qty,
                        entry=entry,
                        entered=now.isoformat(),
                        entered_dt=now,
                        stop=entry * (1.0 - sf),
                        target=entry * (1.0 + tf),
                        high=entry,
                        be_act=float(ov.get("breakeven_activation_fraction", c["breakeven_activation_fraction"])),
                        be_lock=float(ov.get("breakeven_lock_fraction", c["breakeven_lock_fraction"])),
                        trail_act=float(ov.get("trail_activation_fraction", c["trail_activation_fraction"])),
                        trail_fr=float(ov.get("trail_fraction", c["trail_fraction"])),
                        max_hold_minutes=float(ov.get("max_hold_minutes", c["max_hold_minutes"])),
                        step_trail_levels=ov.get("step_trail_levels", c.get("step_trail_levels")),
                        entry_spot=feat["price"],
                        regime_profile="low_vix" if (self._low_vix_c is not None and c is self._low_vix_c) else "normal",
                    )
                    s["trades_today"] += 1
                    entered_now = True
                    if self.persist and self.folder is not None:
                        emit(
                            self.folder / "audit.jsonl",
                            dict(
                                time=now.isoformat(),
                                event="PAPER_ENTRY",
                                key=o["key"],
                                strike=o["strike"],
                                kind=o["kind"],
                                quantity=qty,
                                entry=round(entry, 2),
                                setup=pending.get("setup", setup_tag),
                            ),
                        )

        # 4. Queue Pending Signal on Snapshot T
        if direction and not s["position"] and not halted and not entered_now:
            kind = "CE" if direction == 1 else "PE"
            ov = overrides or {}
            td = float(ov.get("target_delta", c["target_delta"]))
            sf = float(ov.get("stop_fraction", c["stop_fraction"]))
            cands = [o for o in options.values() if o["kind"] == kind and option_ok(o, now, c, snap_ts_check)]
            cands.sort(key=lambda o: abs(abs(o["greeks"]["delta"]) - td))
            for o in cands:
                qty, _ = self.budget(o, stop_frac_override=sf)
                if qty:
                    s["pending"] = dict(
                        key=o["key"],
                        direction=direction,
                        created=snap_ts,
                        created_dt=ts,
                        setup=setup_tag,
                        overrides=ov,
                    )
                    break

        if equity <= c["capital_floor"] + c["safety_buffer"]:
            s["capital_halted"] = True
            s["pending"] = None

        p = s["position"]
        mtm2 = (
            max(0.0, float(options[p["key"]]["bid"])) * (1.0 - c["slippage_fraction"]) * p["quantity"] - c["fee_per_side"]
            if p
            else 0.0
        )
        s["equity"] = s["cash"] + mtm2
        if s["equity"] > s["peak_equity"]:
            s["peak_equity"] = s["equity"]
        dd2 = s["peak_equity"] - s["equity"]
        if dd2 > s["max_drawdown"]:
            s["max_drawdown"] = dd2
        daily["end_equity"] = s["equity"]
        daily["snapshots"] += 1

        if self.persist and self.folder is not None:
            # Strip non-JSON datetime helper keys before saving
            s_save = copy.deepcopy(s)
            if s_save.get("position"):
                s_save["position"].pop("entered_dt", None)
            if s_save.get("pending"):
                s_save["pending"].pop("created_dt", None)
            atomic(self.folder / "practice_state.json", s_save)
        return {} if fast_mode else self.summary()

    def summary(self) -> dict:
        s = self.s
        trades = s["trades"]
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
        daily_rows = self.daily_rows()
        active_days = sum(1 for r in daily_rows if r["closed_trades"] >= 1)
        total_days = len(daily_rows)
        return dict(
            bot_id=self.bot_id,
            bot_name=self.bot_name,
            initial_wallet=round(s["initial_wallet"], 2),
            equity=round(s["equity"], 2),
            cash=round(s["cash"], 2),
            net_pnl=round(s["equity"] - s["initial_wallet"], 2),
            return_pct=round((s["equity"] - s["initial_wallet"]) / s["initial_wallet"] * 100.0, 2),
            closed_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
            profit_factor=round(pf, 3),
            max_drawdown=round(s["max_drawdown"], 2),
            observed_days=total_days,
            active_days=active_days,
            daily_coverage_pct=round(active_days / total_days * 100.0, 2) if total_days else 0.0,
            avg_trades_per_day=round(len(trades) / total_days, 3) if total_days else 0.0,
            capital_halted=s["capital_halted"],
        )

    def daily_rows(self) -> List[dict]:
        rows = []
        for day, d in sorted(self.s["daily"].items()):
            dt = [t for t in self.s["trades"] if stamp(t["exited"]).date().isoformat() == day]
            rows.append(
                dict(
                    date=day,
                    start_equity=round(d["start_equity"], 2),
                    end_equity=round(d["end_equity"], 2),
                    net_pnl=round(d["end_equity"] - d["start_equity"], 2),
                    closed_trades=len(dt),
                    wins=sum(1 for t in dt if t["net_pnl"] > 0),
                    losses=sum(1 for t in dt if t["net_pnl"] <= 0),
                )
            )
        return rows

    def write_reports(self, folder: Optional[Path] = None) -> dict:
        out = Path(folder) if folder else self.folder
        if out is None:
            return self.summary()
        out.mkdir(parents=True, exist_ok=True)
        drows = self.daily_rows()
        if drows:
            with (out / "daily_pnl.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(drows[0].keys()))
                w.writeheader()
                w.writerows(drows)
        tfields = ["key", "kind", "strike", "setup", "entered", "exited", "quantity", "entry", "exit", "net_pnl", "charges", "reason"]
        with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=tfields)
            w.writeheader()
            for t in self.s["trades"]:
                w.writerow({k: t.get(k, "") for k in tfields})
        summ = self.summary()
        atomic(out / "summary.json", summ)
        return summ


# ---------------------------------------------------------------------------
# Standard Standalone CLI Runner for Every Bot File
# ---------------------------------------------------------------------------

def run_bot_cli(
    bot_id: str,
    bot_name: str,
    description: str,
    config: Dict[str, Any],
    signal_fn: Callable[[dict, dict, dict], Tuple[int, str, Optional[dict]]],
    exit_fn: Optional[Callable[[dict, dict, dict, dict], Optional[str]]] = None,
) -> None:
    parser = argparse.ArgumentParser(description=f"{bot_name} — Live Upstox Paper Trading Bot")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run live practice trading on live Upstox NSE stream")
    p_run.add_argument("--wallet", type=float, default=50000.0, help="Starting wallet INR (default 50000)")
    p_run.add_argument("--output", default=f"live_run_{bot_id}", help="Output directory for live logs")

    p_rep = sub.add_parser("replay", help="Replay recorded snapshots.jsonl")
    p_rep.add_argument("--input", required=True, help="Path to snapshots.jsonl")
    p_rep.add_argument("--wallet", type=float, default=50000.0)
    p_rep.add_argument("--output", default=f"replay_{bot_id}")

    sub.add_parser("status", help="Show bot strategy summary & parameters")
    args = parser.parse_args()

    cfg = {**BASE_BOT_CONFIG, **config}
    if getattr(args, "wallet", None):
        cfg["initial_wallet"] = float(args.wallet)

    if args.cmd == "status" or args.cmd is None:
        print(
            json.dumps(
                {
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "description": description,
                    "mode": "LIVE_PAPER_PRACTICE",
                    "initial_wallet": cfg["initial_wallet"],
                    "capital_floor": cfg["capital_floor"],
                    "max_lots": cfg["max_lots"],
                    "max_trades_day": cfg["max_trades_day"],
                    "target_delta": cfg["target_delta"],
                    "stop_fraction": cfg["stop_fraction"],
                    "target_fraction": cfg["target_fraction"],
                    "entry_window": f"{cfg['entry_start_clock']} - {cfg['entry_end_clock']} IST",
                },
                indent=2,
            )
        )
        return

    if args.cmd == "replay":
        out = Path(args.output)
        engine = CompetitionBotEngine(bot_id, bot_name, cfg, signal_fn, exit_fn, folder=out, persist=False)
        cnt = 0
        with Path(args.input).open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                snap = json.loads(line)
                engine.step(snap, stamp(snap["timestamp"]))
                cnt += 1
        summ = engine.write_reports(out)
        print(f"Replayed {cnt} snapshots -> Net PnL: Rs {summ['net_pnl']:+,.2f} ({summ['return_pct']:+.2f}%) | Trades: {summ['closed_trades']}")
        return

    if args.cmd == "run":
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            if not sys.stdin.isatty():
                raise Blocked("Set UPSTOX_ACCESS_TOKEN environment variable.")
            token = getpass.getpass("Enter Upstox access token (hidden): ").strip()
            os.environ["UPSTOX_ACCESS_TOKEN"] = token

        running = True

        def _stop(signum, frame):
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        membership_path = Path(__file__).resolve().parent / "nifty_membership.json"
        api = LiveUpstoxAdapter(membership_path)
        out = Path(args.output)
        engine = CompetitionBotEngine(bot_id, bot_name, cfg, signal_fn, exit_fn, folder=out, persist=True)

        print(f"{BOLD}{BG_CYAN}  {bot_name} ({bot_id}) — LIVE PRACTICE TRADING  {RESET}")
        print(f"Wallet: Rs {cfg['initial_wallet']:,.2f} | Floor: Rs {cfg['capital_floor']:,.2f} | Output: {out}\n", flush=True)

        while running:
            now_dt = datetime.now(IST)
            try:
                st = api.get("/v2/market/status/NSE").get("status")
                if st == "NORMAL_OPEN":
                    print(f"{BG_GREEN}{BOLD} NSE NORMAL_OPEN ({now_dt.strftime('%H:%M:%S')} IST) — LIVE STREAM ACTIVE {RESET}", flush=True)
                    break
                print(f"[{now_dt.strftime('%H:%M:%S')} IST] Market status: {st}. Waiting for NORMAL_OPEN...", flush=True)
            except Exception as e:
                print(f"[{now_dt.strftime('%H:%M:%S')} IST] Retry: {e}", flush=True)
            time.sleep(5.0)

        poll_sec = float(cfg.get("poll_seconds", 2.5))
        prev_trades = len(engine.s["trades"])
        while running:
            now_dt = datetime.now(IST)
            if now_dt.strftime("%H:%M:%S") >= "15:30:00":
                break
            pinned = {engine.s["position"]["key"]} if engine.s.get("position") else None
            try:
                snap = api.snapshot(pinned_keys=pinned)
                engine.step(snap, stamp(snap["timestamp"]))
            except Exception as exc:
                time.sleep(3.0)
                continue

            if len(engine.s["trades"]) > prev_trades:
                lt = engine.s["trades"][-1]
                c_col = GREEN if lt["net_pnl"] >= 0 else RED
                print(
                    f"{c_col}[EXIT] {lt['key']} | Entry: {lt['entry']:.2f} -> Exit: {lt['exit']:.2f} | "
                    f"Net: Rs {lt['net_pnl']:+,.2f} | Reason: {lt['reason']} | Equity: Rs {engine.s['equity']:,.2f}{RESET}",
                    flush=True,
                )
                prev_trades = len(engine.s["trades"])
            time.sleep(poll_sec)

        summ = engine.write_reports(out)
        print(f"\n{BOLD}Session Complete: Net PnL Rs {summ['net_pnl']:+,.2f} ({summ['return_pct']:+.2f}%) | Trades: {summ['closed_trades']}{RESET}")
