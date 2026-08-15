"""诊断 rqalpha v2：subscribe 后 bar_dict/订单/持仓状态"""
import warnings
warnings.filterwarnings("ignore")
from rqalpha import run

CODE = """
def init(context):
    context.s1 = "600519.XSHG"
    context.SHORT = 5
    context.LONG = 20
    context.cnt = 0
    subscribe(context.s1)

def handle_bar(context, bar_dict):
    context.cnt += 1
    has_bar = context.s1 in bar_dict
    close = bar_dict[context.s1].close if has_bar else None
    prices = history_bars(context.s1, context.LONG + 1, "1d", "close")
    pos = get_position(context.s1).quantity

    if context.cnt <= 2 or context.cnt % 50 == 0:
        print(f"[bar {context.cnt}] {context.now.date()} has_bar={has_bar} close={close} pos={pos}")

    if prices is not None and len(prices) >= context.LONG + 1:
        short = prices[-5:].mean()
        long = prices[-20:].mean()
        prev_short = prices[-6:-1].mean()
        prev_long = prices[-21:-1].mean()
        if short > long and prev_short <= prev_long and pos == 0:
            from rqalpha.environment import Environment
            env = Environment.get_instance()
            lp = env.data_proxy.get_last_price(context.s1)
            print(f"[金叉] {context.now.date()} bar_close={close} get_last_price={lp} -> 下单")
            o = order_target_percent(context.s1, 0.99)
            print(f"    返回: {o}")
    if pos > 0 and context.cnt % 20 == 0:
        print(f"    [持仓中] {context.now.date()} pos={pos} 市值={context.portfolio.market_value:.0f}")
"""

config = {
    "base": {
        "data_bundle_path": "/root/.rqalpha/bundle",
        "start_date": "2023-01-01",
        "end_date": "2023-04-30",
        "accounts": {"stock": 100000},
        "frequency": "1d",
        "run_type": "b",
    },
    "extra": {"log_level": "error"},
    "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": False}},
}

run(config, source_code=CODE)
