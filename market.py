"""Read-only Upstox adapter and NIFTY constituent mapping. No order submission."""
import csv
import io
import re
from datetime import datetime
from pathlib import Path
import urllib.request
from core import Upstox, Blocked, INDEX, IST, atomic, stamp, finite

CONSTITUENTS_URL='https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv'


def parse_constituents(text,as_of):
    rows=list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
    members=[]
    for row in rows:
        isin=row.get('ISIN Code','').strip()
        if row.get('Series','').strip()!='EQ' or not re.fullmatch(r'IN[A-Z0-9]{10}',isin):
            raise Blocked('Expected NSE constituent CSV with EQ series and valid ISIN Code')
        members.append(dict(key='NSE_EQ|'+isin,symbol=row.get('Symbol','').strip()))
    if len(members)!=50 or len({m['key'] for m in members})!=50:
        raise Blocked('Expected exactly 50 distinct NIFTY constituents')
    return dict(universe='NIFTY50',membership_as_of=as_of,source=CONSTITUENTS_URL,members=members)


def setup_membership(output,csv_path=None):
    if csv_path:
        text=Path(csv_path).read_text(encoding='utf-8-sig')
    else:
        try:
            with urllib.request.urlopen(CONSTITUENTS_URL,timeout=15) as r:
                text=r.read().decode('utf-8-sig')
        except Exception:
            raise Blocked('Constituent download unavailable. Download the official NIFTY 50 constituent CSV locally and use setup --constituents-csv PATH. No bypass attempted.') from None
    result=parse_constituents(text,datetime.now(IST).isoformat())
    atomic(output,result)
    return dict(status='CURRENT_MEMBERSHIP_IMPORTED',count=50,file=output,
                warning='Current membership is not valid for historical replay; retain point-in-time membership in recordings')


def _previous_close(q):
    last, nc = q.get('last_price'), q.get('net_change')
    if last is not None and nc is not None:
        try:
            return round(finite(last) - finite(nc), 4)
        except (ValueError, TypeError, Blocked):
            pass
    return q.get('ohlc', {}).get('close')


class EnrichedUpstox(Upstox):
    def __init__(self,membership_path,futures_key=None):
        super().__init__()
        import json
        if not Path(membership_path).is_file():
            raise Blocked('Run python nifty_bot.py setup to import current NIFTY membership first')
        self.membership=json.loads(Path(membership_path).read_text())
        self.futures_key=futures_key
        keys=[m['key'] for m in self.membership['members']]
        if len(keys)!=50 or len(set(keys))!=50:raise Blocked('Invalid membership file')
        self.extra_quote_keys=keys+['NSE_INDEX|India VIX']+([futures_key] if futures_key else [])

    def snapshot(self):
        if stamp(self.membership['membership_as_of']).date()!=datetime.now(IST).date():
            raise Blocked('Refresh constituent membership with setup for this session')
        s=super().snapshot()
        bykey={q['instrument_token']:q for q in self.last_quotes.values()}
        quotes=[]
        for m in self.membership['members']:
            q=bykey.get(m['key'])
            if not q:continue
            quotes.append(dict(key=m['key'],timestamp=q.get('timestamp'),last=q.get('last_price'),
                               previous_close=_previous_close(q)))
        s['breadth']=dict(timestamp=s['timestamp'],universe='NIFTY50',expected=50,
            membership_as_of=self.membership['membership_as_of'],quotes=quotes)
        q=bykey.get('NSE_INDEX|India VIX',{})
        s['vix']=dict(timestamp=q.get('timestamp'),value=q.get('last_price'))
        s['source']='upstox-rest-v02'
        # Futures VWAP and previous-session breakout accept externally recorded fields;
        # absent context yields neutral signals. No fabricated data or index VWAP.
        return s
