"""诊断5：官方 SmaCross 示例在 0.6.6 上的真实交易数"""
import warnings
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA, GOOG

class SmaCross(Strategy):
    n1 = 10
    n2 = 20
    def init(self):
        close = self.data.Close
        self.sma1 = self.I(SMA, close, self.n1)
        self.sma2 = self.I(SMA, close, self.n2)
    def next(self):
        if crossover(self.sma1, self.sma2):
            self.buy()
        elif crossover(self.sma2, self.sma1):
            self.sell()

bt = Backtest(GOOG, SmaCross, cash=10000, commission=.002)
stats = bt.run()
print("官方示例 stats['# Trades']:", stats["# Trades"])
trades = stats["_strategy"].trades
print("trades 记录:", len(trades))
for t in trades:
    print(" ", t)
