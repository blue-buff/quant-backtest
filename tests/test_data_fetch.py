"""qbt data fetch 本地缓存复用测试"""
import json

import pytest

from qbt.commands.data import FIELDS_VERSION, _load_cache_meta, _write_cache_meta


def _write(out, pool="hs300", start="2023-01-01", end="2026-08-15",
           adjust="1", stocks=270):
    _write_cache_meta(out, pool, start, end, adjust, stocks)


def test_cache_meta_roundtrip(tmp_path):
    _write(tmp_path)
    m = _load_cache_meta(tmp_path, "hs300", "2023-01-01", "2026-08-15", "1")
    assert m is not None
    assert m["stocks"] == 270
    assert m["fields_version"] == FIELDS_VERSION


def test_cache_miss_on_pool_change(tmp_path):
    _write(tmp_path, pool="hs300")
    assert _load_cache_meta(tmp_path, "zz500", "2023-01-01", "2026-08-15", "1") is None


def test_cache_miss_on_interval_change(tmp_path):
    _write(tmp_path)
    assert _load_cache_meta(tmp_path, "hs300", "2023-01-02", "2026-08-15", "1") is None
    assert _load_cache_meta(tmp_path, "hs300", "2023-01-01", "2026-08-14", "1") is None


def test_cache_miss_on_adjust_change(tmp_path):
    """P2-3: 复权口径变了（如改回前复权 adjust=2）必须重下"""
    _write(tmp_path, adjust="1")
    assert _load_cache_meta(tmp_path, "hs300", "2023-01-01", "2026-08-15", "2") is None


def test_cache_miss_on_old_fields_version(tmp_path):
    """P2-5: 旧字段版本（无 turn/factor 口径）不得复用"""
    meta = {"pool": "hs300", "start": "2023-01-01", "end": "2026-08-15",
            "adjust": "1", "fields_version": "v1", "stocks": 270}
    (tmp_path / ".fetch_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert _load_cache_meta(tmp_path, "hs300", "2023-01-01", "2026-08-15", "1") is None


def test_cache_miss_without_meta(tmp_path):
    assert _load_cache_meta(tmp_path, "hs300", "2023-01-01", "2026-08-15", "1") is None
