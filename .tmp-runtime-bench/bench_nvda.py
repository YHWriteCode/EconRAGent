from pathlib import Path
import json
import numpy as np
import pandas as pd
import yfinance as yf
out = Path('/workspace/output')
out.mkdir(parents=True, exist_ok=True)
df = yf.download('NVDA', start='2023-01-01', auto_adjust=True, progress=False)
if isinstance(df.columns, pd.MultiIndex):
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
df = df.reset_index()
df.columns = [str(c).lower() for c in df.columns]
if 'date' not in df.columns:
    df = df.rename(columns={df.columns[0]: 'date'})
df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
df = df[['date', 'close', 'volume']].copy()
df['r1'] = df['close'].pct_change()
df['r5'] = df['close'].pct_change(5)
df['r20'] = df['close'].pct_change(20)
df['vol20'] = df['r1'].rolling(20).std()
df['ma20_gap'] = df['close'] / df['close'].rolling(20).mean() - 1.0
df['target'] = df['close'].shift(-1) / df['close'] - 1.0
m = df.dropna().copy()
s = max(80, int(len(m) * 0.7)); s = min(s, len(m) - 20)
tr, te = m.iloc[:s].copy(), m.iloc[s:].copy()
cols = ['r1', 'r5', 'r20', 'vol20', 'ma20_gap']
coef = np.linalg.lstsq(np.c_[np.ones(len(tr)), tr[cols]], tr['target'], rcond=None)[0]
te['pred'] = np.c_[np.ones(len(te)), te[cols]] @ coef
te['sig'] = (te['pred'] > 0).astype(int)
te['sr'] = te['sig'] * te['target']
te['eq'] = (1 + te['sr'].fillna(0)).cumprod()
(out / 'nvda_report.json').write_text(json.dumps({'rows': int(len(df)), 'test_rows': int(len(te)), 'total_return': float(te['eq'].iloc[-1] - 1)}), encoding='utf-8')
print('done')
