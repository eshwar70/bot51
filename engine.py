"""Risk-first paper execution, persistent decisions, daily and trade reports."""
import copy
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from core import DEFAULTS as BASE, Blocked, PaperEngine, IST, stamp, finite, atomic, option_ok
from strategies import ResearchEnsemble, CATALOGUE

DEFAULTS={**BASE,'engine_version':'0.2','breadth_min_names':45,'breadth_agreement':.60,
 'vix_min':10.0,'vix_max':30.0,'min_price_checks':2,
 'enabled_strategies':[row[0] for row in CATALOGUE], 'event_windows':[],
 'pending_ttl_seconds':45,'cooldown_minutes':15,'max_hold_minutes':60,
 'max_premium_loss_fraction':.02}


def load_config(path=None):
    c=copy.deepcopy(DEFAULTS)
    if path:
        supplied=json.loads(Path(path).read_text())
        if set(supplied)-set(c):raise Blocked('Unknown config keys: '+str(sorted(set(supplied)-set(c))))
        c.update(supplied)
    for k,v in DEFAULTS.items():
        if isinstance(v,(int,float)) and finite(c[k])<=0:raise Blocked('Config must be positive: '+k)
    if not 0<c['capital_floor']+c['safety_buffer']<c['initial_wallet']:raise Blocked('Invalid capital floor/buffer')
    if c['engine_version']!='0.2':raise Blocked('Wrong engine version')
    for k in ('stop_fraction','target_fraction','trail_fraction','slippage_fraction','max_spread_fraction','max_premium_loss_fraction'):
        if not 0<c[k]<1:raise Blocked('Invalid fraction '+k)
    if not .5<c['breadth_agreement']<=1:raise Blocked('Breadth agreement must exceed half')
    if not 1<=c['breadth_min_names']<=50:raise Blocked('Invalid breadth coverage')
    for k in ('breadth_min_names','max_trades_day','max_loss_streak','min_price_checks'):
        if int(c[k])!=c[k]:raise Blocked('Count must be an integer: '+k)
    if not 0<c['delta_min']<c['delta_max']<1 or not c['vix_min']<c['vix_max']:raise Blocked('Invalid exposure bounds')
    names=[r[0] for r in CATALOGUE]
    if not c['enabled_strategies'] or set(c['enabled_strategies'])-set(names):raise Blocked('Unknown or empty strategy selection')
    if len(set(c['enabled_strategies']))!=len(c['enabled_strategies']):raise Blocked('Duplicate strategy names')
    for event in c['event_windows']:
        if stamp(event['start'])>=stamp(event['end']) or not event['label']:raise Blocked('Invalid event window')
    return c


class EnsemblePaperEngine(PaperEngine):
    def __init__(self,c,folder):
        super().__init__(c,folder)
        self.ensemble=ResearchEnsemble(c)
        for key,val in dict(daily={},capital_halted=False,cooldown_until=None,
                            dataset_source=None,last_decision=None,gap_warnings=0,
                            sample_last_time=None).items():self.s.setdefault(key,val)
        self.current_decision=None
        self._staging=False
        self._events=[]

    def save(self):
        if not self._staging:super().save()

    def log(self,kind,now,**data):
        if self._staging:self._events.append((kind,now,data))
        else:super().log(kind,now,**data)

    def decide(self,snapshot):
        report=self.current_decision or self.ensemble.evaluate(snapshot)
        d=report['direction']
        if self.s['capital_halted'] or (self.s['cooldown_until'] and stamp(snapshot['timestamp'])<stamp(self.s['cooldown_until'])):
            d=0
        return d,report['confirmations'],report

    def budget(self,option):
        quantity,entry=super().budget(option)
        lot=int(option['lot']);fees=2*self.c['fee_per_side']
        cap=max(0,(min(self.s['cash'],self.c['initial_wallet'])*self.c['max_premium_loss_fraction']-fees))
        quantity=min(quantity,max(0,math.floor(cap/(entry*lot)))*lot)
        return quantity,entry

    def close(self,price,now,reason):
        super().close(price,now,reason)
        self.s['cooldown_until']=(now+timedelta(minutes=self.c['cooldown_minutes'])).isoformat()

    def step(self,snap,now=None):
        now=now or datetime.now(IST)
        # Validate core temporal/price data before changing state or evaluating exits.
        from core import validate_snapshot
        validate_snapshot(snap,now,self.c)
        ts=stamp(snap['timestamp']);day=ts.date().isoformat()
        if self.s['last_snapshot'] and ts<=stamp(self.s['last_snapshot']):raise Blocked('Snapshot time must increase strictly')
        if self.s['position'] and self.s['day']!=now.date().isoformat():
            raise Blocked('Overnight position requires reconciliation; no synthetic overnight exit')
        source=snap.get('source')
        if source not in ('upstox-rest-v02','synthetic-test-only'):raise Blocked('Unsupported source schema; use a verified adapter')
        if self.s['dataset_source'] and source!=self.s['dataset_source']:raise Blocked('Cannot mix real-feed and synthetic runs')
        # Working copy: malformed evidence cannot corrupt saved paper state.
        original=copy.deepcopy(self.s)
        self._staging=True
        self._events=[]
        committed=False
        try:
            self.s['dataset_source']=source
            if day not in self.s['daily']:
                self.s['daily'][day]=dict(date=day,start_equity=self.s['equity'],end_equity=self.s['equity'],
                    snapshots=0,first_snapshot=snap['timestamp'],last_snapshot=snap['timestamp'],max_gap_seconds=0)
            daily=self.s['daily'][day]
            if daily['snapshots']:
                gap=(ts-stamp(daily['last_snapshot'])).total_seconds()
                daily['max_gap_seconds']=max(daily['max_gap_seconds'],gap)
                if gap>self.c['snapshot_max_age_seconds']:self.s['gap_warnings']+=1
            pending=self.s['pending']
            if pending and (not pending.get('created') or (ts-stamp(pending['created'])).total_seconds()>self.c['pending_ttl_seconds']):
                self.s['pending']=None
            try:
                report=self.ensemble.evaluate(snap)
            except (Blocked,KeyError,ValueError,TypeError,ZeroDivisionError) as exc:
                # Entry evidence failure must not prevent a valid protective exit.
                report=dict(direction=0,proposed_direction=0,regime='unknown',signals=[],confirmations={},
                    vetoes=['INVALID_ENTRY_EVIDENCE:'+type(exc).__name__],confidence_probability=None)
            self.current_decision=report
            self.s['last_decision']=report
            p=self.s['position']
            if p:
                o=next((o for o in snap['options'] if o['key']==p['key']),None)
                if o and o.get('quote_time') and 0<=(now-stamp(o['quote_time'])).total_seconds()<=self.c['quote_max_age_seconds']:
                    bid=finite(o['bid']);qty=p['quantity']
                    liquidation=self.s['cash']+bid*(1-self.c['slippage_fraction'])*qty-self.c['fee_per_side']
                    risk_exit=liquidation<=self.c['capital_floor']+self.c['safety_buffer'] or self.s['day_start']-liquidation>=self.c['daily_loss_limit']
                    time_exit=(now-stamp(p['entered'])).total_seconds()>=self.c['max_hold_minutes']*60
                    if (risk_exit or time_exit) and bid>0 and finite(o.get('bid_qty',0))>=qty:
                        self.close(bid,now,'ACCOUNT_RISK_EXIT' if risk_exit else 'MAX_HOLD_EXIT')
            result=super().step(snap,now)
            if self.s['pending'] and 'created' not in self.s['pending']:
                self.s['pending']['created']=snap['timestamp']
            if self.s['equity']<=self.c['capital_floor']+self.c['safety_buffer']:
                self.s['capital_halted']=True;self.s['pending']=None
            if self.s['cooldown_until'] and ts<stamp(self.s['cooldown_until']):self.s['pending']=None
            daily=self.s['daily'][day]
            daily.update(end_equity=self.s['equity'],last_snapshot=snap['timestamp'],snapshots=daily['snapshots']+1)
            self.s['sample_last_time']=snap['timestamp']
            self.log('ENSEMBLE_DECISION',now,**report)
            self._staging=False
            self.save()
            committed=True
            for kind,event_time,event_data in self._events:
                super().log(kind,event_time,**event_data)
            return self.summary()
        except Exception:
            if not committed:self.s=original
            raise
        finally:
            self.current_decision=None
            self._staging=False
            self._events=[]

    def summary(self):
        result=super().summary()
        result.update(engine_version='0.2',source=self.s.get('dataset_source'),
            capital_halted=self.s.get('capital_halted',False),gap_warnings=self.s.get('gap_warnings',0),
            last_decision=self.s.get('last_decision'),
            warning='Research paper simulation; not a forecast or real-money result')
        return result

    def reports(self):
        daily_rows=[]
        for day,d in sorted(self.s['daily'].items()):
            trades=[t for t in self.s['trades'] if stamp(t['exited']).date().isoformat()==day]
            entry_fees=sum(self.c['fee_per_side'] for t in self.s['trades'] if stamp(t['entered']).date().isoformat()==day)
            p=self.s['position']
            if p and stamp(p['entered']).date().isoformat()==day:entry_fees+=self.c['fee_per_side']
            exit_fees=len(trades)*self.c['fee_per_side']
            daily_rows.append(dict(date=day,start_equity=round(d['start_equity'],2),
                closed_trades=len(trades),wins=sum(t['net_pnl']>0 for t in trades),losses=sum(t['net_pnl']<0 for t in trades),
                net_pnl=round(d['end_equity']-d['start_equity'],2),
                booked_estimated_charges=round(entry_fees+exit_fees,2),end_equity=round(d['end_equity'],2),
                snapshots=d['snapshots'],first_snapshot=d['first_snapshot'],last_snapshot=d['last_snapshot'],
                max_gap_seconds=d['max_gap_seconds'],source=self.s['dataset_source'],
                scope='Observed snapshots only; open-position equity includes estimated exit costs'))
        if daily_rows:
            with (self.folder/'daily_pnl.csv').open('w',newline='',encoding='utf8') as f:
                w=csv.DictWriter(f,fieldnames=list(daily_rows[0]));w.writeheader();w.writerows(daily_rows)
        with (self.folder/'trades.csv').open('w',newline='',encoding='utf8') as f:
            fields=['key','entered','exited','quantity','entry','exit','net_pnl','charges','reason']
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(self.s['trades'])
        summary=self.summary()
        summary['observed_days']=len(daily_rows)
        summary['net_pnl_reconciliation_error']=round(sum(r['net_pnl'] for r in daily_rows)-summary['net_pnl'],2)
        summary['coverage_note']='Missing days are absent, not zero-profit days. Sparse snapshots miss possible fills and stops.'
        atomic(self.folder/'summary.json',summary)
        return summary
