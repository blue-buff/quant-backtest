"""rqalpha 双均线策略（A股规则版：T+1/涨跌停/印花税由 rqalpha 撮合内置）"""
from rqalpha.apis import *


def init(context):
    context.s1 = "600519.XSHG"          # 贵州茅台
    context.SHORT = 5
    context.LONG = 20
    subscribe(context.s1)               # 关键：rqalpha 6.x 需显式订阅，否则 bar_dict 拿不到该标的、下单不成交


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.LONG + 1, "1d", "close")
    if prices is None or len(prices) < context.LONG + 1:
        return

    short_avg = prices[-context.SHORT:].mean()
    long_avg = prices[-context.LONG:].mean()
    prev_short = prices[-context.SHORT - 1:-1].mean()
    prev_long = prices[-context.LONG - 1:-1].mean()

    pos = get_position(context.s1)

    # 金叉：满仓买入
    if short_avg > long_avg and prev_short <= prev_long and pos.quantity == 0:
        order_target_percent(context.s1, 0.99)
    # 死叉：清仓（T+1 当日买入的仓位 rqalpha 会拒绝卖出）
    elif short_avg < long_avg and prev_short >= prev_long and pos.quantity > 0:
        order_target_percent(context.s1, 0)
