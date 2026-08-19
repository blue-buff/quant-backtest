"""B4 bootstrap 统计检验纯函数测试"""
import numpy as np
import pytest

from scripts.analysis_bootstrap import bootstrap_ci, sharpe_pvalue


def test_bootstrap_ci_contains_sample_mean():
    rng = np.random.default_rng(7)
    returns = rng.normal(0.001, 0.01, size=500)
    lo, hi, boot_mean = bootstrap_ci(returns, n_boot=2000, seed=1)
    assert lo <= returns.mean() <= hi
    assert lo < boot_mean < hi


def test_bootstrap_ci_bounds_around_zero_for_noise():
    returns = np.random.default_rng(3).normal(0.0, 0.01, size=300)
    lo, hi, _ = bootstrap_ci(returns, n_boot=1000, seed=5)
    assert lo < 0 < hi  # 纯噪声序列区间应跨越 0


def test_sharpe_pvalue_negative_mean_is_insignificant():
    returns = np.full(100, -0.001)
    p, obs = sharpe_pvalue(returns, n_boot=500, seed=1)
    assert obs < 0
    assert p > 0.5  # 负均值不拒绝 H0


def test_sharpe_pvalue_positive_strong_mean_significant():
    returns = np.full(300, 0.005)  # 恒定正收益
    p, obs = sharpe_pvalue(returns, n_boot=500, seed=1)
    assert p == 0.0
    assert obs > 0


def test_empty_returns_raises():
    with pytest.raises(ValueError):
        bootstrap_ci(np.array([]))
