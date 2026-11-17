"""Fast 1-minute + multi-second tick PRACTICE engine (read-only Upstox market data only)."""
from __future__ import annotations

from collections import deque
import copy
import csv
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import time
import urllib.parse

from core import (
    Blocked,
    DEFAULTS as BASE_DEFAULTS,
    INDEX,
    IST,
    PaperEngine,
    atomic,
    ema,
    finite,
    option_ok,
    stamp,
)
from market import EnrichedUpstox, _previous_close
from strategies import fresh, rsi

FAST_DEFAULTS = {
    **BASE_DEFAULTS,
    "engine_version": "0.2-fast",
    "timeframe": "1m+2.5s",
    "poll_seconds": 2.5,
    "capital_floor": 30000.0,
    "safety_buffer": 2000.0,
    "planned_risk": 2500.0,
    "daily_loss_limit": 4000.0,
    "daily_profit_lock": 1200.0,
    "dynamic_risk_scaling": True,
    "max_lots": 1,
    "target_delta": 0.58,
    "max_trades_day": 4,
    "max_loss_streak": 2,
    "stop_fraction": 0.10,
    "target_fraction": 0.21,
    "breakeven_activation_fraction": 0.07,
    "breakeven_lock_fraction": 0.025,
    "trail_activation_fraction": 0.10,
    "trail_fraction": 0.05,
    "max_spread_fraction": 0.02,
    "delta_min": 0.40,
    "delta_max": 0.65,
    "max_theta_fraction": 0.20,
    "max_gamma_move": 0.35,
    "max_vega_shock_fraction": 1.0,
    "rsi_overbought": 75.0,
    "rsi_oversold": 25.0,
    "bar_stale_seconds": 120,
    "quote_clock_skew_seconds": 2.0,
    "breadth_min_names": 45,
    "breadth_agreement": 0.55,
    "use_delta_breadth": True,
    "delta_breadth_agreement": 0.56,
    "delta_breadth_min_seconds": 900.0,
    "delta_breadth_max_seconds": 1500.0,
    "vix_min": 14.0,
    "vix_max": 35.0,
    "trend_efficiency_min": 0.25,
    "ema_sep_atr_min": 0.40,
    "morning_brk_enabled": True,
    "morning_brk_only_when_no_setup": False,
    "morning_brk_start": "09:45",
    "morning_brk_end": "11:45",
    "morning_brk_eff_min": 0.36,
    "morning_brk_esep_min": 0.25,
    "tick_trigger_pts": 5.0,
    "tick_window_seconds": 20,
    "tick_min_samples": 3,
    "entry_start_clock": "09:20",
    "entry_end_clock": "14:00",
    "force_exit_clock": "15:15",
    "pending_ttl_seconds": 30,
    "cooldown_minutes": 5,
    "direction_cooldown_minutes": 15,
    "max_hold_minutes": 45,
    "max_premium_loss_fraction": 0.45,
    "lunch_shield_enabled": False,
    "lunch_shield_start": "12:15",
    "lunch_shield_end": "13:05",
    "lunch_shield_eff_min": 0.40,
    "early_stall_exit": False,
    "early_stall_max_vix": 0.0,
    "stall_minutes": 11.0,
    "loss_streak_single_lot": False,
    "loss_streak_lot_cap": 1,
    "g_flag_enabled": False,
    "step_trail_levels": [],
    "event_windows": [],
    "low_vix_regime": None,
}


def _validate_fast_flat(c: dict) -> dict:
    for k, v in FAST_DEFAULTS.items():
        if k == "early_stall_max_vix":
            evx = finite(c[k])
            if evx < 0 or (evx > 0 and not 5.0 <= evx <= 40.0):
                raise Blocked("Invalid early_stall_max_vix bound")
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and finite(c[k]) <= 0:
            raise Blocked("Fast config must be positive: " + k)
    if not isinstance(c["dynamic_risk_scaling"], bool):
        raise Blocked("dynamic_risk_scaling must be a boolean")
    if not isinstance(c["use_delta_breadth"], bool):
        raise Blocked("use_delta_breadth must be a boolean")
    if not isinstance(c["morning_brk_enabled"], bool):
        raise Blocked("morning_brk_enabled must be a boolean")
    for bk in (
        "morning_brk_only_when_no_setup",
        "lunch_shield_enabled",
        "early_stall_exit",
        "loss_streak_single_lot",
        "g_flag_enabled",
    ):
        if not isinstance(c[bk], bool):
            raise Blocked(bk + " must be a boolean")
    if int(c["loss_streak_lot_cap"]) != c["loss_streak_lot_cap"] or int(c["loss_streak_lot_cap"]) < 1:
        raise Blocked("loss_streak_lot_cap must be a positive integer")
    if not 0 < c["lunch_shield_eff_min"] < 1:
        raise Blocked("Invalid fraction: lunch_shield_eff_min")
    if not c["lunch_shield_start"] < c["lunch_shield_end"]:
        raise Blocked("Invalid lunch shield clock bounds")
    if not isinstance(c["step_trail_levels"], list):
        raise Blocked("step_trail_levels must be a list")
    for lvl in c["step_trail_levels"]:
        if not isinstance(lvl, (list, tuple)) or len(lvl) != 2:
            raise Blocked("Each step_trail_levels entry must be [gain_thresh, lock_fraction]")
        g_th, l_fr = finite(lvl[0]), finite(lvl[1])
        if not 0 < l_fr < g_th < 5.0:
            raise Blocked("Invalid step_trail_levels bounds")
    if not 0.0 < float(c["target_delta"]) < 1.0:
        raise Blocked("target_delta must be between 0 and 1")
    if int(c["max_lots"]) != c["max_lots"] or int(c["max_lots"]) < 1:
        raise Blocked("max_lots must be a positive integer")
    if not 0 < c["capital_floor"] + c["safety_buffer"] < c["initial_wallet"]:
        raise Blocked("Invalid capital floor/buffer")
    if c["engine_version"] != "0.2-fast":
        raise Blocked("Wrong fast engine version")
    for k in (
        "stop_fraction",
        "target_fraction",
        "breakeven_activation_fraction",
        "breakeven_lock_fraction",
        "trail_activation_fraction",
        "trail_fraction",
        "slippage_fraction",
        "max_spread_fraction",
        "max_premium_loss_fraction",
        "trend_efficiency_min",
    ):
        if not 0 < c[k] < 1:
            raise Blocked("Invalid fraction: " + k)
    if c["breakeven_lock_fraction"] >= c["breakeven_activation_fraction"]:
        raise Blocked("breakeven_lock_fraction must be below breakeven_activation_fraction")
    if c["breakeven_activation_fraction"] > c["trail_activation_fraction"]:
        raise Blocked("breakeven_activation_fraction cannot exceed trail_activation_fraction")
    if not 0 < c["rsi_oversold"] < 50 < c["rsi_overbought"] < 100:
        raise Blocked("Invalid RSI bounds")
    if c["direction_cooldown_minutes"] < c["cooldown_minutes"]:
        raise Blocked("direction_cooldown_minutes cannot be less than cooldown_minutes")
    if not 0.5 < c["breadth_agreement"] <= 1:
        raise Blocked("Breadth agreement must exceed half")
    if not 0.5 < c["delta_breadth_agreement"] <= 1:
        raise Blocked("Delta breadth agreement must exceed half")
    if c["delta_breadth_min_seconds"] >= c["delta_breadth_max_seconds"]:
        raise Blocked("delta_breadth_min_seconds must be below delta_breadth_max_seconds")
    if not 0 < c["quote_clock_skew_seconds"] <= 10.0:
        raise Blocked("quote_clock_skew_seconds must be within (0, 10]")
    if not 1 <= c["breadth_min_names"] <= 50:
        raise Blocked("Invalid breadth coverage")
    if not 0 < c["delta_min"] < c["delta_max"] < 1 or not c["vix_min"] < c["vix_max"]:
        raise Blocked("Invalid exposure bounds")
    if not c["entry_start_clock"] < c["entry_end_clock"] <= c["force_exit_clock"]:
        raise Blocked("Invalid clock bounds")
    for event in c["event_windows"]:
        if stamp(event["start"]) >= stamp(event["end"]) or not event.get("label"):
            raise Blocked("Invalid event window")
    return c


def load_fast_config(path: str | Path | None = None) -> dict:
    c = copy.deepcopy(FAST_DEFAULTS)
    if path:
        supplied = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(supplied) - set(c)
        if unknown:
            raise Blocked("Unknown fast config keys: " + str(sorted(unknown)))
        c.update(supplied)
    _validate_fast_flat(c)
    lv = c.get("low_vix_regime")
    if lv is not None:
        if not isinstance(lv, dict):
            raise Blocked("low_vix_regime must be a dict or null")
        if "enabled" in lv and not isinstance(lv["enabled"], bool):
            raise Blocked("low_vix_regime.enabled must be a boolean")
        unknown_lv = (set(lv) - {"enabled"}) - (set(FAST_DEFAULTS) - {"low_vix_regime"})
        if unknown_lv:
            raise Blocked("Unknown low_vix_regime keys: " + str(sorted(unknown_lv)))
        merged = {**c, **{k: v for k, v in lv.items() if k != "enabled"}, "low_vix_regime": None}
        _validate_fast_flat(merged)
    return c


class FastUpstox(EnrichedUpstox):
    """Rate-budgeted 1-minute candle + real-time spot/option tick adapter. Read-only GETs only."""

    def __init__(self, membership_path: str | Path, futures_key: str | None = None):
        super().__init__(membership_path, futures_key=futures_key)
        self.extra_quote_keys = [INDEX] + self.extra_quote_keys
        self._contracts_day: str | None = None
        self._expiry: str | None = None
        self._meta: dict = {}
        self._status_cache: tuple[float, str] | None = None
        self._chain_cache: tuple[float, list] | None = None
        self._bars_cache: tuple[float, list] | None = None
        self.live_ticks: list[dict] = []

    def candles_1m(self, start: str | None = None, end: str | None = None) -> list[dict]:
        key = urllib.parse.quote(INDEX, safe="")
        path = f"/v3/historical-candle/intraday/{key}/minutes/1"
        if start:
            path = f"/v3/historical-candle/{key}/minutes/1/{end}/{start}"
        raw = self.get(path)["candles"]
        return [
            dict(
                timestamp=r[0],
                open=r[1],
                high=r[2],
                low=r[3],
                close=r[4],
                volume=r[5],
            )
            for r in sorted(raw, key=lambda r: stamp(r[0]))
        ]

    def _refresh_status(self, now_mono: float) -> str:
        if self._status_cache is None or (now_mono - self._status_cache[0]) >= 30.0:
            st = self.get("/v2/market/status/NSE")["status"]
            self._status_cache = (now_mono, st)
        return self._status_cache[1]

    def _refresh_contracts(self, today: date) -> tuple[str, dict]:
        day_s = today.isoformat()
        if self._contracts_day != day_s or not self._expiry:
            contracts = self.get("/v2/option/contract", {"instrument_key": INDEX})
            expiries = sorted(
                {
                    x["expiry"]
                    for x in contracts
                    if 2 <= (date.fromisoformat(x["expiry"]) - today).days <= 14
                }
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

    def _refresh_bars(self, begin: datetime, now_mono: float) -> list[dict]:
        need_fetch = self._bars_cache is None or (now_mono - self._bars_cache[0]) >= 10.0
        if not need_fetch and self._bars_cache is not None and self._bars_cache[1]:
            last_end = stamp(self._bars_cache[1][-1]["timestamp"]) + timedelta(minutes=1)
            # Fetch promptly once the active minute finishes + 2s grace.
            if (begin - last_end).total_seconds() >= 62.0:
                need_fetch = True
        if need_fetch:
            bars = self.candles_1m()
            if len(bars) < 180:
                prior = self.candles_1m(
                    (begin.date() - timedelta(days=5)).isoformat(),
                    (begin.date() - timedelta(days=1)).isoformat(),
                )
                first_cur = stamp(bars[0]["timestamp"]) if bars else None
                prior = [b for b in prior if first_cur is None or stamp(b["timestamp"]) < first_cur]
                bars = prior + bars
            self._bars_cache = (now_mono, bars)
        bars = [b for b in self._bars_cache[1] if stamp(b["timestamp"]) + timedelta(minutes=1) <= begin]
        return bars[-180:]

    def snapshot(self, pinned_keys: set[str] | None = None) -> dict:
        if stamp(self.membership["membership_as_of"]).date() != datetime.now(IST).date():
            raise Blocked("Refresh constituent membership with setup for this session")
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
                # Keep pinned open-position keys even if far from spot.
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
                self.get(
                    "/v2/market-quote/quotes",
                    {"instrument_key": ",".join(keys[offset : offset + 400])},
                )
            )
        self.last_quotes = quotes
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

        vix_q = bykey.get("NSE_INDEX|India VIX", {})
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


def validate_fast_snapshot(s: dict, now: datetime, c: dict) -> None:
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
    stamps = [stamp(b["timestamp"]) for b in bars]
    if stamps != sorted(set(stamps)):
        raise Blocked("Duplicate or unordered 1-minute candles")
    if stamps[-1] + timedelta(minutes=1) > ts:
        raise Blocked("Incomplete 1-minute candle / future information")
    if (ts - stamps[-1] - timedelta(minutes=1)).total_seconds() > c["bar_stale_seconds"]:
        raise Blocked("1-minute underlying candles are stale")
    for b in bars:
        o, h, l, cl = [finite(b[x]) for x in ("open", "high", "low", "close")]
        if l <= 0 or h < max(o, cl, l) or l > min(o, cl):
            raise Blocked("Malformed 1-minute OHLC")
    keys = [o["key"] for o in s["options"]]
    if len(keys) != len(set(keys)):
        raise Blocked("Duplicate option instrument keys")


def _sign(v: float) -> int:
    return 1 if v > 0 else -1 if v < 0 else 0


def _fast_fresh(value, now: datetime, seconds: float, skew_seconds: float = 0.0) -> bool:
    try:
        age = (now - stamp(value)).total_seconds()
        return -skew_seconds <= age <= seconds
    except (ValueError, TypeError, Blocked):
        return False


def _fast_option_ok(o: dict, now: datetime, c: dict) -> bool:
    try:
        qt = stamp(o["quote_time"])
        skew = float(c.get("quote_clock_skew_seconds", 2.0))
        eval_now = qt if 0 < (qt - now).total_seconds() <= skew else now
    except (KeyError, TypeError, ValueError, Blocked):
        eval_now = now
    return option_ok(o, eval_now, c)


def _fast_macd_series(closes: list[float]) -> list[float]:
    if len(closes) < 26:
        return [ema(closes[:i], 12) - ema(closes[:i], 26) for i in range(26, len(closes) + 1)]
    k12, k26 = 2.0 / 13.0, 2.0 / 27.0
    e12 = e26 = closes[0]
    for v in closes[1:25]:
        e12 += (v - e12) * k12
        e26 += (v - e26) * k26
    out = []
    for v in closes[25:]:
        e12 += (v - e12) * k12
        e26 += (v - e26) * k26
        out.append(e12 - e26)
    return out


def fast_breadth_evidence(s: dict, now: datetime, c: dict) -> tuple[int, str]:
    skew = float(c.get("quote_clock_skew_seconds", 2.0))
    b = s.get("breadth", {})
    if not _fast_fresh(b.get("timestamp"), now, c["quote_max_age_seconds"], skew):
        return 0, "BREADTH_MISSING_OR_STALE"
    rows = b.get("quotes", [])
    valid = []
    for q in rows:
        if not _fast_fresh(q.get("timestamp"), now, c["quote_max_age_seconds"], skew):
            continue
        last = finite(q["last"])
        ref = q.get("day_open")
        if ref is None or float(ref) <= 0:
            ref = q.get("previous_close")
        if ref is not None and finite(ref) > 0 and last > 0:
            valid.append((last, float(ref)))
    if len(valid) < c["breadth_min_names"]:
        return 0, "BREADTH_COVERAGE_TOO_LOW"
    up = sum(1 for last, ref in valid if last > ref) / len(valid)
    down = sum(1 for last, ref in valid if last < ref) / len(valid)
    direction = 1 if up >= c["breadth_agreement"] else -1 if down >= c["breadth_agreement"] else 0
    return direction, f"{len(valid)}/50 fresh vs_open; adv={up:.1%}, dec={down:.1%}"


def fast_positioning_evidence(s: dict, now: datetime, c: dict, ref_spot: float) -> tuple[int, str]:
    skew = float(c.get("quote_clock_skew_seconds", 2.0))
    paired = {}
    try:
        for o in s["options"]:
            if not _fast_fresh(o.get("quote_time"), now, c["quote_max_age_seconds"], skew):
                continue
            strike = finite(o["strike"])
            if abs(strike - ref_spot) > 500.0:
                continue
            oi = finite(o["oi"])
            if oi < 0:
                continue
            paired.setdefault((o["expiry"], strike), {})[o["kind"]] = oi
        complete = [(k, v) for k, v in paired.items() if "CE" in v and "PE" in v]
        if len(complete) < 5:
            return 0, "OI_NEEDS_FIVE_NEAR_STRIKES"
        calls = sum(v["CE"] for _, v in complete)
        puts = sum(v["PE"] for _, v in complete)
        if calls <= 0:
            return 0, "OI_INVALID_CALL_TOTAL"
        pcr = puts / calls
        direction = 1 if 1.02 <= pcr <= 2.50 else -1 if 0.35 <= pcr <= 0.98 else 0
        return direction, f"Near-ATM PCR={pcr:.3f} ({len(complete)} strikes)"
    except (ValueError, TypeError, KeyError, Blocked):
        return 0, "OI_MISSING_OR_INVALID"


def _rolling_delta_breadth(
    s: dict,
    now: datetime,
    c: dict,
    breadth_history: deque[tuple[datetime, dict[str, float]]] | None,
) -> tuple[int, str]:
    if not c.get("use_delta_breadth", True) or breadth_history is None:
        return 0, ""
    skew = float(c.get("quote_clock_skew_seconds", 2.0))
    if breadth_history and (breadth_history[-1][0].date() != now.date() or now < breadth_history[-1][0]):
        breadth_history.clear()
    cur_map: dict[str, float] = {}
    for q in s.get("breadth", {}).get("quotes", []):
        if not _fast_fresh(q.get("timestamp"), now, c["quote_max_age_seconds"], skew):
            continue
        last = finite(q["last"]) if q.get("last") is not None else 0.0
        if last > 0:
            cur_map[q["key"]] = last
    if len(cur_map) >= c["breadth_min_names"] and (
        not breadth_history or (now - breadth_history[-1][0]).total_seconds() >= 55.0
    ):
        breadth_history.append((now, cur_map))
    min_age = float(c.get("delta_breadth_min_seconds", 900.0))
    max_age = float(c.get("delta_breadth_max_seconds", 1500.0))
    ref_map = None
    ref_age = 0.0
    for t_old, mp in breadth_history:
        age = (now - t_old).total_seconds()
        if min_age <= age <= max_age:
            ref_map = mp
            ref_age = age
            break
    if not ref_map or len(cur_map) < c["breadth_min_names"]:
        return 0, ""
    common = [k for k in cur_map if k in ref_map and ref_map[k] > 0]
    if len(common) < c["breadth_min_names"]:
        return 0, ""
    up = sum(1 for k in common if cur_map[k] > ref_map[k]) / len(common)
    down = sum(1 for k in common if cur_map[k] < ref_map[k]) / len(common)
    thr = float(c.get("delta_breadth_agreement", 0.56))
    d = 1 if up >= thr else -1 if down >= thr else 0
    return d, f"delta({int(round(ref_age / 60.0))}m) adv={up:.1%}, dec={down:.1%}"


def evaluate_fast(
    s: dict,
    c: dict,
    breadth_history: deque[tuple[datetime, dict[str, float]]] | None = None,
) -> dict:
    now = stamp(s["timestamp"])
    skew = float(c.get("quote_clock_skew_seconds", 2.0))
    bars = s["bars"]
    closes = [finite(b["close"]) for b in bars]
    price, prev = closes[-1], closes[-2]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    prev_ema9 = ema(closes[:-1], 9)
    macd_series = _fast_macd_series(closes)
    macd_hist = macd_series[-1] - ema(macd_series, 9)
    roc3 = price / closes[-4] - 1.0
    don_high = max(finite(b["high"]) for b in bars[-11:-1])
    don_low = min(finite(b["low"]) for b in bars[-11:-1])
    rsi_now = rsi(closes)
    rsi_prev = rsi(closes[:-1])
    rsi_prev2 = rsi(closes[:-2])

    sig_map = {
        "ema_trend_1m": 1 if price > ema9 > ema21 else -1 if price < ema9 < ema21 else 0,
        "macd_1m": 1 if macd_series[-1] > 0 and macd_hist > 0 else -1 if macd_series[-1] < 0 and macd_hist < 0 else 0,
        "roc_3m": _sign(roc3) if abs(roc3) > 0.0005 else 0,
        "donchian_10m": 1 if price > don_high else -1 if price < don_low else 0,
        "ema_pullback_1m": (
            1
            if ema9 > ema21 and prev <= prev_ema9 and price > ema9
            else -1
            if ema9 < ema21 and prev >= prev_ema9 and price < ema9
            else 0
        ),
        "rsi_1m": (
            1
            if (54.0 < rsi_now <= c["rsi_overbought"] and rsi_now > rsi_prev2) or (rsi_prev < 32.0 <= rsi_now)
            else -1
            if (c["rsi_oversold"] <= rsi_now < 46.0 and rsi_now < rsi_prev2) or (rsi_prev > 68.0 >= rsi_now)
            else 0
        ),
    }

    # Live sub-minute / multi-second tick momentum (strictly causal ticks <= now)
    spot_live = s.get("spot_live", {})
    live_ltp = spot_live.get("ltp")
    ref_spot = finite(live_ltp) if live_ltp is not None else price
    ticks = [
        t
        for t in s.get("ticks", [])
        if _fast_fresh(t.get("timestamp"), now, c["tick_window_seconds"] * 2, skew)
        and (stamp(t["timestamp"]) - now).total_seconds() <= skew
    ]
    tick_dir = 0
    tick_delta = 0.0
    if len(ticks) >= c["tick_min_samples"]:
        cutoff = now - timedelta(seconds=c["tick_window_seconds"])
        window = [t for t in ticks if stamp(t["timestamp"]) >= cutoff]
        if len(window) < c["tick_min_samples"]:
            window = ticks[-c["tick_min_samples"] :]
        span = (stamp(window[-1]["timestamp"]) - stamp(window[0]["timestamp"])).total_seconds()
        if span >= 4.0:
            tick_delta = finite(window[-1]["ltp"]) - finite(window[0]["ltp"])
            last_step = finite(window[-1]["ltp"]) - finite(window[-2]["ltp"])
            # Guard against post-close / settlement single-tick spikes
            if abs(last_step) <= 25.0 and abs(tick_delta) <= 45.0:
                if tick_delta >= c["tick_trigger_pts"] and last_step >= 0:
                    tick_dir = 1
                elif tick_delta <= -c["tick_trigger_pts"] and last_step <= 0:
                    tick_dir = -1

    # Live intra-minute breakout vs recent 5 completed 1m candles
    hi5 = max(finite(b["high"]) for b in bars[-5:])
    lo5 = min(finite(b["low"]) for b in bars[-5:])
    live_brk = 1 if ref_spot >= hi5 + 2.0 else -1 if ref_spot <= lo5 - 2.0 else 0

    ups_1m = sum(1 for v in sig_map.values() if v == 1)
    downs_1m = sum(1 for v in sig_map.values() if v == -1)

    # Causal 5-minute MTF aggregation strictly from completed 5-minute buckets inside s["bars"]
    grouped_5m: dict[tuple[str, int], list[dict]] = {}
    for b in bars:
        bt = stamp(b["timestamp"])
        bk = (bt.date().isoformat(), (bt.hour * 60 + bt.minute) // 5)
        grouped_5m.setdefault(bk, []).append(b)
    bars_5m = [
        dict(
            open=finite(bs[0]["open"]),
            high=max(finite(x["high"]) for x in bs),
            low=min(finite(x["low"]) for x in bs),
            close=finite(bs[-1]["close"]),
        )
        for bk in sorted(grouped_5m)
        for bs in (grouped_5m[bk],)
        if len(bs) == 5
    ]
    trend_5m = 0
    trend_5m_fast = 0
    ema9_5m = price
    ema21_5m = price
    eff_5m = 0.0
    esep_5m = 0.0
    esep_5m_fast = 0.0
    if len(bars_5m) >= 10:
        closes_5m = [x["close"] for x in bars_5m]
        ema5_5m = ema(closes_5m, 5)
        ema9_5m = ema(closes_5m, min(9, len(closes_5m)))
        ema13_5m = ema(closes_5m, min(13, len(closes_5m)))
        ema21_5m = ema(closes_5m, min(21, len(closes_5m)))
        trend_5m = 1 if closes_5m[-1] > ema9_5m > ema21_5m else -1 if closes_5m[-1] < ema9_5m < ema21_5m else 0
        trend_5m_fast = 1 if closes_5m[-1] > ema5_5m > ema13_5m else -1 if closes_5m[-1] < ema5_5m < ema13_5m else 0
        eff_win = min(12, len(closes_5m) - 1)
        net_move = abs(closes_5m[-1] - closes_5m[-1 - eff_win])
        path_sum = sum(abs(y - x) for x, y in zip(closes_5m[-1 - eff_win : -1], closes_5m[-eff_win:]))
        eff_5m = net_move / path_sum if path_sum > 1e-9 else 0.0
        trs_5m = [
            max(x["high"] - x["low"], abs(x["high"] - p["close"]), abs(x["low"] - p["close"]))
            for p, x in zip(bars_5m[:-1], bars_5m[1:])
        ][-14:]
        atr_5m = sum(trs_5m) / len(trs_5m) if trs_5m else 0.0
        esep_5m = abs(ema9_5m - ema21_5m) / atr_5m if atr_5m > 0 else 0.0
        esep_5m_fast = abs(ema5_5m - ema13_5m) / atr_5m if atr_5m > 0 else 0.0

    # Causal 15-minute higher-timeframe bias strictly from completed 15m buckets inside s["bars"]
    grouped_15m: dict[tuple[str, int], list[dict]] = {}
    for b in bars:
        bt = stamp(b["timestamp"])
        bk15 = (bt.date().isoformat(), (bt.hour * 60 + bt.minute) // 15)
        grouped_15m.setdefault(bk15, []).append(b)
    closes_15m = [
        finite(bs[-1]["close"])
        for bk15 in sorted(grouped_15m)
        for bs in (grouped_15m[bk15],)
        if len(bs) == 15
    ]
    bias_15m = 0
    if len(closes_15m) >= 4:
        e4_15 = ema(closes_15m, min(4, len(closes_15m)))
        e10_15 = ema(closes_15m, min(10, len(closes_15m)))
        bias_15m = 1 if closes_15m[-1] > e4_15 > e10_15 else -1 if closes_15m[-1] < e4_15 < e10_15 else 0

    day_prefix = now.date().isoformat()
    today_tps = [
        (finite(b["high"]) + finite(b["low"]) + finite(b["close"])) / 3.0
        for b in bars
        if b["timestamp"].startswith(day_prefix)
    ]
    if not today_tps:
        today_tps = [(finite(b["high"]) + finite(b["low"]) + finite(b["close"])) / 3.0 for b in bars[-60:]]
    twap_day = sum(today_tps) / len(today_tps)
    high20 = max(finite(b["high"]) for b in bars[-21:-1])
    low20 = min(finite(b["low"]) for b in bars[-21:-1])

    breadth_dir, breadth_detail = fast_breadth_evidence(s, now, c)
    delta_breadth_dir, delta_breadth_detail = _rolling_delta_breadth(s, now, c, breadth_history)
    pcr_dir, pcr_detail = fast_positioning_evidence(s, now, c, ref_spot)

    vetoes: list[str] = []
    # Intraday 1m contiguity check
    for a, b in zip(bars, bars[1:]):
        ta, tb = stamp(a["timestamp"]), stamp(b["timestamp"])
        if ta.date() == tb.date() and tb - ta != timedelta(minutes=1):
            vetoes.append("INTRADAY_1M_BAR_GAP")
            break

    if breadth_detail.startswith("BREADTH_"):
        vetoes.append(breadth_detail)
    vix = s.get("vix", {})
    if not _fast_fresh(vix.get("timestamp"), now, c["quote_max_age_seconds"], skew):
        vetoes.append("VIX_MISSING_OR_STALE")
    elif not c["vix_min"] <= finite(vix["value"]) <= c["vix_max"]:
        vetoes.append("VIX_OUTSIDE_LIMITS")
    for event in c["event_windows"]:
        if stamp(event["start"]) <= now <= stamp(event["end"]):
            vetoes.append("EVENT_BLACKOUT:" + event["label"])
    hard_vetoes = list(vetoes)

    proposed = 0
    mode_tag = "NONE"
    # Setup A: >= 2 net 1-minute signals and live tick tape not opposing
    if ups_1m >= 2 and downs_1m <= 1 and (ups_1m - downs_1m) >= 2 and tick_dir != -1:
        proposed = 1
        mode_tag = "1M_MOMENTUM"
    elif downs_1m >= 2 and ups_1m <= 1 and (downs_1m - ups_1m) >= 2 and tick_dir != 1:
        proposed = -1
        mode_tag = "1M_MOMENTUM"

    # Setup B: Fast multi-second tick thrust + 1m/intra-bar breakout confirmation
    if proposed == 0 and tick_dir == 1 and (live_brk == 1 or ups_1m >= 1) and downs_1m == 0:
        proposed = 1
        mode_tag = "FAST_TICK_BREAKOUT"
    elif proposed == 0 and tick_dir == -1 and (live_brk == -1 or downs_1m >= 1) and ups_1m == 0:
        proposed = -1
        mode_tag = "FAST_TICK_BREAKOUT"

    eff_breadth_dir = breadth_dir
    if not proposed:
        vetoes.append("NO_FAST_SETUP")
    else:
        if len(bars_5m) < 10:
            vetoes.append("INSUFFICIENT_5M_BARS")
        else:
            if trend_5m != proposed:
                vetoes.append("MTF_5M_TREND_MISALIGNED")
            if eff_5m < c["trend_efficiency_min"]:
                vetoes.append("LOW_5M_TREND_EFFICIENCY")
            if esep_5m < c["ema_sep_atr_min"]:
                vetoes.append("LOW_5M_EMA_SEPARATION")
        # Reject single opposing breadth as well as double breadth+OI conflict,
        # unless rolling 15-25m breadth flow confirms an intraday reversal in the setup direction.
        if delta_breadth_dir and delta_breadth_dir == proposed:
            eff_breadth_dir = proposed
        if eff_breadth_dir == -proposed:
            vetoes.append("COUNTER_BREADTH_VETO")
        if (proposed == 1 and eff_breadth_dir == -1 and pcr_dir == -1) or (
            proposed == -1 and eff_breadth_dir == 1 and pcr_dir == 1
        ):
            vetoes.append("DOUBLE_COUNTER_TREND_VETO")
        # Block entering directly into 1-minute RSI exhaustion
        if proposed == 1 and rsi_now > c["rsi_overbought"]:
            vetoes.append("RSI_OVERBOUGHT")
        elif proposed == -1 and rsi_now < c["rsi_oversold"]:
            vetoes.append("RSI_OVERSOLD")

    # Setup C: Morning TWAP + 20m Breakout (09:45-11:45) catches early institutional trends
    # before slow 5m EMA21 crosses, verified by fast 5m EMA(5,13), 15m non-opposition, 15m delta breadth & PCR.
    if (
        vetoes
        and not hard_vetoes
        and (not c.get("morning_brk_only_when_no_setup") or proposed == 0)
        and c.get("morning_brk_enabled", True)
        and c.get("morning_brk_start", "09:45") <= now.strftime("%H:%M") < c.get("morning_brk_end", "11:45")
        and len(bars_5m) >= 10
        and eff_5m >= c.get("morning_brk_eff_min", 0.36)
        and esep_5m_fast >= c.get("morning_brk_esep_min", 0.25)
    ):
        brk_dir = 0
        if (
            ups_1m >= 3
            and price > twap_day
            and price > high20
            and trend_5m_fast == 1
            and bias_15m != -1
            and delta_breadth_dir == 1
            and pcr_dir == 1
            and rsi_now <= 73.0
        ):
            brk_dir = 1
        elif (
            downs_1m >= 3
            and price < twap_day
            and price < low20
            and trend_5m_fast == -1
            and bias_15m != 1
            and delta_breadth_dir == -1
            and pcr_dir == -1
            and rsi_now >= 27.0
        ):
            brk_dir = -1
        if brk_dir:
            proposed = brk_dir
            mode_tag = "MORNING_TWAP_BRK"
            vetoes = []
            eff_breadth_dir = brk_dir

    # Setup D ([g] Jakeschincariol/day-trader-skill): Bull/Bear Flag Continuation
    if (
        c.get("g_flag_enabled")
        and not hard_vetoes
        and proposed == 0
        and len(bars) >= 25
        and len(bars_5m) >= 10
    ):
        imp = (closes[-6] - closes[-16]) / max(1.0, closes[-16])
        cons_rng = (max(closes[-6:-1]) - min(closes[-6:-1])) / max(1.0, closes[-6])
        flag_dir = 0
        if imp >= 0.0018 and cons_rng <= 0.0015 and price > max(closes[-6:-1]):
            flag_dir = 1
        elif imp <= -0.0018 and cons_rng <= 0.0015 and price < min(closes[-6:-1]):
            flag_dir = -1
        eff_br_g = delta_breadth_dir if delta_breadth_dir else breadth_dir
        if (
            flag_dir
            and trend_5m == flag_dir
            and eff_br_g == flag_dir
            and ((flag_dir == 1 and rsi_now <= 73.0) or (flag_dir == -1 and rsi_now >= 27.0))
        ):
            proposed = flag_dir
            mode_tag = "G_FLAG_BRK"
            vetoes = []
            eff_breadth_dir = eff_br_g

    if (
        not vetoes
        and proposed
        and c.get("lunch_shield_enabled")
        and c["lunch_shield_start"] <= now.strftime("%H:%M") <= c["lunch_shield_end"]
    ):
        shield_br = delta_breadth_dir if delta_breadth_dir == proposed else breadth_dir
        if not (eff_5m >= float(c.get("lunch_shield_eff_min", 0.40)) and shield_br == proposed):
            vetoes.append("LUNCH_CHOP_SHIELD")

    if delta_breadth_detail:
        breadth_detail = f"{breadth_detail}; {delta_breadth_detail}"

    return dict(
        direction=0 if vetoes else proposed,
        proposed_direction=proposed,
        setup_type=mode_tag,
        regime="mtf_5m_1m_tick",
        features=dict(
            spot_live=ref_spot,
            bar_close_1m=price,
            ema9=round(ema9, 2),
            ema21=round(ema21, 2),
            ema9_5m=round(ema9_5m, 2),
            ema21_5m=round(ema21_5m, 2),
            trend_eff_5m=round(eff_5m, 3),
            ema_sep_atr_5m=round(esep_5m, 3),
            rsi_1m=round(rsi_now, 2),
            roc_3m_pct=round(roc3 * 100, 4),
            tick_delta_pts=round(tick_delta, 2),
            tick_samples=len(ticks),
            live_breakout_5m=live_brk,
        ),
        signals=sig_map,
        confirmations=dict(
            bars_1m=1 if ups_1m > downs_1m and ups_1m >= 2 else -1 if downs_1m > ups_1m and downs_1m >= 2 else 0,
            bars_5m=trend_5m,
            ticks_s=tick_dir,
            breadth=eff_breadth_dir,
            options_positioning=pcr_dir,
        ),
        evidence_details=dict(
            mtf_5m=f"trend5={trend_5m}, eff5={eff_5m:.3f}, esep5={esep_5m:.3f} ({len(bars_5m)}x5m)",
            breadth=breadth_detail,
            options_positioning=pcr_detail,
            tick_window=f"delta={tick_delta:+.2f}pts over {len(ticks)} ticks",
        ),
        vetoes=list(dict.fromkeys(vetoes)),
        status="PRACTICE_FAST_SCALP_NOT_PROFIT_PROVEN",
    )


_DEFAULT_EVALUATE_FAST = evaluate_fast


class QuickPracticeEngine(PaperEngine):
    """High-responsiveness 1m + multi-second PRACTICE paper engine."""

    def __init__(self, c: dict, folder: str | Path):
        super().__init__(c, folder)
        for key, val in dict(
            daily={},
            capital_halted=False,
            cooldown_until=None,
            last_loss_direction=None,
            direction_cooldown_until=None,
            dataset_source=None,
            last_decision=None,
            gap_warnings=0,
            sample_last_time=None,
        ).items():
            self.s.setdefault(key, val)
        self._staging = False
        self._events: list = []
        self._breadth_history: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=40)
        self._base_c = copy.deepcopy(c)
        lv = c.get("low_vix_regime")
        if isinstance(lv, dict) and lv.get("enabled"):
            self._low_vix_c = {**self._base_c, **{k: v for k, v in lv.items() if k != "enabled"}, "low_vix_regime": None}
        else:
            self._low_vix_c = None

    def _select_regime_config(self, snap: dict) -> dict:
        p = self.s.get("position")
        if p:
            if p.get("regime_profile") == "low_vix" and self._low_vix_c is not None:
                return self._low_vix_c
            return self._base_c
        if self._low_vix_c is not None:
            vix_val = finite((snap.get("vix") or {}).get("value"))
            if vix_val < float(self._base_c["vix_min"]):
                return self._low_vix_c
        return self._base_c

    def save(self):
        if not self._staging:
            super().save()

    def log(self, kind, now, **data):
        if self._staging:
            self._events.append((kind, now, data))
        else:
            super().log(kind, now, **data)

    def effective_risk_limits(self) -> tuple[float, float, float]:
        """Scale risk caps proportionally to wallet equity when dynamic_risk_scaling is enabled."""
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

    def budget(self, option):
        c, s = self.c, self.s
        entry = finite(option["ask"]) * (1.0 + c["slippage_fraction"])
        lot = int(option["lot"])
        if entry <= 0 or lot <= 0:
            return 0, entry
        stop_fill_est = max(0.05, entry * (1.0 - c["stop_fraction"]) * (1.0 - c["slippage_fraction"]))
        per_unit_risk = max(0.05, entry - stop_fill_est)
        fees = 2.0 * c["fee_per_side"]
        eff_planned_risk, eff_daily_loss_limit, _ = self.effective_risk_limits()

        headroom = s["cash"] - c["capital_floor"] - c["safety_buffer"]
        wallet_cap = s["cash"] * c["max_premium_loss_fraction"]
        max_deployable = min(headroom, wallet_cap, s["cash"])
        daily_loss_remaining = min(
            eff_daily_loss_limit,
            eff_daily_loss_limit + s["cash"] - s["day_start"],
        )
        risk_budget = min(eff_planned_risk, daily_loss_remaining)

        lots_by_cash = math.floor((max_deployable - fees) / (entry * lot)) if max_deployable > fees else 0
        lots_by_risk = math.floor((risk_budget - fees) / (per_unit_risk * lot)) if risk_budget > fees else 0
        cap_lots = int(c["max_lots"])
        if c.get("loss_streak_single_lot") and s.get("loss_streak", 0) >= 1:
            cap_lots = min(cap_lots, int(c.get("loss_streak_lot_cap", 1)))
        lots = max(0, min(cap_lots, lots_by_cash, lots_by_risk))
        return lots * lot, entry

    def close(self, price, now, reason):
        pos_dir = 1 if (self.s.get("position") or {}).get("kind") == "CE" else -1
        super().close(price, now, reason)
        self.s["cooldown_until"] = (now + timedelta(minutes=self.c["cooldown_minutes"])).isoformat()
        if self.s["trades"] and self.s["trades"][-1]["net_pnl"] < 0:
            self.s["last_loss_direction"] = pos_dir
            self.s["direction_cooldown_until"] = (
                now + timedelta(minutes=self.c["direction_cooldown_minutes"])
            ).isoformat()

    def step(self, snap: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now(IST)
        validate_fast_snapshot(snap, now, self._base_c)
        self.c = self._select_regime_config(snap)
        c, s = self.c, self.s
        ts = stamp(snap["timestamp"])
        if s["last_snapshot"] and ts <= stamp(s["last_snapshot"]):
            raise Blocked("Snapshot time must increase strictly")
        source = snap.get("source")
        if source not in ("upstox-fast-1m-v02", "synthetic-fast-test-only"):
            raise Blocked("Unsupported fast source schema")
        if s["dataset_source"] and source != s["dataset_source"]:
            raise Blocked("Cannot mix real-feed and synthetic fast runs")

        original = copy.deepcopy(s)
        self._staging = True
        self._events = []
        committed = False
        try:
            s["last_snapshot"] = snap["timestamp"]
            s["dataset_source"] = source
            day = ts.date().isoformat()
            clock = now.strftime("%H:%M")

            if s["day"] != day:
                if s["position"]:
                    raise Blocked("Overnight paper position requires reconciliation")
                s.update(
                    day=day,
                    day_start=s["cash"],
                    trades_today=0,
                    loss_streak=0,
                    pending=None,
                    last_loss_direction=None,
                    direction_cooldown_until=None,
                )

            if day not in s["daily"]:
                s["daily"][day] = dict(
                    date=day,
                    start_equity=s["equity"],
                    end_equity=s["equity"],
                    snapshots=0,
                    first_snapshot=snap["timestamp"],
                    last_snapshot=snap["timestamp"],
                    max_gap_seconds=0.0,
                )
            daily = s["daily"][day]
            if daily["snapshots"]:
                gap = (ts - stamp(daily["last_snapshot"])).total_seconds()
                daily["max_gap_seconds"] = max(daily["max_gap_seconds"], gap)
                if gap > c["snapshot_max_age_seconds"]:
                    s["gap_warnings"] += 1

            pending = s["pending"]
            if pending and (
                not pending.get("created")
                or (ts - stamp(pending["created"])).total_seconds() > c["pending_ttl_seconds"]
            ):
                s["pending"] = None

            options = {o["key"]: o for o in snap["options"]}

            # Evaluate protective exits FIRST on every snapshot
            skew = float(c.get("quote_clock_skew_seconds", 2.0))
            p = s["position"]
            if p:
                o = options.get(p["key"])
                if not o or not o.get("quote_time") or not _fast_fresh(o.get("quote_time"), now, c["quote_max_age_seconds"], skew):
                    raise Blocked("Open position has no fresh quote")
                bid = finite(o["bid"])
                qty = p["quantity"]
                if bid <= 0 or finite(o.get("bid_qty", 0)) < qty:
                    raise Blocked("Open position lacks executable bid depth")
                _, eff_daily_loss_limit, _ = self.effective_risk_limits()
                liquidation = s["cash"] + bid * (1.0 - c["slippage_fraction"]) * qty - c["fee_per_side"]
                risk_exit = (
                    liquidation <= c["capital_floor"] + c["safety_buffer"]
                    or s["day_start"] - liquidation >= eff_daily_loss_limit
                )
                held_sec = (now - stamp(p["entered"])).total_seconds()
                time_exit = held_sec >= c["max_hold_minutes"] * 60
                stall_vix_cap = float(c.get("early_stall_max_vix", 0.0))
                stall_active = bool(c.get("early_stall_exit")) or (
                    stall_vix_cap > 0.0 and finite((snap.get("vix") or {}).get("value")) < stall_vix_cap
                )
                stall_exit = False
                if stall_active and held_sec >= 8 * 60:
                    mfe_frac = (p["high"] - p["entry"]) / p["entry"]
                    cur_frac = (bid - p["entry"]) / p["entry"]
                    if held_sec >= float(c.get("stall_minutes", 11.0)) * 60 and mfe_frac < 0.035 and cur_frac <= -0.025:
                        stall_exit = True
                    elif cur_frac <= -0.035:
                        b_closes = [finite(b["close"]) for b in snap["bars"]]
                        b_ema21 = ema(b_closes, 21)
                        b_macd = _fast_macd_series(b_closes)
                        b_hist = b_macd[-1] - ema(b_macd, 9)
                        pos_dir = 1 if p["kind"] == "CE" else -1
                        if (pos_dir == 1 and b_closes[-1] < b_ema21 and b_hist < 0) or (
                            pos_dir == -1 and b_closes[-1] > b_ema21 and b_hist > 0
                        ):
                            stall_exit = True
                stop_file = (self.folder / "STOP").exists()
                if bid <= p["stop"]:
                    if p["stop"] > p["entry"] * (1.0 + c["breakeven_lock_fraction"] + 1e-5):
                        stop_reason = "TRAIL_STOP"
                    elif p["stop"] >= p["entry"]:
                        stop_reason = "BREAKEVEN_STOP"
                    else:
                        stop_reason = "STOP"
                    self.close(bid, now, stop_reason)
                elif bid >= p["target"]:
                    self.close(bid, now, "TARGET")
                elif risk_exit:
                    self.close(bid, now, "ACCOUNT_RISK_EXIT")
                elif stall_exit:
                    self.close(bid, now, "EARLY_STALL_EXIT")
                elif time_exit:
                    self.close(bid, now, "MAX_HOLD_EXIT")
                elif clock >= c["force_exit_clock"] or stop_file:
                    self.close(bid, now, "SESSION_OR_KILL")
                else:
                    p["high"] = max(p["high"], bid)
                    if p["high"] >= p["entry"] * (1.0 + c["breakeven_activation_fraction"]):
                        p["stop"] = max(p["stop"], p["entry"] * (1.0 + c["breakeven_lock_fraction"]))
                    if p["high"] >= p["entry"] * (1.0 + c["trail_activation_fraction"]):
                        p["stop"] = max(p["stop"], p["high"] * (1.0 - c["trail_fraction"]))
                    if c.get("step_trail_levels") and p["entry"] > 0:
                        gain_peak = (p["high"] - p["entry"]) / p["entry"]
                        for g_th, l_fr in c["step_trail_levels"]:
                            if gain_peak >= g_th:
                                p["stop"] = max(p["stop"], p["entry"] * (1.0 + l_fr))

            try:
                if evaluate_fast is _DEFAULT_EVALUATE_FAST:
                    report = evaluate_fast(snap, c, breadth_history=self._breadth_history)
                else:
                    report = evaluate_fast(snap, c)
            except (Blocked, KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                report = dict(
                    direction=0,
                    proposed_direction=0,
                    setup_type="NONE",
                    regime="fast_1m_tick",
                    features={},
                    signals={},
                    confirmations={},
                    evidence_details={},
                    vetoes=["INVALID_ENTRY_EVIDENCE:" + type(exc).__name__],
                    status="PRACTICE_FAST_SCALP_NOT_PROFIT_PROVEN",
                )
            s["last_decision"] = report
            direction = report["direction"]
            cooldown_reason = None
            if s["capital_halted"]:
                direction = 0
                cooldown_reason = "CAPITAL_FLOOR_HALT"
            elif s["cooldown_until"] and ts < stamp(s["cooldown_until"]):
                direction = 0
                cooldown_reason = "SESSION_OR_COOLDOWN_OR_HALT"
            elif (
                direction
                and s.get("last_loss_direction") == direction
                and s.get("direction_cooldown_until")
                and ts < stamp(s["direction_cooldown_until"])
            ):
                direction = 0
                cooldown_reason = "SAME_DIRECTION_LOSS_COOLDOWN"

            p = s["position"]
            mtm = (
                max(0.0, finite(options[p["key"]]["bid"])) * (1.0 - c["slippage_fraction"]) * p["quantity"]
                - c["fee_per_side"]
                if p
                else 0.0
            )
            equity = s["cash"] + mtm
            s["equity"] = equity
            s["peak_equity"] = max(s["peak_equity"], equity)
            s["max_drawdown"] = max(s["max_drawdown"], s["peak_equity"] - equity)

            _, eff_daily_loss_limit, eff_daily_profit_lock = self.effective_risk_limits()
            halt_reason = None
            if s["capital_halted"] or equity <= c["capital_floor"] + c["safety_buffer"]:
                halt_reason = "CAPITAL_FLOOR_HALT"
            elif s["day_start"] - equity >= eff_daily_loss_limit:
                halt_reason = "DAILY_LOSS_HALT"
            elif s["cash"] - s["day_start"] >= eff_daily_profit_lock:
                halt_reason = "DAILY_PROFIT_LOCK"
            elif s["trades_today"] >= c["max_trades_day"]:
                halt_reason = "MAX_TRADES_DAY"
            elif s["loss_streak"] >= c["max_loss_streak"]:
                halt_reason = "MAX_LOSS_STREAK_HALT"
            elif (self.folder / "STOP").exists() or day in c["blackout_dates"] or not (c["entry_start_clock"] <= clock < c["entry_end_clock"]):
                halt_reason = "SESSION_OR_COOLDOWN_OR_HALT"
            halted = halt_reason is not None
            if halted:
                s["pending"] = None

            # Two-step causal fill: queue on T, verify and fill on T+1 (~2.5s later)
            pending = s["pending"]
            entered_now = False
            if pending and not s["position"] and not halted:
                s["pending"] = None
                o = options.get(pending["key"])
                if o and direction == pending["direction"] and _fast_option_ok(o, now, c):
                    qty, entry = self.budget(o)
                    if qty and min(o["ask_qty"], o["bid_qty"]) >= qty:
                        s["cash"] -= entry * qty + c["fee_per_side"]
                        s["position"] = dict(
                            key=o["key"],
                            kind=o["kind"],
                            strike=o["strike"],
                            quantity=qty,
                            entry=entry,
                            entered=now.isoformat(),
                            stop=entry * (1.0 - c["stop_fraction"]),
                            target=entry * (1.0 + c["target_fraction"]),
                            high=entry,
                            regime_profile="low_vix" if (self._low_vix_c is not None and c is self._low_vix_c) else "normal",
                        )
                        s["trades_today"] += 1
                        entered_now = True
                        self.log(
                            "PAPER_ENTRY",
                            now,
                            key=o["key"],
                            strike=o["strike"],
                            option_kind=o["kind"],
                            quantity=qty,
                            entry=round(entry, 2),
                            setup=report["setup_type"],
                        )

            # Evaluate new candidate setup on EVERY live tick/bar when flat!
            reason = "IN_POSITION" if s["position"] else (cooldown_reason or "NO_FAST_SETUP")
            if direction and not s["position"] and not halted and not entered_now:
                kind = "CE" if direction == 1 else "PE"
                candidates = [o for o in options.values() if o["kind"] == kind and _fast_option_ok(o, now, c)]
                target_delta = float(c.get("target_delta", 0.58))
                candidates.sort(key=lambda o: abs(abs(o["greeks"]["delta"]) - target_delta))
                reason = "RISK_OR_GREEKS_OR_LIQUIDITY_VETO"
                for o in candidates:
                    qty, _ = self.budget(o)
                    if qty:
                        s["pending"] = dict(key=o["key"], direction=direction, created=snap["timestamp"])
                        reason = f"SIGNAL_PENDING_{kind}"
                        break
            elif halted and not s["position"]:
                reason = halt_reason or "SESSION_OR_COOLDOWN_OR_HALT"

            bar_ts = snap["bars"][-1]["timestamp"]
            if bar_ts != s["last_bar"] or entered_now or reason.startswith("SIGNAL_PENDING"):
                s["last_bar"] = bar_ts
                self.log("FAST_DECISION", now, reason=reason, report=report)

            if equity <= c["capital_floor"] + c["safety_buffer"]:
                s["capital_halted"] = True
                s["pending"] = None
            if (s["cooldown_until"] and ts < stamp(s["cooldown_until"])) or cooldown_reason == "SAME_DIRECTION_LOSS_COOLDOWN":
                s["pending"] = None

            p = s["position"]
            mtm2 = (
                max(0.0, finite(options[p["key"]]["bid"])) * (1.0 - c["slippage_fraction"]) * p["quantity"]
                - c["fee_per_side"]
                if p
                else 0.0
            )
            s["equity"] = s["cash"] + mtm2
            s["peak_equity"] = max(s["peak_equity"], s["equity"])
            s["max_drawdown"] = max(s["max_drawdown"], s["peak_equity"] - s["equity"])
            daily.update(end_equity=s["equity"], last_snapshot=snap["timestamp"], snapshots=daily["snapshots"] + 1)
            s["sample_last_time"] = snap["timestamp"]

            self._staging = False
            self.save()
            committed = True
            for kind, etime, edata in self._events:
                super().log(kind, etime, **edata)
            return self.summary()
        except Exception:
            if not committed:
                self.s = original
            raise
        finally:
            self._staging = False
            self._events = []

    def summary(self):
        result = super().summary()
        result.update(
            engine_version=self.c["engine_version"],
            timeframe=self.c["timeframe"],
            source=self.s.get("dataset_source"),
            capital_halted=self.s.get("capital_halted", False),
            cooldown_until=self.s.get("cooldown_until"),
            gap_warnings=self.s.get("gap_warnings", 0),
            last_decision=self.s.get("last_decision"),
            warning="Research paper simulation; not a forecast or real-money result",
        )
        return result

    def reports(self):
        daily_rows = []
        for day, d in sorted(self.s["daily"].items()):
            trades = [t for t in self.s["trades"] if stamp(t["exited"]).date().isoformat() == day]
            entry_fees = sum(
                self.c["fee_per_side"] for t in self.s["trades"] if stamp(t["entered"]).date().isoformat() == day
            )
            p = self.s["position"]
            if p and stamp(p["entered"]).date().isoformat() == day:
                entry_fees += self.c["fee_per_side"]
            exit_fees = len(trades) * self.c["fee_per_side"]
            daily_rows.append(
                dict(
                    date=day,
                    start_equity=round(d["start_equity"], 2),
                    closed_trades=len(trades),
                    wins=sum(t["net_pnl"] > 0 for t in trades),
                    losses=sum(t["net_pnl"] < 0 for t in trades),
                    net_pnl=round(d["end_equity"] - d["start_equity"], 2),
                    booked_estimated_charges=round(entry_fees + exit_fees, 2),
                    end_equity=round(d["end_equity"], 2),
                    snapshots=d["snapshots"],
                    first_snapshot=d["first_snapshot"],
                    last_snapshot=d["last_snapshot"],
                    max_gap_seconds=round(d["max_gap_seconds"], 3),
                    source=self.s["dataset_source"],
                    scope="Fast 1m+tick practice snapshots; open-position equity includes estimated exit costs",
                )
            )
        if daily_rows:
            with (self.folder / "daily_pnl.csv").open("w", newline="", encoding="utf8") as f:
                w = csv.DictWriter(f, fieldnames=list(daily_rows[0]))
                w.writeheader()
                w.writerows(daily_rows)
        with (self.folder / "trades.csv").open("w", newline="", encoding="utf8") as f:
            fields = ["key", "entered", "exited", "quantity", "entry", "exit", "net_pnl", "charges", "reason"]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(self.s["trades"])
        summary = self.summary()
        summary["observed_days"] = len(daily_rows)
        summary["net_pnl_reconciliation_error"] = round(sum(r["net_pnl"] for r in daily_rows) - summary["net_pnl"], 2)
        summary["coverage_note"] = "Fast 1-minute + sub-minute tick paper session."
        atomic(self.folder / "summary.json", summary)
        return summary
