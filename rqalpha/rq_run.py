"""用 Python API 跑 rqalpha 回测，直接拿结果字典"""
import warnings
import sys, os
warnings.filterwarnings("ignore")
from rqalpha import run

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BUNDLE_PATH, RQALPHA_STRATEGY_FILE

config = {
    "base": {
        "data_bundle_path": BUNDLE_PATH,
        "start_date": "2023-01-01",
        "end_date": "2026-08-15",
        "accounts": {"stock": 1000000},
        "frequency": "1d",
        "run_type": "b",
        "strategy_file": RQALPHA_STRATEGY_FILE,
    },
    "extra": {"log_level": "error"},
    "mod": {
        "sys_progress": {"enabled": False},
        "sys_analyser": {"enabled": True},
    },
}

results = run(config)
analyser = results.get("sys_analyser", {}) if results else {}

summary = analyser.get("summary", {})
print("\n=== 绩效摘要 ===")
for k in ("total_returns", "annualized_returns", "max_drawdown", "sharpe",
          "total_value", "cash", "win_rate", "turnover", "alpha", "beta", "volatility"):
    print(f"  {k}: {summary.get(k)}")

trades = analyser.get("trades")
print("\n=== trades ===", "无" if trades is None or len(trades) == 0 else f"{len(trades)} 笔")
if trades is not None and len(trades):
    print("trades 列名:", list(trades.columns))
    print(trades.head(8).to_string())

pos = analyser.get("stock_positions")
print("\n=== 期末持仓 ===", "无" if pos is None or len(pos) == 0 else pos)
