"""P7 T3: 契约拒绝常数预测（分数 std=0 / 唯一值<2）。"""
import numpy as np
import pandas as pd

from pipeline import executor as ex


def _mk_pair(tmp_path, pred_vals, seed=None):
    dates = pd.to_datetime(pd.bdate_range("2025-01-02", periods=30))
    insts = ["S%02d" % i for i in range(25)]
    idx = pd.MultiIndex.from_product([dates, insts],
                                     names=["datetime", "instrument"])
    pd.DataFrame({"score": pred_vals}, index=idx).to_pickle(tmp_path / "pred.pkl")
    rng = np.random.default_rng(seed)
    pd.DataFrame({"y": rng.normal(0, 0.02, size=len(idx))}, index=idx).to_parquet(
        tmp_path / "test.pq")
    return tmp_path / "pred.pkl", tmp_path / "test.pq"


def test_constant_pred_rejected(tmp_path):
    pred, test = _mk_pair(tmp_path, [0.5] * 750)
    rep = ex.check_pred(str(pred), str(test))
    assert rep["ok"] is False
    assert any("constant" in i for i in rep["issues"])


def test_two_unique_values_accepted(tmp_path):
    pred, test = _mk_pair(tmp_path, [0.1, 0.2] * 375)
    rep = ex.check_pred(str(pred), str(test))
    assert rep["ok"] is True  # 2 个唯一值不构成常数（<2 才拒绝）


def test_varying_pred_passes(tmp_path):
    rng = np.random.default_rng(1)
    pred, test = _mk_pair(tmp_path, rng.uniform(size=750))
    rep = ex.check_pred(str(pred), str(test))
    assert rep["ok"] is True
