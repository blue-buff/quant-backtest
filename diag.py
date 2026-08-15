"""诊断：打印每笔交易明细，弄清 0 交易/-100% 的真相"""
import warnings; warnings.filterwarnings("ignore")
import pandas as pd
from backtesting import Backtest
from demo_backtest import SmaCross, fetch_cn_bs, fetch_hk_qq

for name, symbol, fetcher in [
    ("A股 茅台", "sh.600519", fetch_cn_bs),
    ("港股 腾讯", "hk00700", fetch_hk_qq),
]:
    data = fetcher(symbol, "2023-01-01", "2026-08-15")
    print("=" * 60)
    print(f"{name} | 行数 {len(data)} | 收盘区间 {data['Close'].min():.2f} ~ {data['Close'].max():.2f}")
    bt = Backtest(data, SmaCross, cash=100_000, commission=0.0003)
    stats = bt.run()
    trades = stats["_strategy"].trades
    print(f"trades 记录数: {len(trades)}")
    if len(trades):
        with pd.option_context("display.width", 150):
            print(trades[["Size", "EntryBar", "ExitBar", "EntryPrice", "ExitPrice", "PnL", "ReturnPct"]].head(15).to_string())
        print(f"PnL 合计: {trades['PnL'].sum():.2f} | ReturnPct 合计: {trades['ReturnPct'].sum()*100:.1f}%")
    # 权益曲线采样
    eq = stats["_equity_curve"]
    print(f"权益: 初始 100,000 | 峰值 {eq['Equity'].max():,.0f} | 终值 {eq['Equity'].iloc[-1]:,.0f}")
