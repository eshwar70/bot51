"""Deterministic research signals. None represents a probability of profit."""
from dataclasses import dataclass, asdict
from datetime import timedelta
import math
from statistics import mean, pstdev
from core import Blocked, finite, stamp, ema, INDEX

CATALOGUE = [
 ('ema_alignment','trend','EMA 9/21/50 alignment'),
 ('macd_momentum','trend','MACD above/below its trailing signal'),
 ('roc_continuation','trend','Six-bar price momentum'),
 ('donchian_breakout','trend','Close outside prior 20-bar channel'),
 ('opening_range_breakout','trend','Break of completed 09:15–09:30 range'),
 ('trend_pullback','trend','Pullback across EMA 9 followed by recovery'),
 ('previous_day_breakout','trend','Close beyond prior session high/low'),
 ('bollinger_reentry','range','Re-entry after prior close outside 2-sigma band'),
 ('rsi_reversal','range','RSI recovering through 30 or falling through 70'),
 ('failed_breakout','range','Prior channel break fails on latest close'),
 ('futures_vwap_reclaim','trend','NIFTY futures cross their session VWAP'),
]
DEFERRED = {
 'volatility_relative_value':'Needs historical IV surface, realised-volatility forecast, and tested multi-leg execution',
 'calendar_or_vertical_spreads':'Needs coordinated legs, margin accounting and partial-fill recovery',
 'order_flow_imbalance':'Needs timestamped depth and executed trade feed; snapshots alone are insufficient',
 'statistical_arbitrage':'Needs synchronised instruments, hedge sizing, financing and execution model',
 'market_making':'Needs inventory risk and specialised low-latency execution; not appropriate for this release',
 'smart_money_chart_patterns':'No institution intent is inferred; objective breakout/reversal rules are tested instead',
}

@dataclass
class Signal:
    name: str
    regime: str
    direction: int
    detail: str
    family: str = 'price_action'
    validation: str = 'RESEARCH_UNVALIDATED'


def sign(v):
    return 1 if v > 0 else -1 if v < 0 else 0


def rsi(values):
    changes=[b-a for a,b in zip(values[-15:-1],values[-14:])]
    gains=sum(max(v,0) for v in changes)
    losses=sum(max(-v,0) for v in changes)
    if not gains and not losses:return 50.0
    return 100.0 if not losses else 100-100/(1+gains/losses)


def fresh(value,now,seconds):
    try:return 0 <= (now-stamp(value)).total_seconds() <= seconds
    except (ValueError,TypeError,Blocked):return False


def price_signals(s):
    bars=s['bars']
    if len(bars)<60:raise Blocked('At least 60 completed five-minute candles are required')
    closes=[finite(b['close']) for b in bars]
    price,prev=closes[-1],closes[-2]
    fast,slow,long=ema(closes,9),ema(closes,21),ema(closes,50)
    prevfast=ema(closes[:-1],9)
    atr=mean(max(finite(bars[i]['high'])-finite(bars[i]['low']),
                 abs(finite(bars[i]['high'])-closes[i-1]),
                 abs(finite(bars[i]['low'])-closes[i-1])) for i in range(len(bars)-14,len(bars)))
    path=sum(abs(b-a) for a,b in zip(closes[-21:-1],closes[-20:]))
    efficiency=abs(price-closes[-21])/path if path else 0
    separation=abs(fast-slow)/atr if atr else 0
    regime='trend' if efficiency>=.35 and separation>=.30 else 'range' if efficiency<=.25 else 'uncertain'
    signals=[]
    def add(name,reg,direction,detail):signals.append(Signal(name,reg,direction,detail))
    alignment=1 if price>fast>slow>long else -1 if price<fast<slow<long else 0
    add('ema_alignment','trend',alignment,'Price and EMA 9/21/50 must align')
    macd=[ema(closes[:i],12)-ema(closes[:i],26) for i in range(26,len(closes)+1)]
    hist=macd[-1]-ema(macd,9)
    add('macd_momentum','trend',1 if macd[-1]>0 and hist>0 else -1 if macd[-1]<0 and hist<0 else 0,'MACD sign and histogram agree')
    roc=price/closes[-7]-1
    add('roc_continuation','trend',sign(roc) if abs(roc)>.001 else 0,'Six-bar return exceeds 0.1%')
    high=max(finite(b['high']) for b in bars[-21:-1]);low=min(finite(b['low']) for b in bars[-21:-1])
    add('donchian_breakout','trend',1 if price>high else -1 if price<low else 0,'Latest close versus prior channel')
    day=stamp(s['timestamp']).date()
    today=[b for b in bars if stamp(b['timestamp']).date()==day]
    opening=[b for b in today if '09:15'<=stamp(b['timestamp']).strftime('%H:%M')<'09:30']
    orb=0
    if len(opening)==3 and stamp(bars[-1]['timestamp']).strftime('%H:%M')>='09:30':
        orb=1 if price>max(finite(b['high']) for b in opening) else -1 if price<min(finite(b['low']) for b in opening) else 0
    add('opening_range_breakout','trend',orb,'Requires all three completed opening five-minute bars')
    pullback=1 if fast>slow and prev<=prevfast and price>fast else -1 if fast<slow and prev>=prevfast and price<fast else 0
    add('trend_pullback','trend',pullback,'Latest close reclaims fast EMA in trend direction')
    prior=[b for b in bars if stamp(b['timestamp']).date()<day]
    previous=s.get('previous_session')
    pd=0
    prior_date=max((stamp(b['timestamp']).date() for b in prior),default=None)
    if previous and previous.get('complete') and stamp(previous['timestamp']).date()==prior_date:
        pd=1 if price>finite(previous['high']) else -1 if price<finite(previous['low']) else 0
    add('previous_day_breakout','trend',pd,'Requires explicitly complete previous-session OHLC; partial history is not substituted')
    oldmean,oldsd=mean(closes[-21:-1]),pstdev(closes[-21:-1])
    curmean,cursd=mean(closes[-20:]),pstdev(closes[-20:])
    bb=1 if prev<oldmean-2*oldsd and price>=curmean-2*cursd else -1 if prev>oldmean+2*oldsd and price<=curmean+2*cursd else 0
    add('bollinger_reentry','range',bb,'Prior extreme followed by band re-entry')
    oldr,newr=rsi(closes[:-1]),rsi(closes)
    add('rsi_reversal','range',1 if oldr<30<=newr else -1 if oldr>70>=newr else 0,'RSI crosses back from extreme')
    earlier_high=max(finite(b['high']) for b in bars[-22:-2]);earlier_low=min(finite(b['low']) for b in bars[-22:-2])
    failed=1 if prev<earlier_low and price>=earlier_low else -1 if prev>earlier_high and price<=earlier_high else 0
    add('failed_breakout','range',failed,'Previous close breaks channel; latest close returns inside')
    future=s.get('futures',{})
    fv=0
    fbars=future.get('bars',[])
    if future.get('underlying')==INDEX and len(fbars)>=4 and fresh(future.get('quote_time'),stamp(s['timestamp']),15):
        times=[stamp(b['timestamp']) for b in fbars]
        opening_time=times[0].replace(hour=9,minute=15,second=0,microsecond=0)
        causal=(times[0]==opening_time and all(t.date()==day for t in times) and
                all(b-a==timedelta(minutes=5) for a,b in zip(times,times[1:])) and
                times[-1]+timedelta(minutes=5)<=stamp(s['timestamp']) and
                (stamp(s['timestamp'])-times[-1]-timedelta(minutes=5)).total_seconds()<=360)
        if causal:
            def vwap(bs):
                volume=sum(finite(b['volume']) for b in bs)
                return sum((finite(b['high'])+finite(b['low'])+finite(b['close']))/3*finite(b['volume']) for b in bs)/volume if volume>0 else None
            current,old=vwap(fbars),vwap(fbars[:-1])
            if current is not None and old is not None:
                fv=1 if finite(fbars[-2]['close'])<=old and finite(fbars[-1]['close'])>current else -1 if finite(fbars[-2]['close'])>=old and finite(fbars[-1]['close'])<current else 0
    add('futures_vwap_reclaim','trend',fv,'Uses traded futures volume, never index volume')
    return signals,dict(regime=regime,efficiency=efficiency,atr=atr,ema_separation_atr=separation,rsi=newr)


def breadth_evidence(s,now,c):
    b=s.get('breadth',{})
    if not fresh(b.get('timestamp'),now,c['quote_max_age_seconds']):
        return 0,'BREADTH_MISSING_OR_STALE'
    if b.get('universe')!='NIFTY50' or b.get('expected')!=50 or not b.get('membership_as_of'):
        return 0,'BREADTH_UNIVERSE_UNVERIFIED'
    membership=stamp(b['membership_as_of'])
    if membership>now or (now-membership).total_seconds()>86400:
        return 0,'BREADTH_MEMBERSHIP_DATE_INVALID'
    rows=b.get('quotes',[])
    if len({r['key'] for r in rows})!=len(rows):return 0,'BREADTH_DUPLICATE_KEYS'
    valid=[]
    for q in rows:
        if fresh(q.get('timestamp'),now,c['quote_max_age_seconds']) and finite(q['previous_close'])>0 and finite(q['last'])>0:
            valid.append(q)
    if len(valid)<c['breadth_min_names']:return 0,'BREADTH_COVERAGE_TOO_LOW'
    up=sum(q['last']>q['previous_close'] for q in valid)/len(valid)
    down=sum(q['last']<q['previous_close'] for q in valid)/len(valid)
    direction=1 if up>=c['breadth_agreement'] else -1 if down>=c['breadth_agreement'] else 0
    return direction,f'{len(valid)}/50 fresh; advancing={up:.1%}, declining={down:.1%}'


def positioning_evidence(s,now,c):
    paired={}
    try:
        for o in s['options']:
            if not fresh(o.get('quote_time'),now,c['quote_max_age_seconds']):continue
            oi=finite(o['oi'])
            if oi<0:continue
            paired.setdefault((o['expiry'],finite(o['strike'])),{})[o['kind']]=oi
        complete=[(k,v) for k,v in paired.items() if 'CE' in v and 'PE' in v]
        if len(complete)<5:return 0,'OI_NEEDS_FIVE_PAIRED_STRIKES'
        if len({k[0] for k,v in complete})!=1:return 0,'OI_MIXED_EXPIRIES'
        calls=sum(v['CE'] for k,v in complete);puts=sum(v['PE'] for k,v in complete)
        if calls<=0:return 0,'OI_INVALID_CALL_TOTAL'
        pcr=puts/calls
        direction=1 if 1.1<=pcr<=1.8 else -1 if .5<=pcr<=.9 else 0
        return direction,f'Paired-strike PCR={pcr:.3f}; heuristic, not institutional intent'
    except (ValueError,TypeError,KeyError,Blocked):return 0,'OI_MISSING_OR_INVALID'


class ResearchEnsemble:
    def __init__(self,c):self.c=c

    def evaluate(self,s):
        c=self.c;now=stamp(s['timestamp'])
        signals,features=price_signals(s)
        enabled=set(c['enabled_strategies'])
        active=[a for a in signals if a.name in enabled and a.regime==features['regime']]
        ups=sum(a.direction==1 for a in active);downs=sum(a.direction==-1 for a in active)
        # These are correlated price checks, not independent confirmations.
        direction=1 if ups>=c['min_price_checks'] and downs==0 else -1 if downs>=c['min_price_checks'] and ups==0 else 0
        veto=[]
        if not direction:veto.append('NO_UNAMBIGUOUS_PRICE_SETUP')
        if features['regime']=='uncertain':veto.append('UNCERTAIN_REGIME')
        if any(stamp(b['timestamp'])+timedelta(minutes=5)>now for b in s['bars']):veto.append('FUTURE_OR_INCOMPLETE_BAR')
        # Cross-session gaps allowed; missing intraday bars do not get interpolated.
        for a,b in zip(s['bars'],s['bars'][1:]):
            ta,tb=stamp(a['timestamp']),stamp(b['timestamp'])
            if ta.date()==tb.date() and tb-ta!=timedelta(minutes=5):
                veto.append('INTRADAY_BAR_GAP');break
        breadth,breadth_detail=breadth_evidence(s,now,c)
        oi,oi_detail=positioning_evidence(s,now,c)
        if not direction or breadth!=direction:veto.append('BREADTH_NOT_CONFIRMING')
        if not direction or oi!=direction:veto.append('POSITIONING_NOT_CONFIRMING')
        vix=s.get('vix',{})
        if not fresh(vix.get('timestamp'),now,c['quote_max_age_seconds']):veto.append('VIX_MISSING_OR_STALE')
        elif not c['vix_min']<=finite(vix['value'])<=c['vix_max']:veto.append('VIX_OUTSIDE_RESEARCH_LIMITS')
        for event in c['event_windows']:
            if stamp(event['start'])<=now<=stamp(event['end']):
                veto.append('EVENT_BLACKOUT:'+event['label'])
        return dict(direction=0 if veto else direction,proposed_direction=direction,
            regime=features['regime'],features=features,signals=[asdict(a) for a in signals],
            confirmations={'price_action':direction,'breadth':breadth,'options_positioning':oi},
            evidence_details={'breadth':breadth_detail,'options_positioning':oi_detail},
            vetoes=list(dict.fromkeys(veto)),confidence_probability=None,
            status='RESEARCH_ONLY_NOT_PROFIT_VALIDATED',
            interpretation='Evidence groups use distinct inputs but statistical independence is not established')
