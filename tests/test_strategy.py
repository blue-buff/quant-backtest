"""rqalpha 策略逻辑测试（exec 真实策略文件 + stub rqalpha）

覆盖: P2-1 topping-up / P2-4 未成交重试 / A3 滑点与参与率 / P1-3 卖出语义
"""
import datetime
import sys
import types
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
STRATEGY = PROJECT / "qlib_examples" / "rq_strategy_qlib.py"


class FakeBar:
    def __init__(self, close=10.0, volume=1_000_000):
        self.close = close
        self.volume = volume


class FakePosition:
    def __init__(self, quantity=0):
        self.quantity = quantity


class FakeNow:
    def __init__(self, s):
        self._dt = datetime.datetime.strptime(s, "%Y-%m-%d")

    def strftime(self, fmt):
        return self._dt.strftime(fmt)


class FakeContext:
    def __init__(self, plan, now="2025-01-02", total_value=1_000_000, positions=None):
        self.plan = plan
        self.now = FakeNow(now)
        self.portfolio = types.SimpleNamespace(total_value=total_value, positions=positions or {})
        self.pending_buys = {}
        self.slippage = 0.0
        self.participation = 0.05
        self.retry_days = 2
        self.logged = set()


@pytest.fixture
def strategy():
    fake_rqalpha = types.ModuleType("rqalpha")
    fake_apis = types.ModuleType("rqalpha.apis")
    state = {"orders": [], "positions": {}, "bars": {}}

    def get_position(symbol):
        return state["positions"].setdefault(symbol, FakePosition())

    def current_bar(symbol):
        return state["bars"].get(symbol, FakeBar())

    def order(symbol, quantity, style=None):
        state["orders"].append({"symbol": symbol, "quantity": quantity, "style": style})

    fake_apis.get_position = get_position
    fake_apis.current_bar = current_bar
    fake_apis.order = order
    fake_apis.order_target_percent = lambda *a, **k: None
    fake_apis.subscribe = lambda *a, **k: None
    fake_apis.is_st_stock = lambda s: state.get('is_st_stock', lambda x: False)(s)

    class LimitOrder:
        def __init__(self, price):
            self.price = price

    fake_apis.LimitOrder = LimitOrder
    sys.modules["rqalpha"] = fake_rqalpha
    sys.modules["rqalpha.apis"] = fake_apis
    ns = {}
    exec(compile(STRATEGY.read_text(encoding="utf-8"), "rq_strategy_qlib.py", "exec"), ns)
    yield ns, state
    sys.modules.pop("rqalpha", None)
    sys.modules.pop("rqalpha.apis", None)


def _orders_by_symbol(state):
    return {o["symbol"]: o for o in state["orders"]}


def _bars(state):
    """把 state 中的 bars 转成 bar_dict；未配置的标的用默认 FakeBar"""
    d = dict(state["bars"])
    for sym in ("600000.XSHG", "600036.XSHG", "600519.XSHG", "600009.XSHG"):
        d.setdefault(sym, FakeBar())
    return d


# ---------- 纯函数 ----------

def test_target_quantity_100_lot(strategy):
    ns, _ = strategy
    assert ns["target_quantity"](1_000_000, 0.99 / 2, 10.0) == 49500  # 100 的整数倍
    assert ns["target_quantity"](1_000_000, 0.5, 0) == 0
    assert ns["target_quantity"](None, 0.5, 10) == 0


def test_participation_capped(strategy):
    ns, _ = strategy
    assert ns["participation_capped"](200_000, 1_000_000, 0.05) == 50000
    assert ns["participation_capped"](20_000, 1_000_000, 0.05) == 20000  # 不超限
    assert ns["participation_capped"](20_000, None, 0.05) == 20000  # 无成交量不限制


def test_slippage_price(strategy):
    ns, _ = strategy
    assert ns["slippage_price"](10.0, 0.001, "buy") == pytest.approx(10.01)
    assert ns["slippage_price"](10.0, 0.001, "sell") == pytest.approx(9.99)
    assert ns["slippage_price"](10.0, 0.0, "buy") == 10.0


# ---------- 策略行为 ----------

def test_handle_bar_buy_sell_and_topping_up(strategy):
    """调仓日：新买 + 卖出跌出目标 + 留存持仓 topping-up（P2-1）"""
    ns, state = strategy
    ctx = FakeContext(plan={"2025-01-02": ["600000.XSHG", "600036.XSHG"]})
    ctx.portfolio.positions = {
        "600036.XSHG": FakePosition(quantity=1000),   # 留存 → topping-up
        "600519.XSHG": FakePosition(quantity=500),    # 跌出目标 → 卖出
    }
    state["positions"] = ctx.portfolio.positions
    ns["handle_bar"](ctx, _bars(state))

    buys = _orders_by_symbol(state)
    assert buys["600000.XSHG"]["quantity"] == 49500  # 新买
    assert buys["600036.XSHG"]["quantity"] == 48500  # 1000 → 目标 49500 的补差
    assert buys["600519.XSHG"]["quantity"] == -500   # 卖出（P1-3 语义：不在目标列表）
    # 新买的 600000 进入重试队列，留存 600036 不进
    assert "600000.XSHG" in ctx.pending_buys
    assert "600036.XSHG" not in ctx.pending_buys


def test_participation_cap_applied(strategy):
    """A3: 成交量很小 → 订单量被参与率上限截断（volume 10000 × 5% = 500 股）"""
    ns, state = strategy
    state["bars"]["600000.XSHG"] = FakeBar(close=10.0, volume=10_000)
    ctx = FakeContext(plan={"2025-01-02": ["600000.XSHG"]})
    ns["handle_bar"](ctx, _bars(state))
    assert _orders_by_symbol(state)["600000.XSHG"]["quantity"] == 500


def test_pending_retry_until_filled(strategy):
    """P2-4: 买入未成交 → 非调仓日重试；成交后停止"""
    ns, state = strategy
    ctx = FakeContext(plan={"2025-01-02": ["600000.XSHG"]})
    ns["handle_bar"](ctx, _bars(state))
    assert "600000.XSHG" in ctx.pending_buys  # 未成交（positions 未更新）

    # 非调仓日 1：重试，天数递减
    ctx.now = FakeNow("2025-01-03")
    ns["handle_bar"](ctx, _bars(state))
    assert ctx.pending_buys["600000.XSHG"][1] == 1

    # 模拟成交
    state["positions"]["600000.XSHG"].quantity = 49500
    # 非调仓日 2：已成交 → 清除 pending，不再下单
    n_before = len(state["orders"])
    ctx.now = FakeNow("2025-01-06")
    ns["handle_bar"](ctx, _bars(state))
    assert "600000.XSHG" not in ctx.pending_buys
    assert len(state["orders"]) == n_before


def test_pending_retry_gives_up_after_days(strategy):
    ns, state = strategy
    ctx = FakeContext(plan={"2025-01-02": ["600000.XSHG"]})
    ctx.retry_days = 2
    ns["handle_bar"](ctx, _bars(state))
    # 两次重试机会（days_left 2→1→0），之后放弃
    for d in ["2025-01-03", "2025-01-06"]:
        ctx.now = FakeNow(d)
        ns["handle_bar"](ctx, _bars(state))
    assert ctx.pending_buys["600000.XSHG"][1] == 0
    n_before = len(state["orders"])
    ctx.now = FakeNow("2025-01-07")
    ns["handle_bar"](ctx, _bars(state))
    assert "600000.XSHG" not in ctx.pending_buys
    assert len(state["orders"]) == n_before


def test_no_trade_outside_rebalance_day(strategy):
    ns, state = strategy
    ctx = FakeContext(plan={"2025-01-02": ["600000.XSHG"]}, now="2025-01-03")
    ns["handle_bar"](ctx, _bars(state))
    assert state["orders"] == []

# P0-1（对比审查）: ST 标的从目标列表排除，不买入
def test_st_stock_excluded(strategy):
    ns, state = strategy
    state["is_st_stock"] = lambda s: s == "600519.XSHG"
    ctx = FakeContext(plan={"2025-01-02": ["600519.XSHG", "600000.XSHG"]})
    ns['handle_bar'](ctx, _bars(state))
    orders = _orders_by_symbol(state)
    assert "600519.XSHG" not in orders
    assert "600000.XSHG" in orders
