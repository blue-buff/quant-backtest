"""P7 T3: data/extra 声明三态（存在 / 缺失 / 类型错）。"""
import json

import numpy as np
import pandas as pd

from pipeline import executor as ex


def test_declared_present(tmp_path):
    (tmp_path / "run_info.json").write_text(
        json.dumps({"extra_features": ["f1", "f2"], "other": 1}))
    assert ex.declared_extra_features(tmp_path) == ["f1", "f2"]


def test_declared_missing_file(tmp_path):
    assert ex.declared_extra_features(tmp_path) == []


def test_declared_wrong_type(tmp_path, capsys):
    (tmp_path / "run_info.json").write_text(json.dumps({"extra_features": "f1"}))
    assert ex.declared_extra_features(tmp_path) == []
    assert "extra_features not a list" in capsys.readouterr().err


def test_declared_bad_json(tmp_path):
    (tmp_path / "run_info.json").write_text("{not json")
    assert ex.declared_extra_features(tmp_path) == []


def test_check_pred_records_declared_extras(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    idx = pd.MultiIndex.from_product([dates, ["A", "B"]],
                                     names=["datetime", "instrument"])
    pd.DataFrame({"score": [0.1, 0.2, 0.3, 0.4]}, index=idx).to_pickle(
        tmp_path / "pred.pkl")
    pd.DataFrame({"y": [0.01, 0.02, 0.03, 0.04]}, index=idx).to_parquet(
        tmp_path / "test.pq")
    (tmp_path / "run_info.json").write_text(json.dumps({"extra_features": ["f9"]}))
    rep = ex.check_pred(str(tmp_path / "pred.pkl"), str(tmp_path / "test.pq"),
                        run_dir=str(tmp_path))
    assert rep["extra_features"] == ["f9"]


def test_check_pred_missing_run_info_warns(tmp_path):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    idx = pd.MultiIndex.from_product([dates, ["A", "B"]],
                                     names=["datetime", "instrument"])
    pd.DataFrame({"score": [0.1, 0.2, 0.3, 0.4]}, index=idx).to_pickle(
        tmp_path / "pred.pkl")
    pd.DataFrame({"y": [0.01, 0.02, 0.03, 0.04]}, index=idx).to_parquet(
        tmp_path / "test.pq")
    rep = ex.check_pred(str(tmp_path / "pred.pkl"), str(tmp_path / "test.pq"),
                        run_dir=str(tmp_path))
    assert rep["extra_features"] == []
    assert any("run_info.json missing" in w for w in rep["warns"])
