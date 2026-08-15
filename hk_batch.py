"""港股多股票回测：5/20 双均线 × 3 只港股（backtesting.py，腾讯接口数据）"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
from backtesting import Backtest
from demo_backtest import SmaCross, fetch_hk_qq

STOCKS = [
    ("hk00700", "腾讯控股"),
    ("hk03690", "美团"),
    ("hk01810", "小米集团"),
]

rows = []
for symbol, name in STOCKS:
    print(f"回测中: {name} ({symbol}) ...", flush=True)
    try:
        data = fetch_hk_qq(symbol, "2023-01-01", "2026-08-15")
        bt = Backtest(data, SmaCross, cash=1_000_000, commission=0.0003)
        stats = bt.run()
        bh = stats["Buy & Hold Return [%]"]
        rows.append({
            "股票": name,
            "代码": symbol[2:],
            "策略收益%": round(stats["Return [%]"], 2),
            "年化%": round(stats["Return (Ann.) [%]"], 2),
            "最大回撤%": round(stats["Max. Drawdown [%]"], 2),
            "夏普": round(stats["Sharpe Ratio"], 2),
            "胜率%": round(stats["Win Rate [%]"], 1) if stats["Win Rate [%]"] == stats["Win Rate [%]"] else float("nan"),
            "交易数": int(stats["# Trades"]),
            "买入持有%": round(bh, 2),
        })
    except Exception as e:
        print(f"  ✗ {name} 失败: {type(e).__name__}: {e}", flush=True)

df = pd.DataFrame(rows)
pd.set_option("display.unicode.east_asian_width", True)
print("\n" + "=" * 100)
print(df.to_string(index=False))
print("=" * 100)
print("注: 港股为 backtesting.py 无 A股规则版(100万资金, 佣金万3)，未含印花税0.1%/交易费；与 A股 rqalpha 版口径不同")
