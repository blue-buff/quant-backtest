"""P8 T3: portfolio/backtest/attribution 数值正确性（合成数据，手算对照）。"""
import json

import numpy as np
import pandas as pd

from pipeline import metrics as mm


def _mk_prices(tmp_path, close_map, dates, insts):
    rows = []
    for d in dates:
        for s in insts:
            rows.append((d, s, close_map[s][d]))
    px = pd.DataFrame(rows, columns=["datetime", "instrument", "close"]).set_index(
        ["datetime", "instrument"])
    p = tmp_path / "prices.pq"
    px.to_parquet(p)
    return p


def _mk_pf(tmp_path, dates, weights_map):
    rows = []
    for d in dates:
        for s, w in weights_map.items():
            rows.append((d, s, w))
    pf = pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"])
    p = tmp_path / "portfolio.pkl"
    pf.to_pickle(p)
    return p


def test_backtest_hand_calculated(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    close = {
        "A": {dates[0]: 10.0, dates[1]: 11.0, dates[2]: 12.1},   # +10% 每天
        "B": {dates[0]: 20.0, dates[1]: 20.0, dates[2]: 20.0},   # 0
        "C": {dates[0]: 100.0, dates[1]: 99.0, dates[2]: 98.01}, # -1% 每天
    }
    px = _mk_prices(tmp_path, close, dates, ["A", "B", "C"])
    w = {"A": 0.5, "B": 0.3, "C": 0.1}  # cash 0.1，此后不变
    pf = _mk_pf(tmp_path, dates, w)
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)
    # 手算：gross_t = 0.5*0.1 + 0.3*0 + 0.1*(-0.01) = 0.049
    # turnover0 = 0.9, turnover1 = 0；sell = 0（无减仓）
    # cost0 = (0.00025+0.0001)*0.9 = 0.000315；cost1 = 0
    assert bt["n_days"] == 2  # 最后一天无次日收益被丢弃
    assert abs(bt["total_cost"] - 0.000315) < 1e-12
    assert abs(bt["worst_day"] - (0.049 - 0.000315)) < 1e-12
    assert abs(bt["cost_components"]["commission"] - 0.000225) < 1e-12
    assert abs(bt["cost_components"]["stamp"] - 0.0) < 1e-12
    assert abs(bt["cost_components"]["slippage"] - 0.00009) < 1e-12
    assert bt["benchmark"] == "equal_weight_universe"
    # 2 个观测点下 beta/excess 数值不稳定（年化幂次放大 + 方差趋零），不在此断言
    assert bt["costs_used"] == {"commission": 0.00025, "stamp": 0.001, "slippage": 0.0001}


def test_backtest_stamp_on_sells(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    close = {"A": {d: 10.0 for d in dates}, "B": {d: 10.0 for d in dates}}
    px = _mk_prices(tmp_path, close, dates, ["A", "B"])
    # 第一天 A=0.8 B=0.2，第二天 A=0.2 B=0.8：卖出 A 0.6
    pf = tmp_path / "portfolio.pkl"
    pfdf = pd.DataFrame([
        (dates[0], "A", 0.8), (dates[0], "B", 0.2),
        (dates[1], "A", 0.2), (dates[1], "B", 0.8),
    ], columns=["datetime", "instrument", "weight"]).set_index(["datetime", "instrument"])
    pfdf.to_pickle(pf)
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)
    # turnover1 = |0.2-0.8| + |0.8-0.2| = 1.2；sell1 = max(0.8-0.2,0) = 0.6
    # stamp 只收卖出：0.001 * 0.6 = 0.0006
    assert abs(bt["cost_components"]["stamp"] - 0.0006) < 1e-12


def test_backtest_named_benchmark(tmp_path):
    dates = pd.to_datetime(pd.bdate_range("2025-01-02", periods=6))
    close = {
        "X": {d: float(100 + 2 * i) for i, d in enumerate(dates)},
        "Y": {d: 100.0 for d in dates},
        "SH000905": {d: float(100 + (i % 3) * 3) for i, d in enumerate(dates)},
    }
    px = _mk_prices(tmp_path, close, dates, ["X", "Y", "SH000905"])
    pf = _mk_pf(tmp_path, dates, {"X": 1.0})
    bt = mm.compute_backtest(str(pf), str(px), benchmark="sh000905")
    assert bt["benchmark"] == "SH000905"
    assert bt["beta"] is not None


def test_backtest_open_execution_lag(tmp_path):
    """The expanded price cache exposes T+1 open execution as a fourth lag."""
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    idx = pd.MultiIndex.from_product([dates, ["A"]], names=["datetime", "instrument"])
    # Match the expanded cache schema: adjusted open is derived from raw+factor.
    px = pd.DataFrame({"close": [10.0, 11.0, 12.1],
                       "open_raw": [10.0, 10.5, 12.0],
                       "factor": [1.0, 1.0, 1.0]}, index=idx)
    p = tmp_path / "prices.pq"
    px.to_parquet(p)
    pf = _mk_pf(tmp_path, dates, {"A": 1.0})
    bt = mm.compute_backtest(str(pf), str(p))
    lags = [row["lag"] for row in bt["execution_lag"]]
    assert lags == [0, 1, 2, "open"]
    open_row = bt["execution_lag"][-1]
    assert open_row["n_days"] == 2  # last close has no next open
    assert np.isfinite(open_row["net_ann_ret"])


def test_portfolio_family(tmp_path):
    dates = pd.to_datetime(pd.bdate_range("2025-01-02", periods=10))
    insts = ["S%02d" % i for i in range(20)]
    idx = pd.MultiIndex.from_product([dates, insts],
                                     names=["datetime", "instrument"])
    # 分数 = 仪器编号：权重 = 前 5 只各 0.1
    score = pd.DataFrame({"score": [-(i % 20) for i in range(len(idx))]}, index=idx)
    score.to_pickle(tmp_path / "pred.pkl")
    rows = [(d, s, 0.1) for d in dates for s in insts[:5]]
    pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"]).to_pickle(tmp_path / "pf.pkl")
    pf = mm.compute_portfolio(str(tmp_path / "pred.pkl"), str(tmp_path / "pf.pkl"))
    assert pf["n_days"] == 10
    assert abs(pf["cash_frac_mean"] - 0.5) < 1e-12
    assert abs(pf["turnover_mean"] - 0.05) < 1e-12  # 第一天建仓 0.5，之后 9 天 0
    assert pf["n_held_mean"] == 5
    assert abs(pf["hhi_mean"] - 0.2) < 1e-12  # 5 只等权：5*(0.1)^2/0.5^2 = 0.2
    # 权重与分数正相关（高分数拿到权重）→ weight_ic 应为正
    assert pf["weight_ic_mean"] > 0


def test_backtest_restricted_to_portfolio_window(tmp_path):
    """回归：价格史从 2021 开始、组合 2025 才建仓时，回测只算组合声明窗口。"""
    dates_full = pd.to_datetime(pd.bdate_range("2021-06-01", "2021-06-10"))
    dates_pf = dates_full[5:]
    close = {"A": {d: float(10 + i) for i, d in enumerate(dates_full)},
             "B": {d: float(20 + i) for i, d in enumerate(dates_full)}}
    px = _mk_prices(tmp_path, close, dates_full, ["A", "B"])
    pf = _mk_pf(tmp_path, dates_pf, {"A": 0.8})
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)
    assert bt["n_days"] == len(dates_pf) - 1  # 最后一天无次日收益被丢弃
    assert bt["sample_window"][0] == str(dates_pf[0])[:10]


def test_backtest_case_insensitive_instruments(tmp_path):
    """回归：价格缓存的 instrument 大写、portfolio 大写；混合大小写也应对齐。"""
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    close = {"SH600000": {d: float(10 + i) for i, d in enumerate(dates)},
             "SH000905": {d: float(100 + i) for i, d in enumerate(dates)}}
    px = _mk_prices(tmp_path, close, dates, list(close.keys()))
    pf = _mk_pf(tmp_path, dates, {"SH600000": 1.0})
    bt = mm.compute_backtest(str(pf), str(px), benchmark="sh000905")
    assert bt["benchmark"] == "SH000905"
    assert bt["n_days"] == 2
    assert bt["total_cost"] > 0  # 建仓换手有佣金/滑点


def test_attribution_verdicts(tmp_path):
    full = {"nonoverlap_mean_rank_ic": 0.05,
            "bootstrap_rankic": {"p_le0": 0.01}}
    pf = {"weight_ic_mean": 0.045, "turnover_mean": 0.3}
    bt = {"cost_drag_ann": 0.02, "excess_ann": 0.1, "beta": 0.9}
    a = mm.compute_attribution(full, pf, bt)
    assert a["pred"]["verdict"] == "strong"
    assert a["align"]["verdict"] == "good"     # 0.045/0.05 = 0.9
    assert a["cost"]["verdict"] == "low"
    assert a["market"]["verdict"] == "helped"
    # 做坏一层：高换手 → cost 层背锅，不是 pred 层
    bt2 = {"cost_drag_ann": 0.3, "excess_ann": -0.05, "beta": 1.1}
    a2 = mm.compute_attribution(full, pf, bt2)
    assert a2["pred"]["verdict"] == "strong"   # 信号没问题
    assert a2["cost"]["verdict"] == "high"
    assert a2["market"]["verdict"] == "hurt"


def test_core_metrics_tolerates_selective_families():
    """回归：spec.metrics 勾选制下 hit_rate/deciles 缺席，core_metrics 不得崩。"""
    full = {"task": "regression", "n_days": 100, "n_inst": 300,
            "sample_window": ["a", "b"],
            "nonoverlap_mean_rank_ic": 0.04, "rank_ic_std": 0.1,
            "nonoverlap_rank_icir": 0.4, "n_nonoverlap": 10,
            "bootstrap_rankic": {"p_le0": 0.01},
            "portfolio": {"turnover_mean": 0.2},
            "backtest": {"sharpe": 1.0}}
    core = mm.core_metrics(full, "t", "r", "v3",
                           {"rankic_mean_min": 0.01, "turnover_max": 0.5})
    assert core["hit"]["rate"] is None
    assert core["rankic"]["mean"] == 0.04
    assert core["conclusion"]["expectation_check"] == "met"


def test_filter_pred_selection():
    full = {"task": "regression", "n_days": 10, "n_inst": 100,
            "sample_window": ["a", "b"], "nonoverlap_mean_rank_ic": 0.05,
            "hit_rate": 0.5, "deciles": {"top_minus_bottom": 0.1},
            "bootstrap_rankic": {"p_le0": 0.02}}
    out = mm.filter_pred(full, ["rankic", "portfolio"])
    assert "nonoverlap_mean_rank_ic" in out
    assert "hit_rate" not in out
    assert "deciles" not in out
    # 回归：dispatcher 追加的 trade 族不能被 filter 丢掉
    full2 = dict(full)
    full2["portfolio"] = {"turnover_mean": 0.2}
    full2["backtest"] = {"sharpe": 1.0}
    full2["attribution"] = {"pred": {}}
    out2 = mm.filter_pred(full2, ["rankic", "portfolio", "backtest", "attribution"])
    assert out2["portfolio"]["turnover_mean"] == 0.2
    assert out2["backtest"]["sharpe"] == 1.0
    assert "attribution" in out2
    # 未勾选的 trade 族不保留
    out3 = mm.filter_pred(full2, ["rankic", "portfolio"])
    assert "backtest" not in out3 and "attribution" not in out3
    # attribution 自动保留 bootstrap（attribution 需要 p）
    out2 = mm.filter_pred(full, ["rankic", "attribution"])
    assert "bootstrap_rankic" in out2
    # families=None = 现状全量
    out3 = mm.filter_pred(full, None)
    assert out3 is full


def test_backtest_wealth_negative_marker(tmp_path):
    """回归（次要项）：净值跌到 <=0 时 ann_ret 为 NaN，但要显式标记而不是静默。"""
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    close = {"A": {dates[0]: 10.0, dates[1]: 0.001, dates[2]: 0.001}}  # -99.99%
    px = _mk_prices(tmp_path, close, dates, ["A"])
    pf = _mk_pf(tmp_path, dates, {"A": 1.0})
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)
    # 满仓单票 -99.99%，扣成本后 net < -1 → 净值转负
    assert bt["wealth_negative"] is True
    assert np.isnan(bt["ann_ret"])


def test_backtest_wealth_negative_false_normal(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-04"])
    close = {"A": {dates[0]: 10.0, dates[1]: 11.0, dates[2]: 12.1}}
    px = _mk_prices(tmp_path, close, dates, ["A"])
    pf = _mk_pf(tmp_path, dates, {"A": 1.0})
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)
    assert bt["wealth_negative"] is False
