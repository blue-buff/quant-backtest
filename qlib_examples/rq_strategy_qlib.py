"""rqalpha 真实规则回测：按 qlib LightGBM 每月 top50 调仓计划执行
真实规则: T+1 / 涨跌停 / 印花税 / 100股整数倍 / 佣金，由 rqalpha 撮合处理

注意: rqalpha 用 exec 加载本文件，__file__/sys.path 不可靠，plan 路径用环境变量 QBT_PLAN_FILE 传入
"""
import os
import pandas as pd
from rqalpha.apis import *


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
    context.logged = set()


def handle_bar(context, bar_dict):
    today = context.now.strftime("%Y-%m-%d")
    if today not in context.plan:
        return
    target = set(context.plan[today])

    # 卖出不在目标列表的持仓
    for order_book_id in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[order_book_id]
        if order_book_id not in target and pos.quantity > 0:
            order_target_percent(order_book_id, 0)

    # 买入目标中未持仓的（等权）
    weight = 0.99 / len(target)
    for s in target:
        if get_position(s).quantity == 0:
            order_target_percent(s, weight)

    if today not in context.logged:
        context.logged.add(today)
        print(f"[调仓] {today} 目标 {len(target)} 只")
