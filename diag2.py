import warnings; warnings.filterwarnings("ignore")
from backtesting import Backtest
from demo_backtest import SmaCross, fetch_cn_bs

data = fetch_cn_bs("sh.600519", "2023-01-01", "2026-08-15")
bt = Backtest(data, SmaCross, cash=100_000, commission=0.0003)
stats = bt.run()
tr = stats["_strategy"].trades
print("type:", type(tr))
print("repr head:", repr(tr)[:300])
if hasattr(tr, "columns"):
    print("columns:", list(tr.columns))
elif isinstance(tr, tuple):
    for i, t in enumerate(tr):
        print(f"tuple[{i}]: type={type(t)}")
        print("  ", repr(t)[:400])
# 权益曲线
eq = stats.get("_equity_curve")
print("equity key:", type(eq))
