"""诊断3：打印茅台每个 crossover 信号 + 持仓状态"""
import warnings; warnings.filterwarnings("ignore")
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from demo_backtest import fetch_cn_bs

class LogStrategy(Strategy):
    n_fast = 5
    n_slow = 20

    def init(self):
        self.sma_fast = self.I(lambda x: pd.Series(x).rolling(self.n_fast).mean(), self.data.Close)
        self.sma_slow = self.I(lambda x: pd.Series(x).rolling(self.n_slow).mean(), self.data.Close)

    def next(self):
        d = self.data.index[-1]
        if crossover(self.sma_fast, self.sma_slow):
            print(f"{d.date()} 金叉信号 -> buy() 调用 | 持仓={self.position.size}")
            self.buy()
        elif crossover(self.sma_slow, self.sma_fast):
            print(f"{d.date()} 死叉信号 -> sell() 调用 | 持仓={self.position.size}")
            self.sell()

data = fetch_cn_bs("sh.600519", "2023-01-01", "2026-08-15")
bt = Backtest(data, LogStrategy, cash=100_000, commission=0.0003)
stats = bt.run()
trades = stats["_strategy"].trades
print(f"\n最终 trades: {len(trades)} 笔")
for t in trades:
    print(f"  {t}")
