"""rqalpha 多股票批量回测：5/20 双均线 × 6 只 A股（真实规则：T+1/涨跌停/印花税/100股整数倍）"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
from rqalpha import run

STOCKS = [
    ("600519.XSHG", "贵州茅台"),
    ("000001.XSHE", "平安银行"),
    ("300750.XSHE", "宁德时代"),
    ("601318.XSHG", "中国平安"),
    ("002594.XSHE", "比亚迪"),
    ("600036.XSHG", "招商银行"),
]

CODE_TEMPLATE = """
def init(context):
    context.s1 = "{symbol}"
    context.SHORT = 5
    context.LONG = 20
    subscribe(context.s1)

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.LONG + 1, "1d", "close")
    if prices is None or len(prices) < context.LONG + 1:
        return
    short = prices[-context.SHORT:].mean()
    long = prices[-context.LONG:].mean()
    prev_short = prices[-context.SHORT - 1:-1].mean()
    prev_long = prices[-context.LONG - 1:-1].mean()
    pos = get_position(context.s1)
    if short > long and prev_short <= prev_long and pos.quantity == 0:
        order_target_percent(context.s1, 0.99)
    elif short < long and prev_short >= prev_long and pos.quantity > 0:
        order_target_percent(context.s1, 0)
"""


def run_one(symbol):
    config = {
        "base": {
            "data_bundle_path": "/root/.rqalpha/bundle",
            "start_date": "2023-01-01",
            "end_date": "2026-08-15",
            "accounts": {"stock": 1000000},
            "frequency": "1d",
            "run_type": "b",
        },
        "extra": {"log_level": "error"},
        "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": True}},
    }
    results = run(config, source_code=CODE_TEMPLATE.format(symbol=symbol))
    s = results["sys_analyser"]["summary"]
    n_trades = len(results["sys_analyser"]["trades"])
    return s, n_trades


def bh_return(symbol_bs, start="2023-01-01", end="2026-08-15"):
    """买入持有收益（baostock 前复权，参考值）"""
    import baostock as bs
    bs.login()
    rs = bs.query_history_k_data_plus(
        symbol_bs, "date,close",
        start_date=start, end_date=end, frequency="d", adjustflag="2")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    if len(rows) < 2:
        return None
    return (float(rows[-1][1]) / float(rows[0][1]) - 1) * 100


rows = []
for symbol, name in STOCKS:
    print(f"回测中: {name} ({symbol}) ...", flush=True)
    try:
        s, n_trades = run_one(symbol)
        sharpe = s["sharpe"]
        rows.append({
            "股票": name,
            "代码": symbol.split(".")[0],
            "策略收益%": round(s["total_returns"] * 100, 2),
            "年化%": round(s["annualized_returns"] * 100, 2),
            "最大回撤%": round(s["max_drawdown"] * 100, 2),
            "夏普": round(sharpe, 2) if sharpe == sharpe else float("nan"),
            "胜率%": round(s["win_rate"] * 100, 1),
            "交易数": n_trades,
        })
    except Exception as e:
        print(f"  ✗ {name} 失败: {type(e).__name__}: {e}", flush=True)

# 买入持有（baostock 前复权）
for r in rows:
    code = r["代码"]
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    bh = bh_return(f"{prefix}.{code}")
    r["买入持有%"] = round(bh, 2) if bh is not None else None

df = pd.DataFrame(rows)
pd.set_option("display.unicode.east_asian_width", True)
print("\n" + "=" * 100)
print(df.to_string(index=False))
print("=" * 100)
print("注: 策略收益为 rqalpha 真实规则版(100万资金, T+1/涨跌停/印花税0.05%/佣金万8/100股整数倍)")
print("    买入持有为 baostock 前复权参考值(2023-01 ~ 2026-08)")
