"""rqalpha 跑 qlib 选股计划（真实规则版）"""
import warnings
import sys, os
warnings.filterwarnings("ignore")
from rqalpha import run

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BUNDLE_PATH, STRATEGY_FILE_ZZ500

config = {
    "base": {
        "data_bundle_path": BUNDLE_PATH,
        "start_date": "2025-01-01",
        "end_date": "2026-08-14",
        "accounts": {"stock": 1000000},
        "frequency": "1d",
        "run_type": "b",
        "strategy_file": STRATEGY_FILE_ZZ500,
    },
    "extra": {"log_level": "error"},
    "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": True}},
}

results = run(config)
analyser = results["sys_analyser"]
s = analyser["summary"]
print("\n=== 真实规则回测结果 ===")
for k in ("total_returns", "annualized_returns", "max_drawdown", "sharpe",
          "win_rate", "turnover", "total_value", "cash"):
    print(f"  {k}: {s.get(k)}")
print(f"  交易笔数: {len(analyser['trades'])}")
print(f"  期末持仓: {len(analyser['stock_positions'])} 只")
