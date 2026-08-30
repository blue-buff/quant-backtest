"""Systems executor cleanroom primitives and executor-contract smoke test."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

EXECUTOR_DIR = Path(__file__).resolve().parents[1] / "executors" / "systems_topk"
sys.path.insert(0, str(EXECUTOR_DIR))
sys.path.insert(0, str(EXECUTOR_DIR / "vendor"))
spec = importlib.util.spec_from_file_location("systems_topk_main",
                                              EXECUTOR_DIR / "main.py")
systems_topk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(systems_topk)


def test_forecast_scale_and_cap():
    scores = pd.Series([-5.0, 5.0, 1.0])
    scalar = systems_topk.fit_absolute_scale(scores, target_abs=10.0)
    scaled = systems_topk.scale_and_cap(scores, scalar, cap=8.0)
    assert scalar == pytest.approx(10.0 / np.mean([5, 5, 1]))
    assert scaled.iloc[0] == pytest.approx(-8.0)
    assert scaled.iloc[1] == pytest.approx(8.0)


def test_vol_inverse_weights_is_budgeted_and_capped():
    signals = pd.DataFrame({"A": [100.0], "B": [1.0]})
    vol = pd.DataFrame({"A": [1.0], "B": [1.0]})
    selected = pd.DataFrame({"A": [True], "B": [True]})
    w = systems_topk.vol_inverse_weights(signals, vol, selected,
                                         invest_frac=0.95, max_weight=0.5)
    assert w.sum(axis=1).iloc[0] == pytest.approx(0.95)
    assert w.iloc[0]["A"] == pytest.approx(0.5)
    assert w.iloc[0]["B"] == pytest.approx(0.45)


def test_complete_vector_liquidates_out_of_universe():
    picks = pd.Series({"B": 0.4})
    held = pd.Series({"A": 0.5})
    universe = pd.Index(["B", "C"])
    target = systems_topk.complete_target_vector(picks, held, universe)
    assert list(target.index) == ["A", "B", "C"]
    assert target["A"] == 0.0
    assert target["B"] == pytest.approx(0.4)
    assert target["C"] == 0.0


def test_position_band_avoids_noise_trade():
    previous = pd.Series({"A": 0.100})
    target = pd.Series({"A": 0.104})
    assert systems_topk.apply_position_band(previous, target, 0.10).iloc[0] == 0.100
    large = pd.Series({"A": 0.200})
    assert systems_topk.apply_position_band(previous, large, 0.10).iloc[0] == 0.200


def test_trade_feasibility_executor_side():
    previous = pd.Series({"A": 0.10, "B": 0.10, "C": 0.10})
    target = pd.Series({"A": 0.30, "B": 0.0, "C": 0.0})
    out = systems_topk.apply_trade_feasibility(
        previous, target,
        suspended=pd.Series({"A": False, "B": True, "C": False}),
        limit_up=pd.Series({"A": True, "B": False, "C": False}),
        limit_down=pd.Series({"A": False, "B": False, "C": False}),
        amount=pd.Series({"A": 1e9, "B": 1e9, "C": 10_000.0}),
        capital=100_000.0, participation_rate=0.05)
    assert out["A"] == pytest.approx(0.10)  # limit-up: no increase
    assert out["B"] == pytest.approx(0.10)  # suspended: no trade
    assert out["C"] == pytest.approx(0.095) # limited one-day sell


def test_rebalance_schedule_weekly_and_monthly():
    days = pd.bdate_range("2025-01-01", "2025-03-31")
    weekly = systems_topk.rebalance_days(days, {"rebalance": "weekly",
                                                "rebalance_weekday": 4})
    monthly = systems_topk.rebalance_days(days, {"rebalance": "monthly"})
    assert all(d.weekday() == 4 for d in weekly)
    assert monthly == set(pd.DatetimeIndex(
        ["2025-01-31", "2025-02-28", "2025-03-31"]))


def test_vendor_is_mit_and_importable():
    license_text = (EXECUTOR_DIR / "vendor/qstrader/LICENSE").read_text()
    assert "MIT License" in license_text
    assert systems_topk.RiskModel is not None


def _contract_smoke_data(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=70)
    insts = [f"S{i:02d}" for i in range(25)]
    idx = pd.MultiIndex.from_product([dates, insts],
                                     names=["datetime", "instrument"])
    rng = np.random.default_rng(42)
    features = pd.DataFrame({"f0": rng.normal(size=len(idx)),
                             "f1": rng.normal(size=len(idx))}, index=idx)
    features["y"] = features["f0"] * 0.05 + rng.normal(scale=0.01, size=len(idx))
    train_pq = tmp_path / "train.pq"
    test_pq = tmp_path / "test.pq"
    features.iloc[:55 * len(insts)].to_parquet(train_pq)
    features.iloc[55 * len(insts):].to_parquet(test_pq)

    px = features["y"].to_frame("close").copy()
    px["open"] = px["close"]
    px["high"] = px["close"]
    px["low"] = px["close"]
    px["close_raw"] = px["close"]
    px["open_raw"] = px["close"]
    px["high_raw"] = px["close"]
    px["low_raw"] = px["close"]
    px["volume"] = 1_000_000.0
    px["amount"] = 10_000_000.0
    px["vwap"] = px["close"]
    px["turn"] = 1.0
    px["factor"] = 1.0
    px["limit_up"] = False
    px["limit_down"] = False
    px["suspended"] = False
    price_pq = tmp_path / "prices.pq"
    px.to_parquet(price_pq)
    return train_pq, test_pq, price_pq


def test_executor_end_to_end_contract(tmp_path):
    from pipeline import executor
    train_pq, test_pq, price_pq = _contract_smoke_data(tmp_path)
    cfg = {"pool": "hs300", "train": ["2025-01-02", "2025-03-06"],
           "valid": ["2025-03-07", "2025-03-20"],
           "test_start": "2025-03-21", "test_end": "2025-04-17",
           "rounds": 10, "early_stopping": 5, "num_threads": 1,
           "seeds": [42], "price_pq": str(price_pq),
           "params": {"top_k": 5, "rebalance": "daily", "buffer": 0.0,
                      "mode": "strategy", "max_weight": 0.25}}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg))
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "main.py", "--config", str(config),
                        "--train", str(train_pq), "--test", str(test_pq),
                        "--out", str(out)], cwd=EXECUTOR_DIR,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    pred_rep = executor.check_pred(out / "pred.pkl", test_pq, out)
    pf_rep = executor.check_portfolio(out / "portfolio.pkl", test_pq)
    assert pred_rep["ok"], pred_rep
    assert pf_rep["ok"], pf_rep
    info = json.loads((out / "run_info.json").read_text())
    assert info["portfolio"]["n_days"] > 0
    assert info["portfolio"]["forecast_scalar"] > 0
