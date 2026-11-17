#!/usr/bin/env python3
"""NIFTY options research bot. PRACTICE-only paper execution; all LIVE / broker account endpoints removed.
Shared read-only market-data adapter and paper execution components. Use nifty_bot.py as the entry point.
Python 3.10+, standard library only. No broker portfolio, funds, or order endpoints exist.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
INDEX = 'NSE_INDEX|Nifty 50'
DEFAULTS = dict(initial_wallet=50000.0, capital_floor=35000.0,
    safety_buffer=2000.0, planned_risk=250.0, daily_loss_limit=1000.0,
    max_trades_day=3, max_loss_streak=3, stop_fraction=.20,
    target_fraction=.40, trail_fraction=.15, slippage_fraction=.002,
    fee_per_side=40.0, max_spread_fraction=.02, min_oi=10000,
    min_volume=10000, delta_min=.35, delta_max=.70, max_theta_fraction=.08,
    max_gamma_move=0.15, max_iv=40.0, min_iv=5.0,
    max_vega_shock_fraction=.15, quote_max_age_seconds=15,
    snapshot_max_age_seconds=30, blackout_dates=[])

class Blocked(Exception):
    pass

def stamp(value):
    if isinstance(value, (int, float)) or str(value).isdigit():
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, IST)
    raw = str(value).strip().replace('Z', '+00:00')
    raw = re.sub(r'\.(\d+)(?=[+-]|$)', lambda m: '.' + (m.group(1) + '000000')[:6], raw)
    d = datetime.fromisoformat(raw)
    if d.tzinfo is None:
        raise Blocked('Timestamp must include a timezone')
    return d.astimezone(IST)

def finite(value):
    v = float(value)
    if not math.isfinite(v):
        raise Blocked('Non-finite numeric data')
    return v

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf8') as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)

def emit(path, data):
    with Path(path).open('a', encoding='utf8') as f:
        f.write(json.dumps(data, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())

def config(path=None):
    c = DEFAULTS.copy()
    if path:
        supplied = json.loads(Path(path).read_text())
        if set(supplied) - set(c):
            raise Blocked('Unknown configuration keys')
        c.update(supplied)
    for key, default in DEFAULTS.items():
        if isinstance(default, (float, int)) and finite(c[key]) <= 0:
            raise Blocked('Configuration numbers must be positive: ' + key)
    if not c['capital_floor'] + c['safety_buffer'] < c['initial_wallet']:
        raise Blocked('Wallet must exceed floor plus safety buffer')
    for k in ['stop_fraction', 'target_fraction', 'trail_fraction', 'slippage_fraction', 'max_spread_fraction']:
        if not 0 < c[k] < 1:
            raise Blocked('Invalid fraction: ' + k)
    if not 0 < c['delta_min'] < c['delta_max'] < 1:
        raise Blocked('Invalid delta bounds')
    return c

class Upstox:
    ALLOWED_MARKET_DATA_PREFIXES = (
        '/v2/market/status/',
        '/v2/option/contract',
        '/v2/option/chain',
        '/v2/market-quote/',
        '/v3/historical-candle/',
        '/v2/historical-candle/',
    )

    def __init__(self):
        self.token = os.getenv('UPSTOX_ACCESS_TOKEN', '')
        if not self.token:
            raise Blocked('Set UPSTOX_ACCESS_TOKEN locally. Do not paste credentials into chat.')

    def get(self, path, params=None):
        if not any(str(path).startswith(p) for p in self.ALLOWED_MARKET_DATA_PREFIXES):
            raise Blocked(f'Blocked endpoint {path}: only read-only market-data endpoints are allowed in PRACTICE mode')
        url = 'https://api.upstox.com' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0',
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            raise Blocked(f'Upstox HTTP {e.code}; no order submitted') from None
        except (urllib.error.URLError, TimeoutError):
            raise Blocked('Upstox connection unavailable; no order submitted') from None
        if result.get('status') != 'success':
            raise Blocked('Upstox returned a non-success response')
        return result['data']

    def candles(self, start=None, end=None):
        key = urllib.parse.quote(INDEX, safe='')
        path = f'/v3/historical-candle/intraday/{key}/minutes/5'
        if start:
            path = f'/v3/historical-candle/{key}/minutes/5/{end}/{start}'
        raw = self.get(path)['candles']
        return [dict(timestamp=r[0], open=r[1], high=r[2], low=r[3],
                     close=r[4], volume=r[5]) for r in sorted(raw, key=lambda r: stamp(r[0]))]

    def snapshot(self):
        begin = datetime.now(IST)
        status = self.get('/v2/market/status/NSE')['status']
        if status != 'NORMAL_OPEN':
            raise Blocked('Exchange not NORMAL_OPEN: ' + status)
        contracts = self.get('/v2/option/contract', {'instrument_key': INDEX})
        expiries = sorted({x['expiry'] for x in contracts
                           if 2 <= (date.fromisoformat(x['expiry']) - begin.date()).days <= 14})
        if not expiries:
            raise Blocked('No suitable expiry 2–14 calendar days away')
        expiry = expiries[0]
        chain = self.get('/v2/option/chain', {'instrument_key': INDEX, 'expiry_date': expiry})
        bars = self.candles()
        if len(bars) < 65:
            bars = self.candles((begin.date()-timedelta(days=10)).isoformat(),
                                (begin.date()-timedelta(days=1)).isoformat()) + bars
        bars = [b for b in bars if stamp(b['timestamp']) + timedelta(minutes=5) <= begin]
        meta = {x['instrument_key']: x for x in contracts if x['expiry'] == expiry}
        options = []
        for row in chain:
            for side, kind in [('call_options', 'CE'), ('put_options', 'PE')]:
                raw = row.get(side, {})
                key = raw.get('instrument_key')
                if key not in meta:
                    continue
                md, g = raw.get('market_data', {}), raw.get('option_greeks', {})
                options.append(dict(key=key, kind=kind, expiry=expiry,
                    strike=row['strike_price'], lot=meta[key]['lot_size'],
                    bid=md.get('bid_price'), ask=md.get('ask_price'),
                    bid_qty=md.get('bid_qty'), ask_qty=md.get('ask_qty'),
                    oi=md.get('oi'), volume=md.get('volume'), greeks=g))
        # Independently obtain exchange quote timestamps; chain has no source timestamp.
        keys = list(dict.fromkeys([o['key'] for o in options] + getattr(self,'extra_quote_keys',[])))
        quotes = {}
        for offset in range(0,len(keys),400):
            quotes.update(self.get('/v2/market-quote/quotes', {'instrument_key': ','.join(keys[offset:offset+400])}))
        self.last_quotes = quotes
        bykey = {q['instrument_token']: q for q in quotes.values()}
        for o in options:
            q = bykey.get(o['key'], {})
            o['quote_time'] = q.get('timestamp')
            depth = q.get('depth', {})
            buy, sell = depth.get('buy', []), depth.get('sell', [])
            if buy and sell:
                o.update(bid=buy[0]['price'], bid_qty=buy[0]['quantity'],
                         ask=sell[0]['price'], ask_qty=sell[0]['quantity'])
        end = datetime.now(IST)
        return dict(timestamp=end.isoformat(), collected_from=begin.isoformat(),
                    source='upstox-rest', market_status=status,
                    underlying=INDEX, bars=bars[-100:], options=options)


def ema(values, period):
    result = values[0]
    for v in values[1:]:
        result += (v-result)*2/(period+1)
    return result

def votes(snapshot):
    bars = snapshot['bars']
    if len(bars) < 60:
        raise Blocked('Need at least 60 completed 5-minute candles')
    prices = [finite(b['close']) for b in bars]
    fast, slow = ema(prices, 9), ema(prices, 21)
    trend = 1 if fast > slow and prices[-1] > fast else -1 if fast < slow and prices[-1] < fast else 0
    diffs = [b-a for a,b in zip(prices[-15:-1], prices[-14:])]
    gain, loss = sum(max(x,0) for x in diffs), sum(max(-x,0) for x in diffs)
    rsi = 100 if loss == 0 and gain else 50 if loss == gain == 0 else 100-100/(1+gain/loss)
    momentum = 1 if 55 <= rsi <= 75 else -1 if 25 <= rsi <= 45 else 0
    high = max(finite(b['high']) for b in bars[-21:-1])
    low = min(finite(b['low']) for b in bars[-21:-1])
    breakout = 1 if prices[-1] > high else -1 if prices[-1] < low else 0
    roc = prices[-1]/prices[-7]-1
    rate = 1 if roc > .001 else -1 if roc < -.001 else 0
    # OI imbalance is a heuristic, not evidence of buying versus selling intent.
    call_oi = sum(finite(o['oi']) for o in snapshot['options'] if o['kind']=='CE')
    put_oi = sum(finite(o['oi']) for o in snapshot['options'] if o['kind']=='PE')
    pcr = put_oi/call_oi if call_oi > 0 else None
    oi_vote = 1 if pcr and 1.1 <= pcr <= 1.8 else -1 if pcr and .5 <= pcr <= .9 else 0
    ballot = dict(ema_trend=trend, rsi_momentum=momentum, donchian_breakout=breakout,
                  rate_of_change=rate, oi_pcr=oi_vote)
    direction = 1 if list(ballot.values()).count(1) >= 4 else -1 if list(ballot.values()).count(-1) >= 4 else 0
    return direction, ballot, dict(rsi=rsi, pcr=pcr, roc=roc)


def validate_snapshot(s, now, c):
    ts = stamp(s['timestamp'])
    if s.get('underlying') != INDEX or s.get('market_status') != 'NORMAL_OPEN':
        raise Blocked('Wrong underlying or market closed')
    if not 0 <= (now-ts).total_seconds() <= c['snapshot_max_age_seconds']:
        raise Blocked('Stale or future snapshot')
    if not 0 <= (ts-stamp(s['collected_from'])).total_seconds() <= c['snapshot_max_age_seconds']:
        raise Blocked('Data collection too slow')
    bars = s['bars']
    stamps = [stamp(b['timestamp']) for b in bars]
    if stamps != sorted(set(stamps)):
        raise Blocked('Duplicate or unordered candles')
    if not bars or stamps[-1]+timedelta(minutes=5) > ts:
        raise Blocked('Incomplete candle / future information')
    if (ts-stamps[-1]-timedelta(minutes=5)).total_seconds() > 360:
        raise Blocked('Underlying candles are stale')
    for b in bars:
        o,h,l,cl = [finite(b[x]) for x in ('open','high','low','close')]
        if l <= 0 or h < max(o,cl,l) or l > min(o,cl):
            raise Blocked('Malformed OHLC')
    keys = [o['key'] for o in s['options']]
    if len(keys) != len(set(keys)):
        raise Blocked('Duplicate option instrument keys')


def option_ok(o, now, c):
    try:
        bid, ask = finite(o['bid']), finite(o['ask'])
        if not 0 < bid <= ask or (ask-bid)/ask > c['max_spread_fraction']:
            return False
        if not 0 <= (now-stamp(o['quote_time'])).total_seconds() <= c['quote_max_age_seconds']:
            return False
        if not 2 <= (date.fromisoformat(o['expiry'])-now.date()).days <= 14:
            return False
        if finite(o['lot']) <= 0 or int(o['lot']) != finite(o['lot']):
            return False
        if min(finite(o['bid_qty']), finite(o['ask_qty'])) < o['lot']:
            return False
        if finite(o['oi']) < c['min_oi'] or finite(o['volume']) < c['min_volume']:
            return False
        g = {k: finite(o['greeks'][k]) for k in ('delta','theta','gamma','vega','iv')}
        if o['kind'] == 'CE' and g['delta'] <= 0 or o['kind'] == 'PE' and g['delta'] >= 0:
            return False
        return (c['delta_min'] <= abs(g['delta']) <= c['delta_max'] and
                abs(g['theta'])/ask <= c['max_theta_fraction'] and
                0 <= g['gamma']*100 <= c['max_gamma_move'] and
                c['min_iv'] <= g['iv'] <= c['max_iv'] and
                0 <= g['vega']*5/ask <= c['max_vega_shock_fraction'])
    except (KeyError, TypeError, ValueError, Blocked):
        return False


class PaperEngine:
    def __init__(self, c, folder):
        self.c, self.folder = c, Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder/'practice_state.json'
        digest = hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
        self.s = json.loads(self.path.read_text()) if self.path.exists() else dict(
            config_hash=digest, initial_wallet=c['initial_wallet'], cash=c['initial_wallet'],
            position=None, day=None, day_start=c['initial_wallet'], trades_today=0,
            loss_streak=0, last_bar=None, last_snapshot=None, pending=None, trades=[],
            peak_equity=c['initial_wallet'], max_drawdown=0.0, equity=c['initial_wallet'])
        if self.s['config_hash'] != digest:
            raise Blocked('Configuration differs from saved wallet. Use a separate run folder.')

    def save(self):
        atomic(self.path, self.s)

    def decide(self, snapshot):
        return votes(snapshot)

    def log(self, kind, now, **data):
        emit(self.folder/'audit.jsonl', dict(time=now.isoformat(), event=kind, **data))

    def budget(self, option):
        c,s = self.c,self.s
        entry = finite(option['ask'])*(1+c['slippage_fraction'])
        # Include adverse exit slippage in planned stop loss.
        per_unit_risk = entry-entry*(1-c['stop_fraction'])*(1-c['slippage_fraction'])
        fees = 2*c['fee_per_side']
        headroom = s['cash']-c['capital_floor']-c['safety_buffer']
        daily_remaining = min(c['daily_loss_limit'], c['daily_loss_limit'] + s['cash']-s['day_start'])
        worst_budget = min(headroom,daily_remaining)
        # Both normal stop risk AND full-premium loss must fit. No naked options.
        lots = min(math.floor((c['planned_risk']-fees)/(per_unit_risk*option['lot'])),
                   math.floor((worst_budget-fees)/(entry*option['lot'])))
        return max(0,lots)*option['lot'], entry

    def close(self, price, now, reason):
        s,c = self.s,self.c
        p = s['position']
        fill = max(0, price*(1-c['slippage_fraction']))
        s['cash'] += fill*p['quantity']-c['fee_per_side']
        pnl = (fill-p['entry'])*p['quantity']-2*c['fee_per_side']
        trade = dict(key=p['key'], entered=p['entered'], exited=now.isoformat(),
                     quantity=p['quantity'], entry=p['entry'], exit=fill,
            net_pnl=round(pnl,2), charges=2*c['fee_per_side'], reason=reason)
        s['trades'].append(trade)
        s['loss_streak'] = s['loss_streak']+1 if pnl < 0 else 0
        s['position'] = None
        self.log('PAPER_EXIT', now, **trade)

    def step(self, snap, now=None):
        c,s = self.c,self.s
        now = now or datetime.now(IST)
        validate_snapshot(snap, now, c)
        ts = stamp(snap['timestamp'])
        if s['last_snapshot'] and ts <= stamp(s['last_snapshot']):
            raise Blocked('Snapshots must advance strictly; duplicate processing prevented')
        s['last_snapshot'] = snap['timestamp']
        options = {o['key']: o for o in snap['options']}
        clock = now.strftime('%H:%M')
        if s['day'] != now.date().isoformat():
            if s['position']:
                raise Blocked('Overnight paper position: replay gap or interrupted session; reconcile before continuing')
            s.update(day=now.date().isoformat(), day_start=s['cash'], trades_today=0,
                     loss_streak=0, pending=None)
        p=s['position']
        if p:
            o=options.get(p['key'])
            if not o or not o.get('quote_time') or not 0 <= (now-stamp(o['quote_time'])).total_seconds() <= c['quote_max_age_seconds']:
                raise Blocked('Open position has no fresh quote: paper stop cannot be evaluated')
            bid=finite(o['bid'])
            if bid <= 0 or finite(o.get('bid_qty',0)) < p['quantity']:
                raise Blocked('Open position lacks executable bid depth; no paper exit invented')
            # Evaluate previous stop before raising it: no same-snapshot intrabar assumption.
            if bid <= p['stop'] or bid >= p['target'] or clock >= '15:15' or (self.folder/'STOP').exists():
                reason='STOP' if bid <= p['stop'] else 'TARGET' if bid >= p['target'] else 'SESSION_OR_KILL'
                self.close(bid,now,reason)
            else:
                p['high']=max(p['high'],bid)
                if p['high'] >= p['entry']*(1+c['stop_fraction']):
                    p['stop']=max(p['stop'],p['high']*(1-c['trail_fraction']))
        # Record liquidation equity inclusive of exit fee; never value at the ask.
        p=s['position']
        equity=s['cash']+(max(0,finite(options[p['key']]['bid']))*(1-c['slippage_fraction'])*p['quantity']-c['fee_per_side'] if p else 0)
        s['equity']=equity
        s['peak_equity']=max(s['peak_equity'],equity)
        s['max_drawdown']=max(s['max_drawdown'],s['peak_equity']-equity)
        halted=(equity <= c['capital_floor']+c['safety_buffer'] or
                s['day_start']-equity >= c['daily_loss_limit'] or
                s['trades_today'] >= c['max_trades_day'] or
                s['loss_streak'] >= c['max_loss_streak'] or
                (self.folder/'STOP').exists() or s['day'] in c['blackout_dates'] or
                not '09:30' <= clock < '14:45')
        if halted:
            s['pending']=None
        # Signal on one snapshot, fill on a later snapshot. Recheck every entry gate.
        pending=s['pending']
        if pending and not s['position'] and not halted:
            s['pending']=None
            direction,ballot,_=self.decide(snap)
            o=options.get(pending['key'])
            if o and direction==pending['direction'] and option_ok(o,now,c):
                qty,entry=self.budget(o)
                if qty and min(o['ask_qty'],o['bid_qty']) >= qty:
                    s['cash']-=entry*qty+c['fee_per_side']
                    s['position']=dict(key=o['key'],quantity=qty,entry=entry,
                        entered=now.isoformat(),stop=entry*(1-c['stop_fraction']),
                        target=entry*(1+c['target_fraction']),high=entry)
                    s['trades_today']+=1
                    self.log('PAPER_ENTRY',now,key=o['key'],quantity=qty,entry=entry,votes=ballot)
        bar=snap['bars'][-1]['timestamp']
        if bar != s['last_bar']:
            s['last_bar']=bar
            direction,ballot,metrics=self.decide(snap)
            reason='NO_CONSENSUS'
            if direction and not s['position'] and not halted:
                kind='CE' if direction==1 else 'PE'
                candidates=[o for o in options.values() if o['kind']==kind and option_ok(o,now,c)]
                candidates.sort(key=lambda o: abs(abs(o['greeks']['delta'])-.5))
                reason='RISK_OR_GREEKS_OR_LIQUIDITY_VETO'
                for o in candidates:
                    qty,_=self.budget(o)
                    if qty:
                        s['pending']=dict(key=o['key'],direction=direction)
                        reason='SIGNAL_PENDING_NEXT_SNAPSHOT'
                        break
            if halted:
                reason='SESSION_OR_RISK_HALT'
            self.log('DECISION',now,votes=ballot,metrics=metrics,reason=reason)
        # Recompute after a new entry, including spread, slippage and both fee sides.
        p=s['position']
        s['equity']=s['cash']+(finite(options[p['key']]['bid'])*(1-c['slippage_fraction'])*p['quantity']-c['fee_per_side'] if p else 0)
        s['peak_equity']=max(s['peak_equity'],s['equity'])
        s['max_drawdown']=max(s['max_drawdown'],s['peak_equity']-s['equity'])
        self.save()
        return self.summary()

    def summary(self):
        s=self.s
        return dict(mode='PRACTICE', initial_wallet=s['initial_wallet'],
            equity=round(s['equity'],2), cash=round(s['cash'],2),
            net_pnl=round(s['equity']-s['initial_wallet'],2),
            closed_trades=len(s['trades']), open_position=s['position'],
            max_drawdown=round(s['max_drawdown'],2),
            performance_validated=False, fees='Estimated Rs40/side; replace with actual all-in charges')


def download_history(api,start,end,out):
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    if first>last or last>datetime.now(IST).date():
        raise Blocked('Invalid date range or future end date')
    rows={}
    cursor=first
    while cursor<=last:
        stop=min(last,cursor+timedelta(days=20))
        for row in api.candles(cursor.isoformat(),stop.isoformat()):
            if first<=stamp(row['timestamp']).date()<=last:
                rows[row['timestamp']]=row
        cursor=stop+timedelta(days=1)
    if not rows:
        raise Blocked('API returned no historical candles; no performance can be computed')
    with Path(out).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['timestamp','open','high','low','close','volume'])
        w.writeheader(); w.writerows(sorted(rows.values(),key=lambda x:stamp(x['timestamp'])))
    return dict(status='UNDERLYING_DATA_ONLY',rows=len(rows),file=out,
                options_backtest=False,reason='Historical option quotes, OI and Greeks are still required')

