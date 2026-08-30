"""指标清单扩展（2026-08-27）：信号/组合/绩效/风险/稳健 新增指标数值正确性。"""
import math

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
    pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"]).to_pickle(tmp_path / "portfolio.pkl")
    return tmp_path / "portfolio.pkl"


def test_backtest_extended_return_metrics(tmp_path):
    # 单票满仓，价格路径给出手算日收益 +2%/-1%/+3%/-2%/+1%（首日含建仓成本 0.00035）
    dates = pd.to_datetime(pd.bdate_range("2025-01-06", periods=6))
    close = {"X": {dates[0]: 100.0, dates[1]: 102.0, dates[2]: 100.98,
                   dates[3]: 104.0094, dates[4]: 101.929212,
                   dates[5]: 101.929212 * 1.01}}
    px = _mk_prices(tmp_path, close, dates, ["X"])
    pf = _mk_pf(tmp_path, dates, {"X": 1.0})
    bt = mm.compute_backtest(str(pf), str(px), benchmark=None)

    net = np.array([0.02 - 0.00035, -0.01, 0.03, -0.02, 0.01])
    assert bt["n_days"] == 5
    # 总收益 = 连乘 - 1
    assert abs(bt["total_return"] - ((1 + net).prod() - 1)) < 1e-9
    # 胜负结构
    assert abs(bt["win_rate"] - 3 / 5) < 1e-12
    assert abs(bt["avg_win"] - (0.01965 + 0.03 + 0.01) / 3) < 1e-9
    assert abs(bt["avg_loss"] - (-0.015)) < 1e-9
    assert abs(bt["profit_factor"] - (0.05965 / 0.03)) < 1e-9
    assert abs(bt["worst_day"] - (-0.02)) < 1e-12
    # 尾部
    q = np.sort(net)
    var95 = np.quantile(q, 0.05)
    assert abs(bt["var_95"] - var95) < 1e-9
    assert abs(bt["cvar_95"] - q[q <= var95].mean()) < 1e-9
    assert math.isfinite(bt["skewness"]) and math.isfinite(bt["kurtosis"])
    assert bt["tail_ratio"] > 0
    # 换手/暴露
    assert abs(bt["turnover_mean"] - 0.2) < 1e-9  # 首日 1.0，之后 4 天 0
    assert bt["max_exposure"] == 1.0 and bt["min_exposure"] == 1.0
    # 回撤持续：净值两次跌破前高（t1 一天、t3-t4 两天）→ 最长 2 天
    assert bt["mdd_duration_days"] == 2


def test_backtest_identity_relations(tmp_path):
    """alpha/excess/cost_drag/IR/TE/PSR 的代数恒等关系 + 取值范围。"""
    dates = pd.to_datetime(pd.bdate_range("2025-01-06", periods=12))
    close = {
        "A": {d: float(100 * (1.03 ** i)) for i, d in enumerate(dates)},
        "B": {d: float(100 * (0.98 ** i)) for i, d in enumerate(dates)},
        "SH000905": {d: float(100 * (1.005 ** i)) for i, d in enumerate(dates)},
    }
    px = _mk_prices(tmp_path, close, dates, ["A", "B", "SH000905"])
    pf = _mk_pf(tmp_path, dates, {"A": 0.6, "B": 0.3})  # 0.1 现金
    bt = mm.compute_backtest(str(pf), str(px), benchmark="sh000905")

    # CAPM alpha = ann_ret - beta * bmk_ann_ret
    assert abs(bt["alpha_ann"] - (bt["ann_ret"] - bt["beta"] * bt["bmk_ann_ret"])) < 1e-9
    # excess = ann_ret - bmk_ann_ret
    assert abs(bt["excess_ann"] - (bt["ann_ret"] - bt["bmk_ann_ret"])) < 1e-9
    # cost_drag = gross - net
    assert abs(bt["cost_drag_ann"] - (bt["gross_ann_ret"] - bt["ann_ret"])) < 1e-9
    # IR/TE 口径
    assert bt["tracking_error"] >= 0
    assert math.isfinite(bt["information_ratio"])
    # PSR 是概率，落在 [0,1]
    assert bt["psr"] is None or 0.0 <= bt["psr"] <= 1.0
    # 回撤持续/滚动回撤/季度 结构存在
    assert bt["mdd_duration_days"] >= 0
    assert bt["rolling_mdd_60d"] <= 0
    assert isinstance(bt["quarterly_returns"], list)
    # 执行延迟对照：3 档 lag
    assert [e["lag"] for e in bt["execution_lag"]] == [0, 1, 2]
    assert bt["execution_lag"][1]["n_days"] == bt["n_days"]


def test_portfolio_extended_metrics(tmp_path):
    dates = pd.to_datetime(pd.bdate_range("2025-01-02", periods=10))
    insts = ["S%02d" % i for i in range(20)]
    idx = pd.MultiIndex.from_product([dates, insts], names=["datetime", "instrument"])
    score = pd.DataFrame({"score": [-(i % 20) for i in range(len(idx))]}, index=idx)
    score.to_pickle(tmp_path / "pred.pkl")
    rows = [(d, s, 0.1) for d in dates for s in insts[:5]]
    pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"]).to_pickle(tmp_path / "pf.pkl")
    pf = mm.compute_portfolio(str(tmp_path / "pred.pkl"), str(tmp_path / "pf.pkl"))

    assert pf["n_inst"] == 20
    assert abs(pf["oneway_buy_mean"] - 0.05) < 1e-12   # 首日 0.5，之后 0
    assert abs(pf["oneway_sell_mean"] - 0.0) < 1e-12
    assert abs(pf["top1_frac_mean"] - 0.2) < 1e-12     # 0.1 / 0.5
    assert abs(pf["top5_frac_mean"] - 1.0) < 1e-12
    assert abs(pf["top10_frac_mean"] - 1.0) < 1e-12
    assert abs(pf["active_share_mean"] - 0.5) < 1e-12  # 见手算
    assert abs(pf["rebalance_day_frac"] - 0.0) < 1e-12 # 建仓后不再调仓


def test_portfolio_oneway_sell(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    insts = ["A", "B"]
    idx = pd.MultiIndex.from_product([dates, insts], names=["datetime", "instrument"])
    pd.DataFrame({"score": [1.0, 0.0, 1.0, 0.0]}, index=idx).to_pickle(tmp_path / "pred.pkl")
    pfdf = pd.DataFrame([
        (dates[0], "A", 0.8), (dates[0], "B", 0.2),
        (dates[1], "A", 0.2), (dates[1], "B", 0.8),
    ], columns=["datetime", "instrument", "weight"]).set_index(["datetime", "instrument"])
    pfdf.to_pickle(tmp_path / "pf.pkl")
    pf = mm.compute_portfolio(str(tmp_path / "pred.pkl"), str(tmp_path / "pf.pkl"))
    assert abs(pf["oneway_buy_mean"] - 0.8) < 1e-12   # (1.0 + 0.6)/2
    assert abs(pf["oneway_sell_mean"] - 0.3) < 1e-12  # (0.0 + 0.6)/2


def test_nonoverlap_offsets_hand_calc():
    daily = pd.DataFrame({"rank_ic": [0.1, 0.2, 0.3, 0.4, 0.5]})
    offs = mm.nonoverlap_offsets(daily, 2)
    assert abs(offs[0]["mean_rank_ic"] - 0.3) < 1e-12  # 0.1,0.3,0.5
    assert abs(offs[1]["mean_rank_ic"] - 0.3) < 1e-12  # 0.2,0.4
    assert abs(mm.nonoverlap_min_rankic(daily, 2) - 0.3) < 1e-12


def test_compute_full_coverage_offsets(tmp_path):
    """全有限输入：coverage=1，nonoverlap_offsets 数量=h，min<=mean。"""
    dates = pd.to_datetime(pd.bdate_range("2025-01-02", periods=40))
    insts = ["S%02d" % i for i in range(25)]
    idx = pd.MultiIndex.from_product([dates, insts], names=["datetime", "instrument"])
    rng = np.random.default_rng(0)
    score = pd.DataFrame({"score": rng.normal(size=len(idx))}, index=idx)
    label = pd.DataFrame({"y": score["score"] + 0.1 * rng.normal(size=len(idx))}, index=idx)
    score.to_pickle(tmp_path / "pred.pkl")
    label.to_pickle(tmp_path / "label.pkl")
    full = mm.compute_full(str(tmp_path / "pred.pkl"), str(tmp_path / "label.pkl"), h=2)
    assert abs(full["coverage"] - 1.0) < 1e-12
    assert len(full["nonoverlap_offsets"]) == 2
    assert full["nonoverlap_min_rank_ic"] <= full["nonoverlap_mean_rank_ic"]
