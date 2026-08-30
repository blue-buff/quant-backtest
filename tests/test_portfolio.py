"""P8 T2: portfolio.pkl 契约检查（schema/非负/行和≤1/覆盖度/常数权重）。"""
import numpy as np
import pandas as pd

from pipeline import executor as ex


def _mk_test_pq(tmp_path, dates=None, n_inst=25):
    dates = dates or pd.to_datetime(pd.bdate_range("2025-01-02", periods=30))
    insts = ["S%02d" % i for i in range(n_inst)]
    idx = pd.MultiIndex.from_product([dates, insts],
                                     names=["datetime", "instrument"])
    pq = tmp_path / "test.pq"
    pd.DataFrame({"y": np.zeros(len(idx))}, index=idx).to_parquet(pq)
    return pq, dates, insts


def _mk_pf(tmp_path, dates, insts, weight_fn):
    rows = []
    for d in dates:
        for i, s in enumerate(insts):
            w = weight_fn(d, s, i)
            if w is not None:
                rows.append((d, s, w))
    pf = pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"])
    p = tmp_path / "portfolio.pkl"
    pf.to_pickle(p)
    return p


def test_valid_sparse_weights_pass(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    # 每天前 25 只各 0.015 + 0.005*(日号%3) + 0.0005*(编号%5)（跨日跨股变化），其余省略
    p = _mk_pf(tmp_path, dates, insts,
               lambda d, s, i: (0.015 + 0.005 * (d.day % 3) + 0.0005 * (i % 5))
               if i < 25 else None)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is True
    assert rep["max_daily_sum"] <= 0.7
    assert rep["n_dates"] == 30


def test_negative_weight_rejected(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    p = _mk_pf(tmp_path, dates, insts,
               lambda d, s, i: (0.1 if i == 0 else (0.02 if i < 10 else None)))
    # 改造：第一只改成 -0.1
    pf = pd.read_pickle(p)
    pf.iloc[0, 0] = -0.1
    pf.to_pickle(p)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is False
    assert any("negative" in x for x in rep["issues"])


def test_row_sum_over_one_rejected(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    p = _mk_pf(tmp_path, dates, insts,
               lambda d, s, i: 0.05 if i < 25 else None)  # sum 1.25
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is False
    assert any("exceed 1" in x for x in rep["issues"])


def test_constant_weights_rejected(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    p = _mk_pf(tmp_path, dates, insts,
               lambda d, s, i: 0.04 if i < 10 else None)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is False
    assert any("constant" in x for x in rep["issues"])


def test_rotating_equal_weight_topk_passes(tmp_path):
    """等权 topK（权重值恒定、成员轮换）是合法策略，必须放行。"""
    pq, dates, insts = _mk_test_pq(tmp_path)
    rows = []
    for di, d in enumerate(dates):
        pick = insts[di % 15:(di % 15) + 10]  # 每天轮换 10 只，共覆盖 25 只
        for s in pick:
            rows.append((d, s, 0.05))
    pf = pd.DataFrame(rows, columns=["datetime", "instrument", "weight"]).set_index(
        ["datetime", "instrument"])
    p = tmp_path / "portfolio.pkl"
    pf.to_pickle(p)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is True, rep["issues"]
    assert rep["n_inst"] >= 10


def test_missing_file_readable_error(tmp_path):
    pq, _, _ = _mk_test_pq(tmp_path)
    rep = ex.check_portfolio(str(tmp_path / "nope.pkl"), str(pq))
    assert rep["ok"] is False
    assert any("unreadable" in x for x in rep["issues"])


def test_all_nan_rejected(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    p = _mk_pf(tmp_path, dates, insts, lambda d, s, i: np.nan)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is False
    assert any("all-NaN" in x for x in rep["issues"])


def test_low_coverage_recorded(tmp_path):
    pq, dates, insts = _mk_test_pq(tmp_path)
    short = dates[:3]  # 只有 3 天
    p = _mk_pf(tmp_path, short, insts, lambda d, s, i: 0.05 if i < 10 else None)
    rep = ex.check_portfolio(str(p), str(pq))
    assert rep["ok"] is False
    assert rep["date_frac"] < 0.5
    assert any("coverage" in x for x in rep["issues"])
