"""P8 T1: 价格缓存（CSV -> parquet、manifest/修订号联动、重建）。"""
import hashlib
import json

import pandas as pd

from pipeline import data as d
import numpy as np


def _mk_csvs(tmp_path, n=3):
    src = tmp_path / "src"
    src.mkdir()
    dates = pd.bdate_range("2021-06-01", "2021-06-10").strftime("%Y-%m-%d")
    for i in range(n):
        rows = "date,open,high,low,close,volume,amount,vwap,turn,factor\n"
        for j, dt in enumerate(dates):
            rows += "%s,%.2f,0,0,%.2f,0,0,0,,1.0\n" % (dt, 10 + i, 10 + i + j)
        (src / ("S%d.csv" % i)).write_text(rows)
    rows = "date,open,high,low,close,volume,amount,vwap,turn,factor\n"
    for j, dt in enumerate(dates):
        rows += "%s,%.2f,0,0,%.2f,0,0,0,,1.0\n" % (dt, 100.0, 100.0 + j)
    (src / "sh000905.csv").write_text(rows)
    return src, dates


def test_price_ensure_builds_and_caches(tmp_path, monkeypatch):
    src, dates = _mk_csvs(tmp_path)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"zz500": src})
    cfg = {"pool": "zz500", "fit_start_time": "2021-06-01", "test_end": "2021-06-10"}
    pq = d.price_ensure(cfg)
    assert pq.exists()
    assert d.cache_valid(pq, required_cols=d.PRICE_COLUMNS)
    px = pd.read_parquet(pq)
    assert px.index.names == ["datetime", "instrument"]
    assert "close" in px.columns
    assert set(d.PRICE_COLUMNS).issubset(px.columns)
    assert set(px.index.get_level_values(1)) == {"S0", "S1", "S2", "SH000905"}
    assert len(px.index.get_level_values(0).unique()) == len(dates)
    mf = d._load_manifest(cache_dir)
    assert mf[pq.name]["pool"] == "zz500"
    # 二次调用直读不重建
    assert d.price_ensure(cfg) == pq


def test_price_key_changes_when_execution_columns_added():
    old = {"kind": "prices", "pool": "hs300", "window": ["2021-06-01", "2026-08-20"]}
    assert d.price_key("hs300", "2021-06-01", "2026-08-20") != (
        hashlib.sha256(json.dumps(
            old, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16])


def test_price_ensure_rebuild(tmp_path, monkeypatch):
    src, _ = _mk_csvs(tmp_path)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"zz500": src})
    cfg = {"pool": "zz500", "fit_start_time": "2021-06-01", "test_end": "2021-06-10"}
    pq = d.price_ensure(cfg)
    pq.write_bytes(b"broken")
    assert not d.cache_valid(pq, required_cols=d.PRICE_COLUMNS)
    pq2 = d.price_ensure(cfg)
    assert d.cache_valid(pq2, required_cols=("close",))
    assert d.data_revision(cache_dir) == 0  # 损坏重建不推进修订号


def test_price_execution_fields_and_limit_flag(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    dates = pd.bdate_range("2021-06-01", "2021-06-03").strftime("%Y-%m-%d")
    # raw prices are 10.0000 -> 11.0000 (a 10% limit-up); source prices are hfq.
    rows = "date,open,high,low,close,volume,amount,vwap,turn,factor\n"
    prices = [10.0, 11.0, 12.1]
    for j, (dt, px) in enumerate(zip(dates, prices)):
            rows += "%s,%.4f,%.4f,%.4f,%.4f,%d,1000,%.4f,1.0,1.5\n" % (
                dt, px * 1.5, px * 1.5, px * 1.5, px * 1.5,
                1000 if j != 2 else 0, px * 1.5)
    (src / "sh600000.csv").write_text(rows)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"hs300": src})
    cfg = {"pool": "hs300", "fit_start_time": "2021-06-01", "test_end": "2021-06-03"}
    pq = d.price_ensure(cfg)
    px = pd.read_parquet(pq).xs("SH600000", level="instrument")
    assert np.allclose(px[["open_raw", "close_raw"]], np.array(prices)[:, None],
                       rtol=0, atol=5e-6)
    assert px.loc[dates[1], "limit_up"] is np.True_
    assert not px.loc[dates[1], "limit_down"]
    assert px.loc[dates[2], "suspended"] is np.True_


def test_invalidate_pool_removes_prices(tmp_path, monkeypatch):
    src, _ = _mk_csvs(tmp_path)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"zz500": src})
    cfg = {"pool": "zz500", "fit_start_time": "2021-06-01", "test_end": "2021-06-10"}
    pq = d.price_ensure(cfg)
    rev0 = d.data_revision(cache_dir)
    res = d.invalidate("zz500", cache_dir=cache_dir)
    assert not pq.exists()
    assert res["revision"] == rev0 + 1
    assert d._load_manifest(cache_dir) == {}


def test_price_instruments_uppercased(tmp_path, monkeypatch):
    """回归：CSV 文件名小写（sh600000），价格缓存的 instrument 必须大写（SH600000）。"""
    src = tmp_path / "src"
    src.mkdir()
    dates = pd.bdate_range("2021-06-01", "2021-06-05").strftime("%Y-%m-%d")
    rows = "date,open,high,low,close,volume,amount,vwap,turn,factor\n"
    for j, dt in enumerate(dates):
        rows += "%s,%.2f,0,0,%.2f,0,0,0,,1.0\n" % (dt, 10.0, 10.0 + j)
    (src / "sh600000.csv").write_text(rows)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"hs300": src})
    cfg = {"pool": "hs300", "fit_start_time": "2021-06-01", "test_end": "2021-06-05"}
    pq = d.price_ensure(cfg)
    px = pd.read_parquet(pq)
    assert set(px.index.get_level_values(1)) == {"SH600000"}


def test_price_ensure_missing_source(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(d, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(d, "PRICE_SOURCE_DIRS", {"zz500": tmp_path / "nope"})
    cfg = {"pool": "zz500", "fit_start_time": "2021-06-01", "test_end": "2021-06-10"}
    try:
        d.price_ensure(cfg)
        raised = False
    except ValueError as e:
        raised = "price source missing" in str(e)
    assert raised
