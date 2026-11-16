#!/usr/bin/env python3
"""
build_data.py - reproducible market-data pipeline for the portfolio dashboard.

Usage
-----
    pip install yfinance pandas numpy requests
    python build_data.py                # writes data/portfolio_data.json (+ CSV copies in data/csv)
    python build_data.py --start 2014-01-01 --out my.json

What it does
------------
1. Downloads dividend-adjusted ("Adj Close") and unadjusted ("Close") prices for every holding,
   the candidate instruments, benchmark proxies and factor proxies from Yahoo Finance (yfinance).
2. Downloads yields, spreads and the GBP risk-free rate from FRED (public CSV endpoint, no key).
3. Converts every series to GBP using Yahoo FX crosses (price in quote currency / units of quote
   currency per GBP). Pence-quoted LSE lines are divided by 100.
4. Aligns everything on a Monday-Friday calendar, forward-filling local holidays by at most 5 days,
   and writes one JSON the dashboard loads from its data drawer.
5. Splices a proxy for short-history holdings (EWSP -> RSP) and records the splice in metadata.

Nothing is fabricated: if a series cannot be downloaded it is written as null with a note.
Verify tickers before relying on the output; Yahoo symbols change.
"""
import argparse, datetime as dt, io, json, math, os, sys, time
import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is required: pip install yfinance")
try:
    import requests
except ImportError:
    requests = None

# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------
# key = dashboard ticker, yf = Yahoo symbol, ccy = quote currency (GBp = pence), proxy = optional splice
HOLDINGS = {
    'KNSL':     {'yf': 'KNSL',      'ccy': 'USD'},
    'TBCG.L':   {'yf': 'TBCG.L',    'ccy': 'GBp'},
    'EGO':      {'yf': 'EGO',       'ccy': 'USD'},
    'AVGO':     {'yf': 'AVGO',      'ccy': 'USD'},
    'EWSP.L':   {'yf': 'EWSP.L',    'ccy': 'GBP', 'proxy': {'yf': 'RSP', 'ccy': 'USD', 'note': 'Invesco S&P 500 Equal Weight (USD) spliced before the EWSP launch; fee and tracking differences ignored.'}},
    'ZPRX.DE':  {'yf': 'ZPRX.DE',   'ccy': 'EUR'},
    'VIE.PA':   {'yf': 'VIE.PA',    'ccy': 'EUR'},
    'ADYEN.AS': {'yf': 'ADYEN.AS',  'ccy': 'EUR'},
    'GD':       {'yf': 'GD',        'ccy': 'USD'},
    'ADBE':     {'yf': 'ADBE',      'ccy': 'USD'},
    'BIPS.L':   {'yf': 'BIPS.L',    'ccy': 'GBp'},
    'JAGI.L':   {'yf': 'JAGI.L',    'ccy': 'GBp'},
    'KAP.L':    {'yf': 'KAP.L',     'ccy': 'USD'},
    'TEZNY':    {'yf': 'TRN.MI',    'ccy': 'EUR', 'note': 'Terna ordinary shares (Milan) used for the OTC ADR; ADR_RATIO applies to price levels only, returns are unaffected.'},
    'NOV.DE':   {'yf': 'NOVO-B.CO', 'ccy': 'DKK', 'note': 'Novo Nordisk B (Copenhagen, DKK) used for the Xetra EUR line; GBP returns are equivalent.'},
    'SGLN.L':   {'yf': 'SGLN.L',    'ccy': 'GBp'},
    'THEON.AS': {'yf': 'THEON.AS',  'ccy': 'EUR'},
    'ACT.DE':   {'yf': 'ACT.DE',    'ccy': 'EUR'},
    'SSLN.L':   {'yf': 'SSLN.L',    'ccy': 'GBp'},
    'MGCI.L':   {'yf': 'MGCI.L',    'ccy': 'GBp'},
}
ADR_RATIO = {'TEZNY': 1.0}   # set to the ADR ratio (ordinary shares per ADR) if you want px_local to match the ADR quote
CANDIDATES = {
    'XDWH.L': {'yf': 'XDWH.L', 'ccy': 'USD'}, 'XDWS.L': {'yf': 'XDWS.L', 'ccy': 'USD'}, 'IGLT.L': {'yf': 'IGLT.L', 'ccy': 'GBp'}, 'IGLS.L': {'yf': 'IGLS.L', 'ccy': 'GBp'},
    'SJPA.L': {'yf': 'SJPA.L', 'ccy': 'USD'}, 'XDW0.L': {'yf': 'XDW0.L', 'ccy': 'USD'}, 'NDIA.L': {'yf': 'NDIA.L', 'ccy': 'USD'}, 'ASML.AS': {'yf': 'ASML.AS', 'ccy': 'EUR'},
    'HICL.L': {'yf': 'HICL.L', 'ccy': 'GBp'}, 'INXG.L': {'yf': 'INXG.L', 'ccy': 'GBp'}, 'IWQU.L': {'yf': 'IWQU.L', 'ccy': 'USD'}, 'ITX.MC': {'yf': 'ITX.MC', 'ccy': 'EUR'}, 'ERNS.L': {'yf': 'ERNS.L', 'ccy': 'GBp'},
    'AGBP.L': {'yf': 'AGBP.L', 'ccy': 'GBp', 'note': 'iShares Core Global Aggregate Bond GBP Hedged (used for the 80/20 and 60/40 blends).'},
}
BENCHMARKS = {   # dashboard key -> source (first that works)
    'MSCI_WORLD_GBP':    [{'yf': 'SWDA.L', 'ccy': 'GBp', 'note': 'iShares Core MSCI World UCITS ETF (GBP line) as MSCI World NTR GBP proxy (~0.20% fee drag).'}, {'yf': 'IWDA.L', 'ccy': 'USD'}, {'yf': 'URTH', 'ccy': 'USD'}],
    'GLOBAL_BONDS_GBPH': [{'yf': 'AGBP.L', 'ccy': 'GBp'}, {'yf': 'VAGS.L', 'ccy': 'GBp'}, {'yf': 'VAGP.L', 'ccy': 'GBp'}],
    'UK_GILTS':          [{'yf': 'IGLT.L', 'ccy': 'GBp'}, {'yf': 'VGOV.L', 'ccy': 'GBp'}],
}
FACTORS_YF = {   # price-based factor proxies (levels; the dashboard differences them)
    'GOLD_USD': 'GC=F', 'SILVER_USD': 'SI=F', 'BRENT': 'BZ=F', 'VIX': '^VIX', 'DXY': 'DX-Y.NYB',
    'GBPUSD': 'GBPUSD=X', 'GBPEUR': 'GBPEUR=X', 'EM': 'EEM', 'SEMIS': 'SMH', 'DEFENCE': 'ITA',
}
FACTOR_SPREADS = {'GROWTH_VALUE': ('VUG', 'VTV'), 'SMALL_LARGE': ('IWM', 'SPY')}   # ratio indices
URANIUM = [('U-UN.TO', 'CAD'), ('URA', 'USD')]   # first that works becomes URANIUM_PROXY (converted to USD)
FACTORS_FRED = {   # level series in percent
    'US10Y': ['DGS10'], 'US10Y_REAL': ['DFII10'], 'UK10Y': ['IRLTLT01GBM156N'], 'DE10Y': ['IRLTLT01DEM156N'],
    'US_HY_OAS': ['BAMLH0A0HYM2'], 'US_IG_OAS': ['BAMLC0A0CM'], 'EU_HY_OAS': ['BAMLHE00EHYIOAS'],
}
RISKFREE_FRED = ['IR3TIB01GBM156N', 'IRSTCB01GBM156N']   # UK 3-month interbank; fallback central-bank rate
RISKFREE_CONSTANT = 4.0   # % used only if FRED is unavailable (Bank Rate, 5 Nov 2026)
FX_PAIRS = {'USD': 'GBPUSD=X', 'EUR': 'GBPEUR=X', 'DKK': 'GBPDKK=X', 'CAD': 'GBPCAD=X', 'JPY': 'GBPJPY=X', 'CHF': 'GBPCHF=X'}

# ----------------------------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)

STOOQ_USED = []
def yf_history(symbol, start, retries=4):
    """Return DataFrame with columns ['close','adj'] indexed by date (naive)."""
    for attempt in range(retries):
        try:
            time.sleep(0.4)   # be gentle with Yahoo from shared CI runners
            t = yf.Ticker(symbol)
            h = t.history(start=start, auto_adjust=False, actions=False)
            if h is None or h.empty:
                h = t.history(period='max', auto_adjust=False, actions=False)
            if h is None or h.empty:
                return None
            h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
            close = h['Close'] if 'Close' in h.columns else None
            adj = h['Adj Close'] if 'Adj Close' in h.columns else None
            if adj is None:   # some yfinance versions only return adjusted data
                h2 = t.history(start=start, auto_adjust=True, actions=False)
                adj = h2['Close']; adj.index = pd.to_datetime(adj.index).tz_localize(None).normalize()
            df = pd.DataFrame({'close': close, 'adj': adj}).dropna(how='all')
            df = df[~df.index.duplicated(keep='last')].sort_index()
            return df
        except Exception as e:
            log(f"  retry {attempt+1} for {symbol}: {e}")
            time.sleep(3 * (attempt + 1))
    log(f"  Yahoo failed for {symbol}; trying stooq")
    df = stooq_history(symbol, start)
    if df is not None:
        STOOQ_USED.append(symbol)
    return df

STOOQ_SUFFIX = {'': '.us', '.L': '.uk', '.DE': '.de', '.PA': '.fr', '.AS': '.nl', '.MI': '.it', '.CO': '.dk', '.MC': '.es'}
def stooq_history(symbol, start):
    """Fallback daily closes from stooq.com (no API key). Price only: not dividend-adjusted."""
    if not requests or '=' in symbol or symbol.startswith('^'):
        return None
    base, suf = (symbol.rsplit('.', 1)[0], '.' + symbol.rsplit('.', 1)[1]) if '.' in symbol else (symbol, '')
    if suf not in STOOQ_SUFFIX:
        return None
    sq = (base + STOOQ_SUFFIX[suf]).lower()
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={sq}&i=d", timeout=30)
        if r.status_code != 200 or 'Date' not in r.text[:50]:
            return None
        df = pd.read_csv(io.StringIO(r.text), parse_dates=['Date']).set_index('Date').sort_index()
        df = df[df.index >= pd.Timestamp(start)]
        if df.empty:
            return None
        return pd.DataFrame({'close': df['Close'], 'adj': df['Close']})
    except Exception as e:
        log(f"  stooq {sq} failed: {e}")
        return None

def fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        if requests:
            r = requests.get(url, timeout=30); r.raise_for_status(); txt = r.text
            df = pd.read_csv(io.StringIO(txt))
        else:
            df = pd.read_csv(url)
        dcol = [c for c in df.columns if c.lower() in ('date', 'observation_date')][0]
        s = pd.to_numeric(df[series_id], errors='coerce'); s.index = pd.to_datetime(df[dcol]); s = s.dropna()
        return s.sort_index()
    except Exception as e:
        log(f"  FRED {series_id} failed: {e}")
        return None

def to_json_list(series, calendar):
    s = series.reindex(calendar)
    out = []
    for v in s.values:
        out.append(None if (v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))) else float(f"{v:.7g}"))
    return out


# Your statement prices on 16 Nov 2026, in each line's quote units (pence for GBp lines). Used to confirm the right line was downloaded.
SNAPSHOT_PX = {'KNSL': 395.89, 'TBCG.L': 5400.00, 'EGO': 31.58, 'AVGO': 347.01, 'EWSP.L': 6.12, 'ZPRX.DE': 72.22, 'VIE.PA': 34.76, 'ADYEN.AS': 872.80,
               'GD': 363.46, 'ADBE': 243.08, 'BIPS.L': 165.90, 'JAGI.L': 502.00, 'KAP.L': 60.30, 'TEZNY': 33.07, 'NOV.DE': 38.35, 'SGLN.L': 5696.00,
               'THEON.AS': 27.82, 'ACT.DE': 139.20, 'SSLN.L': 3444.82, 'MGCI.L': 90.33}
SNAPSHOT_DATE = '2026-11-16'
UNIT_FIXES, HEALTH = {}, []

def fix_unit_jumps(series):
    """Repair Yahoo's pence/pounds switches on London lines: a one-day move of ~100x is a unit change, not a price move."""
    vals = series.values.astype(float).copy(); fixes = 0; last = None
    for i in range(len(vals)):
        v = vals[i]
        if not np.isfinite(v) or v <= 0:
            continue
        if last is not None:
            r = v / last
            if r > 30: vals[i:] = vals[i:] / 100; fixes += 1
            elif r < 1 / 30: vals[i:] = vals[i:] * 100; fixes += 1
        last = vals[i]
    return pd.Series(vals, index=series.index), fixes

def health_check(key, cfg, conv):
    tr = conv['tr_local'].dropna(); px = conv['px_local']
    h = {'ticker': key, 'source': cfg['yf'], 'status': 'ok', 'checks': []}
    if tr.empty:
        h.update(status='missing', checks=['No data downloaded']); return h
    h['first'], h['last'] = tr.index[0].date().isoformat(), tr.index[-1].date().isoformat()
    if cfg['yf'] in STOOQ_USED:
        h['status'] = 'warning'; h['checks'].append('Yahoo failed; Stooq fallback used (price only, dividends not included)')
    if UNIT_FIXES.get(key):
        h['checks'].append(f"Repaired {UNIT_FIXES[key]} pence/pounds unit jump(s) in Yahoo's history")
    r = tr.pct_change().dropna(); big = r[r.abs() > 0.35]
    if len(big):
        h['status'] = 'warning'; h['checks'].append(f"{len(big)} daily move(s) above 35%; largest {big.abs().max():.0%} on {big.abs().idxmax().date()} - check for a data error")
    age = (pd.Timestamp.today().normalize() - tr.index[-1]).days
    if age > 7:
        h['status'] = 'warning'; h['checks'].append(f"Latest price is {age} days old")
    snap = SNAPSHOT_PX.get(key)
    if snap and cfg['yf'] == key:
        ref = snap / 100 if cfg['ccy'] == 'GBp' else snap
        v = px.loc[:pd.Timestamp(SNAPSHOT_DATE)].dropna()
        if len(v):
            ratio = v.iloc[-1] / ref
            if 80 < ratio < 120 or 1 / 120 < ratio < 1 / 80:
                h['checks'].append('Yahoo quotes this line in different units (pence vs pounds); returns are unaffected')
                ratio = ratio / 100 if ratio > 1 else ratio * 100
            dev = ratio - 1; h['statement_dev'] = round(float(dev), 4)
            if abs(dev) > 0.05:
                h['status'] = 'warning'; h['checks'].append(f"Price on 16 Nov differs from your statement by {dev:+.1%} - possibly the wrong line")
            else:
                h['checks'].append(f"Price on 16 Nov matches your statement ({dev:+.1%})")
    elif snap:
        h['checks'].append(f"History uses the home listing {cfg['yf']} (same company, GBP returns equivalent); statement-price check not applicable")
    return h

# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--start', default='2015-01-01'); ap.add_argument('--out', default='data/portfolio_data.json'); ap.add_argument('--csv', default='data/csv')
    args = ap.parse_args(); start = args.start
    notes, sources = [], []
    today = dt.date.today().isoformat()

    # 1. FX crosses (units of foreign currency per GBP)
    log('Downloading FX crosses...')
    fx = {}
    for ccy, sym in FX_PAIRS.items():
        df = yf_history(sym, start)
        if df is not None: fx[ccy] = df['close']; sources.append(f"{sym} (Yahoo) for {ccy}")
        else: notes.append(f"FX {sym} unavailable; {ccy}-quoted series could not be converted.")
    fx['GBP'] = None; fx['GBp'] = None

    # 2. Calendar
    end = pd.Timestamp(today)
    calendar = pd.bdate_range(pd.Timestamp(start), end)

    def to_gbp(df, ccy, note_key):
        """Convert close/adj to GBP on the calendar; returns dict of series."""
        local_close, local_adj = df['close'], df['adj']
        if ccy in ('GBp', 'GBP'):
            local_close, f1 = fix_unit_jumps(local_close); local_adj, f2 = fix_unit_jumps(local_adj)
            if f1 or f2: UNIT_FIXES[note_key] = max(f1, f2)
        if ccy == 'GBp': local_close, local_adj = local_close / 100.0, local_adj / 100.0; rate = None
        elif ccy == 'GBP': rate = None
        else:
            rate = fx.get(ccy)
            if rate is None: notes.append(f"{note_key}: no FX for {ccy}; GBP series omitted."); return None
        close = local_close.reindex(calendar).ffill(limit=5); adj = local_adj.reindex(calendar).ffill(limit=5)
        if rate is not None:
            r = rate.reindex(calendar).ffill(limit=5); px_gbp = close / r; tr_gbp = adj / r
        else:
            px_gbp, tr_gbp = close, adj
        return {'px_local': close, 'tr_local': adj, 'px_gbp': px_gbp, 'tr_gbp': tr_gbp}

    # 3. Holdings and candidates
    out_hold = {}
    for key, cfg in {**HOLDINGS, **CANDIDATES}.items():
        log(f"Downloading {key} ({cfg['yf']})...")
        df = yf_history(cfg['yf'], start)
        if df is None:
            notes.append(f"{key}: download failed for {cfg['yf']}; series written as null."); out_hold[key] = {'tr_gbp': None, 'source': cfg['yf'], 'ccy': cfg['ccy']}; continue
        conv = to_gbp(df, cfg['ccy'], key)
        if conv is None: out_hold[key] = {'tr_gbp': None, 'source': cfg['yf'], 'ccy': cfg['ccy']}; continue
        if key in ADR_RATIO and ADR_RATIO[key] != 1.0:
            conv['px_local'] = conv['px_local'] * ADR_RATIO[key]; conv['px_gbp'] = conv['px_gbp'] * ADR_RATIO[key]
        proxy_note = None
        if cfg.get('proxy'):
            p = cfg['proxy']; pdf = yf_history(p['yf'], start)
            if pdf is not None:
                pconv = to_gbp(pdf, p['ccy'], key + ' proxy')
                if pconv is not None:
                    first_real = conv['tr_gbp'].first_valid_index()
                    if first_real is not None and pconv['tr_gbp'].loc[:first_real].notna().sum() > 20:
                        scale = conv['tr_gbp'].loc[first_real] / pconv['tr_gbp'].loc[first_real]
                        spliced = conv['tr_gbp'].copy(); before = pconv['tr_gbp'].loc[:first_real - pd.Timedelta(days=1)] * scale
                        spliced.loc[before.index] = before.values
                        conv['tr_gbp'] = spliced; proxy_note = f"{p['yf']} spliced before {first_real.date()}: {p.get('note','')}"
                        notes.append(f"{key}: {proxy_note}")
        first = conv['tr_gbp'].first_valid_index()
        out_hold[key] = {'tr_gbp': to_json_list(conv['tr_gbp'], calendar), 'tr_local': to_json_list(conv['tr_local'], calendar), 'px_local': to_json_list(conv['px_local'], calendar), 'px_gbp': to_json_list(conv['px_gbp'], calendar),
                         'ccy': cfg['ccy'], 'source': f"Yahoo Finance {cfg['yf']} adjusted close / FX", 'proxy': proxy_note, 'first': first.date().isoformat() if first is not None else None, 'note': cfg.get('note')}
        sources.append(f"{cfg['yf']} (Yahoo Finance)")
        if key in HOLDINGS: HEALTH.append(health_check(key, cfg, conv))

    # 4. Benchmarks
    out_bench = {}
    for key, opts in BENCHMARKS.items():
        got = False
        for o in opts:
            log(f"Downloading benchmark {key} ({o['yf']})...")
            df = yf_history(o['yf'], start)
            if df is None: continue
            conv = to_gbp(df, o['ccy'], key)
            if conv is None: continue
            out_bench[key] = to_json_list(conv['tr_gbp'], calendar); sources.append(f"{o['yf']} (Yahoo) as {key}"); notes.append(f"{key}: {o.get('note', o['yf'] + ' total return in GBP')}"); got = True; break
        if not got: notes.append(f"Benchmark {key} unavailable from {[o['yf'] for o in opts]}.")

    # 5. Factors
    out_fac = {}
    for key, sym in FACTORS_YF.items():
        log(f"Downloading factor {key} ({sym})...")
        df = yf_history(sym, start)
        if df is None: notes.append(f"Factor {key} ({sym}) unavailable."); continue
        s = df['close']
        if key in ('EM', 'SEMIS', 'DEFENCE'): s = df['adj']   # total return for equity proxies
        out_fac[key] = to_json_list(s.reindex(calendar).ffill(limit=5), calendar); sources.append(f"{sym} (Yahoo) as {key}")
    for key, (a, b) in FACTOR_SPREADS.items():
        da, db = yf_history(a, start), yf_history(b, start)
        if da is None or db is None: notes.append(f"Factor {key} unavailable."); continue
        ratio = (da['adj'].reindex(calendar).ffill(limit=5) / db['adj'].reindex(calendar).ffill(limit=5))
        out_fac[key] = to_json_list(ratio / ratio.dropna().iloc[0] * 100, calendar); sources.append(f"{a}/{b} (Yahoo) as {key}")
    for sym, ccy in URANIUM:
        df = yf_history(sym, start)
        if df is None: continue
        s = df['close'].reindex(calendar).ffill(limit=5)
        if ccy != 'USD' and fx.get(ccy) is not None and fx.get('USD') is not None:
            s = s / fx[ccy].reindex(calendar).ffill(limit=5) * fx['USD'].reindex(calendar).ffill(limit=5)
        out_fac['URANIUM_PROXY'] = to_json_list(s, calendar); notes.append(f"URANIUM_PROXY uses {sym} (no free continuous U3O8 series)."); sources.append(f"{sym} (Yahoo) as URANIUM_PROXY"); break
    for key, ids in FACTORS_FRED.items():
        for sid in ids:
            log(f"Downloading FRED {key} ({sid})...")
            s = fred_series(sid)
            if s is not None and len(s) > 10:
                out_fac[key] = to_json_list(s.reindex(calendar).ffill(limit=40), calendar); sources.append(f"FRED {sid} as {key}"); break
        else: notes.append(f"Factor {key} unavailable from FRED {ids}.")

    # 6. Risk-free
    rf = None
    for sid in RISKFREE_FRED:
        s = fred_series(sid)
        if s is not None and len(s) > 10: rf = s.reindex(calendar).ffill(limit=40); sources.append(f"FRED {sid} as GBP risk-free"); notes.append(f"Risk-free: FRED {sid} (annual %, monthly, forward-filled)."); break
    if rf is None:
        rf = pd.Series(RISKFREE_CONSTANT, index=calendar); notes.append(f"Risk-free: constant {RISKFREE_CONSTANT}% (FRED unavailable).")

    notes.append('Calendar: Monday-Friday; local holidays forward-filled up to 5 business days, so daily returns on those days are zero for the affected market.')
    notes.append('GBP conversion: price in quote currency divided by units of quote currency per GBP (Yahoo crosses); pence-quoted lines divided by 100.')

    done = {x['ticker'] for x in HEALTH}
    for k, cfg in HOLDINGS.items():
        if k not in done: HEALTH.append({'ticker': k, 'source': cfg['yf'], 'status': 'missing', 'checks': ['Download failed; this holding is excluded from the analysis until the next successful run']})
    order = list(HOLDINGS); HEALTH.sort(key=lambda x: order.index(x['ticker']))
    log('Data health: ' + ', '.join(f"{x['ticker']}={x['status']}" for x in HEALTH))
    data = {'meta': {'health': HEALTH, 'generated': dt.datetime.now().isoformat(timespec='seconds'), 'base_currency': 'GBP', 'start': start, 'notes': notes, 'sources': sorted(set(sources)), 'snapshot_date': '2026-11-16'},
            'dates': [d.date().isoformat() for d in calendar], 'holdings': out_hold, 'benchmarks': out_bench, 'factors': out_fac, 'riskfree': to_json_list(rf, calendar)}
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w') as f: json.dump(data, f, separators=(',', ':'))
    log(f"Wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB)")

    # 7. CSV copies
    os.makedirs(args.csv, exist_ok=True)
    idx = [d.date().isoformat() for d in calendar]
    pd.DataFrame({k: v['tr_gbp'] for k, v in out_hold.items() if v.get('tr_gbp')}, index=idx).to_csv(os.path.join(args.csv, 'prices_gbp_tr.csv'), index_label='Date')
    if out_bench: pd.DataFrame(out_bench, index=idx).to_csv(os.path.join(args.csv, 'benchmarks.csv'), index_label='Date')
    if out_fac: pd.DataFrame(out_fac, index=idx).to_csv(os.path.join(args.csv, 'factors.csv'), index_label='Date')
    pd.DataFrame({'RF': data['riskfree']}, index=idx).to_csv(os.path.join(args.csv, 'riskfree.csv'), index_label='Date')
    log('CSV copies written to ' + args.csv)
    if STOOQ_USED:
        notes.append('Stooq fallback (price only, dividends NOT included) used for: ' + ', '.join(STOOQ_USED))
        with open(args.out) as f: d2 = json.load(f)
        d2['meta']['notes'] = notes
        with open(args.out, 'w') as f: json.dump(d2, f, separators=(',', ':'))
    missing = [k for k, v in out_hold.items() if not v.get('tr_gbp')]
    loaded = [k for k in HOLDINGS if out_hold.get(k, {}).get('tr_gbp')]
    log(f"Loaded {len(loaded)}/{len(HOLDINGS)} holdings")
    if len(loaded) < len(HOLDINGS) // 2:
        sys.exit('Too few holdings loaded; failing so the workflow keeps the last good data.')
    if missing: log('WARNING: no data for ' + ', '.join(missing))
    log('\n'.join('- ' + n for n in notes))

if __name__ == '__main__':
    main()
