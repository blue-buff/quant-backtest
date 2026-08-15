"""A股/港股 最小回测 demo v2 —— 双均线金叉死叉策略
数据: A股=baostock | 港股=腾讯行情接口 (东财接口在云服务器IP上被断连)
回测: backtesting.py
"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
import requests
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

# ---------- 数据获取 ----------
def fetch_cn_bs(symbol_bs: str, start: str, end: str) -> pd.DataFrame:
    """A股日线（前复权, adjustflag=2），baostock 免费源"""
    import baostock as bs
    lg = bs.login()
    rs = bs.query_history_k_data_plus(
        symbol_bs, "date,open,high,low,close,volume",
        start_date=start, end_date=end, frequency="d", adjustflag="2")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return df.set_index("Date")

def fetch_hk_qq(symbol: str, start: str, end: str) -> pd.DataFrame:
    """港股日线（前复权 qfq），腾讯行情接口"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{symbol},day,{start},{end},1000,qfq"}
    r = requests.get(url, params=params, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()["data"][symbol]
    klines = data.get("qfqday") or data.get("day")
    rows = [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
            for k in klines]
    df = pd.DataFrame(rows, columns=["Date", "Open", "Close", "High", "Low", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]

# ---------- 策略 ----------
class SmaCross(Strategy):
    """双均线：快线上穿慢线买入，下穿卖出"""
    n_fast = 5
    n_slow = 20

    def init(self):
        self.sma_fast = self.I(lambda x: pd.Series(x).rolling(self.n_fast).mean(), self.data.Close)
        self.sma_slow = self.I(lambda x: pd.Series(x).rolling(self.n_slow).mean(), self.data.Close)

    def next(self):
        # 注意: 0.6.x 的坑——默认 size 是比例型(0.9999)，按 margin_available 算股数，
        # 全仓买入后 margin≈0，裸 sell() 会被静默取消。平仓必须用 position.close()
        if crossover(self.sma_fast, self.sma_slow) and not self.position:
            self.buy()  # 全仓买入
        elif crossover(self.sma_slow, self.sma_fast) and self.position:
            self.position.close()  # 显式平仓

# ---------- 运行 ----------
def main():
    START, END = "2023-01-01", "2026-08-15"
    CASH = 100_000

    cases = [
        ("A股 贵州茅台", "sh.600519", fetch_cn_bs),
        ("港股 腾讯控股", "hk00700", fetch_hk_qq),
    ]

    for name, symbol, fetcher in cases:
        print("=" * 64)
        print(f"标的: {name} ({symbol})  区间: {START[:4]}-{END[:4]}")
        try:
            data = fetcher(symbol, START, END)
            if len(data) < 70:
                print(f"  数据不足 {len(data)} 行，跳过"); continue
            print(f"  数据 {len(data)} 行 | 首日 {data.index[0].date()} 收 {data['Close'].iloc[0]:.2f}"
                  f" | 末日 {data.index[-1].date()} 收 {data['Close'].iloc[-1]:.2f}")
            bt = Backtest(data, SmaCross, cash=CASH, commission=0.0003)  # 佣金万3
            stats = bt.run()
            print(f"  策略总收益 : {stats['Return [%]']:8.2f}%   买入持有: {stats['Buy & Hold Return [%]']:.2f}%")
            print(f"  年化收益   : {stats['Return (Ann.) [%]']:8.2f}%   夏普比率: {stats['Sharpe Ratio']:.2f}")
            print(f"  最大回撤   : {stats['Max. Drawdown [%]']:8.2f}%   胜率   : {stats['Win Rate [%]']:.1f}%")
            print(f"  交易次数   : {stats['# Trades']}  |  终值: {stats['Equity Final [$]']:,.0f} 元")
        except Exception as e:
            print(f"  ✗ 失败: {type(e).__name__}: {e}")

    print("=" * 64)
    print("提示: 最简 demo——未处理 A股 T+1/涨跌停/停牌/分红再投, 未含印花税; 港股未含交易所费/印花税。")

if __name__ == "__main__":
    main()
