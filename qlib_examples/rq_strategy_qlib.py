"""rqalpha 真实规则回测：按 qlib 月度调仓计划执行（T+1/涨跌停/印花税/100股整数倍/佣金）

OPTIMIZATION.md 修复项：
- P1-3 rank-buffer：跌出 top-(K+N) 的持仓不在计划目标列表 → 被卖出；
  仍在前 K+N 的留存持仓 → 继续持有（计划由 planlib.build_plan_with_buffer 生成）
- P2-1 topping-up：留存持仓每次调仓也调回目标权重（不再放任权重漂移）
- P2-4/A4 停牌/涨跌停重试：买入未成交进入重试队列，非调仓日重试至 N 天
- A3（P1-2/3 对比审查）: 滑点与参与率改由官方 sys_simulation 配置
  （PriceRatioSlippage 夹涨跌停；volume_limit 按当日累计），策略不再自研

环境变量（qbt backtest 自动注入，均可配）:
  QBT_PLAN_FILE      调仓计划 CSV 路径（rqalpha exec 加载，__file__ 不可靠）
  QBT_RETRY_DAYS     未成交买单重试天数上限（默认 2）
  滑点/参与率见 qbt.yaml backtest.slippage / backtest.participation（官方撮合生效）
"""
import os

import pandas as pd
from rqalpha.apis import *


# ---------- 纯函数（pytest 直接测试） ----------

def target_quantity(portfolio_value, weight, price):
    """目标股数：目标市值 / 价格，向下取 100 股整数倍（A股最小交易单位）"""
    if price is None or price <= 0 or portfolio_value is None or weight is None:
        return 0
    return int(portfolio_value * weight / price / 100) * 100


# ---------- 策略主体 ----------

def init(context):
    plan_path = os.environ.get("QBT_PLAN_FILE", "rebalance_plan.csv")
    df = pd.read_csv(plan_path, header=None)
    context.plan = {
        str(row[0]): [s for s in row[1:] if isinstance(s, str) and s]
        for _, row in df.iterrows()
    }
    all_symbols = set()
    for v in context.plan.values():
        all_symbols.update(v)
    for s in all_symbols:
        subscribe(s)
    # P2-4/A4: sym -> [目标权重, 剩余重试天数]
    context.pending_buys = {}
    # P1-2/3（对比审查）: 滑点与参与率由官方 sys_simulation 配置，策略不再自研
    context.retry_days = int(os.environ.get("QBT_RETRY_DAYS", "2"))
    context.logged = set()


def _buy_to_weight(context, bar_dict, symbol, weight):
    """按目标权重买入，返回是否成功下单。

    P2-1 topping-up：下单量 = 目标股数 - 已持仓（留存持仓补回目标权重，
    避免超配）；已超配时不下单（减仓由卖出逻辑负责）。
    bar_dict[symbol] 无当日 bar（停牌等）→ 返回 False，由调用方决定重试。
    """
    try:
        bar = bar_dict[symbol]
    except (KeyError, TypeError):
        return False
    if bar.close is None or bar.close <= 0:
        return False
    # P1-2/3: 挂 close 限价单；滑点与参与率由官方 sys_simulation 撮合处理
    qty = target_quantity(context.portfolio.total_value, weight, bar.close)
    qty -= get_position(symbol).quantity or 0  # P2-1: 补差
    if qty <= 0:
        return False
    order(symbol, qty, style=LimitOrder(round(bar.close, 3)))
    return True


def _sell_all(context, bar_dict, symbol):
    """清仓卖出（挂 close 限价单；滑点由官方撮合处理）"""
    pos = get_position(symbol)
    if pos.quantity <= 0:
        return
    try:
        bar = bar_dict[symbol]
    except (KeyError, TypeError):
        return
    if bar.close is None or bar.close <= 0:
        return
    order(symbol, -pos.quantity, style=LimitOrder(round(bar.close, 3)))


def _retry_pending(context, bar_dict):
    """P2-4/A4: 未成交买单在非调仓日重试，直至成交或超过重试上限"""
    for symbol in list(context.pending_buys.keys()):
        if get_position(symbol).quantity > 0:
            del context.pending_buys[symbol]
            continue
        weight, days_left = context.pending_buys[symbol]
        if days_left <= 0:
            del context.pending_buys[symbol]
            continue
        if _buy_to_weight(context, bar_dict, symbol, weight):
            context.pending_buys[symbol] = [weight, days_left - 1]
        else:
            del context.pending_buys[symbol]  # 无行情（长期停牌）放弃重试


def handle_bar(context, bar_dict):
    today = context.now.strftime("%Y-%m-%d")
    _retry_pending(context, bar_dict)

    if today not in context.plan:
        return
    target = set(context.plan[today])
    # P0-1（代码对比审查）: 排除 ST（风险警示股，机构禁投）
    target = {s for s in target if not is_st_stock(s)}

    # 卖出不在目标列表的持仓（P1-3: 跌出 buffer 的才不在目标列表）
    for order_book_id in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[order_book_id]
        if order_book_id not in target and pos.quantity > 0:
            _sell_all(context, bar_dict, order_book_id)

    weight = 0.99 / max(len(target), 1)
    for s in target:
        if get_position(s).quantity == 0:
            # 新买入；未成交则进入重试队列（P2-4/A4）
            if _buy_to_weight(context, bar_dict, s, weight):
                context.pending_buys[s] = [weight, context.retry_days]
        else:
            # P2-1 topping-up：留存持仓也补回目标权重
            _buy_to_weight(context, bar_dict, s, weight)

    if today not in context.logged:
        context.logged.add(today)
        print(f"[调仓] {today} 目标 {len(target)} 只 | 重试 {context.retry_days} 天")
