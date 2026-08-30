"""P7 T1: spec 校验（未知键警告、exp_id/base 必填）与 params 透传。"""
import pytest

from pipeline import spec as specmod


def test_validate_warn_unknown_key(capsys):
    spec = {"exp_id": "t_p1", "base": {}, "params": {"lr": 0.01}, "dataste": {"x": 1}}
    warns = specmod.validate_warn(spec)
    assert warns == ["dataste"]
    assert "QLAB_SPEC_WARN unknown key: dataste" in capsys.readouterr().err


def test_validate_warn_known_keys_silent(capsys):
    spec = {"exp_id": "t", "base": "ref:x", "overrides": {}, "changes": "c",
            "hypothesis": "h", "pre_registration": "p", "idea": "i",
            "expectation": {}, "runner": "auto", "timeout_min": 10,
            "action": {"kind": "smoke"}, "params": {}, "metrics": ["rankic"]}
    assert specmod.validate_warn(spec) == []
    assert capsys.readouterr().err == ""


def test_validate_missing_exp_id():
    with pytest.raises(ValueError, match="exp_id"):
        specmod.validate_warn({"base": {}})


def test_validate_missing_base():
    with pytest.raises(ValueError, match="base"):
        specmod.validate_warn({"exp_id": "t"})


def test_resolve_passes_params():
    spec = {"exp_id": "t_p1", "base": {}, "params": {"lr": 0.01},
            "action": {"kind": "smoke"}}
    eff = specmod.resolve(spec)
    assert eff["_params"] == {"lr": 0.01}
    assert eff["_exp_id"] == "t_p1"


def test_resolve_calls_validate_warn(capsys):
    spec = {"exp_id": "t", "base": {}, "bogus_key": 1}
    eff = specmod.resolve(spec)
    assert eff["_exp_id"] == "t"
    assert "QLAB_SPEC_WARN unknown key: bogus_key" in capsys.readouterr().err


def test_unknown_metric_family_warns(capsys):
    spec = {"exp_id": "t", "base": {}, "metrics": ["rankic", "bogus_family"]}
    eff = specmod.resolve(spec)
    assert eff["_exp_id"] == "t"
    assert "QLAB_SPEC_WARN unknown metric family: bogus_family" in capsys.readouterr().err


def test_known_metric_families_silent(capsys):
    spec = {"exp_id": "t", "base": {},
            "metrics": ["rankic", "bootstrap", "portfolio", "backtest", "attribution"]}
    specmod.resolve(spec)
    assert capsys.readouterr().err == ""


def test_hash_includes_metrics_and_expectation():
    """回归（审计 #1）：只改 metrics/expectation 必须得到不同 spec_hash。"""
    base = {"metrics": ["rankic"], "expectation": {"rankic_mean_min": 0.05}}

    def h(**over):
        s = {"exp_id": "hash_probe", "base": dict(base), **over}
        specmod.validate_warn(s)
        return specmod.spec_hash(specmod.resolve(s))

    a = h()
    b = h(metrics=["rankic", "bootstrap", "deciles", "quarters", "hit"],
          expectation={"sharpe_min": 1.0})
    c = h(expectation={"rankic_mean_min": 0.99})
    assert len({a, b, c}) == 3


def test_hash_metrics_order_canonical():
    """回归（审计 #1 细节）：metrics 列表顺序/重复不影响 hash（同口径不重跑）。"""

    def h(m):
        s = {"exp_id": "hash_probe", "base": {}, "metrics": m}
        specmod.validate_warn(s)
        return specmod.spec_hash(specmod.resolve(s))

    assert h(["rankic", "hit"]) == h(["hit", "rankic"])
    assert h(["rankic"]) == h(["rankic", "rankic"])


def test_top_level_metrics_override_base_in_hash():
    """回归（审计 #1 细节）：顶层显式 metrics 覆盖 base 同名键，不再出现
    base 键 + _metrics 双份导致的假差异。"""

    def h(spec):
        specmod.validate_warn(spec)
        return specmod.spec_hash(specmod.resolve(spec))

    s1 = {"exp_id": "hash_probe", "base": {"metrics": ["rankic"]}}
    s2 = {"exp_id": "hash_probe", "base": {"metrics": ["rankic"]},
          "metrics": ["rankic"]}  # 显式同值 = 语义相同
    assert h(s1) == h(s2)
    s3 = {"exp_id": "hash_probe", "base": {"metrics": ["rankic"]},
          "metrics": ["hit"]}
    assert h(s1) != h(s3)


def test_exp_id_path_segment_validation():
    """回归（审计 #4）：exp_id 必须是单路径段，'..'/斜杠直接拒绝。"""
    with pytest.raises(ValueError, match="exp_id"):
        specmod.validate_warn({"exp_id": "..", "base": {}})
    with pytest.raises(ValueError, match="exp_id"):
        specmod.validate_warn({"exp_id": "a/b", "base": {}})
    with pytest.raises(ValueError, match="exp_id"):
        specmod.validate_warn({"exp_id": "../../tmp/evil", "base": {}})
    specmod.validate_warn({"exp_id": "ok_1.2-b", "base": {}})
    specmod.validate_warn({"exp_id": "p8_torch_gpu_hs300_10d", "base": {}})
