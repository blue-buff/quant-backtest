"""股票池注册表测试（OPTIMIZATION.md C1）"""
import pytest

from qbt import pools


def test_all_pools_have_required_keys():
    assert len(pools.pool_names()) >= 2
    for name in pools.pool_names():
        p = pools.get_pool(name)
        assert set(p) >= pools.REQUIRED_KEYS, f"{name} 缺注册表字段"


def test_hs300_known_fields():
    p = pools.get_pool("hs300")
    assert p["query_fn"] == "query_hs300_stocks"
    assert p["index_code"] == "sh.000300"
    assert p["index_sym"] == "SH000300"
    assert p["universe"] == "csi300"
    assert p["yaml"].endswith("lightgbm_alpha158_full.yaml")
    assert p["strategy"].endswith("rq_strategy_qlib.py")


def test_zz500_known_fields():
    p = pools.get_pool("zz500")
    assert p["query_fn"] == "query_zz500_stocks"
    assert p["index_sym"] == "SH000905"
    assert p["universe"] == "csi500"
    assert p["yaml"].endswith("lightgbm_alpha158_zz500.yaml")
    assert p["strategy"].endswith("rq_strategy_qlib_zz500.py")
    assert p["qlib_dir"].endswith("cn_data_zz500")


def test_unknown_pool_raises():
    with pytest.raises(ValueError, match="未知股票池"):
        pools.get_pool("csi800")
